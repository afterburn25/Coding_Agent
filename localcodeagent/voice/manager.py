"""VoiceManager — single point for synthesis, queueing, playback state,
muting and metrics. Frontend plays served WAVs; the manager tracks response
and segment ids so stale/dup speech is suppressed across reconnects.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from collections import OrderedDict, deque
from pathlib import Path
from typing import Any, Callable

from . import dsp
from .cache import AudioCache
from .engine import VoiceEngineError, get_engine
from .presets import VoicePresetStore
from .speech_filter import SpeechTextFilter
from .streamer import SentenceStreamer, split_for_speech
from .types import VoicePreset
from .vocalizations import VocalizationEngine, adapter_for

log = logging.getLogger(__name__)


class SpeechJob:
    __slots__ = ("job_id", "task_id", "seq", "text", "preset_id", "speed",
                 "delivery", "cancelled", "created_at", "events", "priority",
                 "raw_end")

    def __init__(self, task_id: str, seq: int, text: str, preset_id: str,
                 speed: float, delivery: dict | None = None,
                 raw_end: int = 0) -> None:
        self.job_id = uuid.uuid4().hex[:16]
        self.task_id = task_id
        self.seq = seq
        self.text = text
        self.preset_id = preset_id
        self.speed = speed
        self.delivery = delivery or {}
        self.cancelled = False
        self.created_at = time.time()
        self.events: list[dict] = []
        self.priority = False
        # Raw-response-stream offset where this segment's source text ends —
        # the client reveals display text up to here when playback starts.
        self.raw_end = int(raw_end)


class VoiceManager:
    """Owns engines, presets, cache, the synthesis queue and spoken state."""

    def __init__(self, config, preset_dir: Path, cache_dir: Path,
                 publish: Callable[[str, dict], None] | None = None,
                 asset_dir: Path | None = None,
                 persist: Callable[[], None] | None = None,
                 personality_voice: Callable[[], dict] | None = None,
                 persona_context: Callable[[], dict] | None = None) -> None:
        self.config = config
        # Zero-arg resolver returning the active profile's delivery map
        # ({preset_id?, speed?, pitch_semitones?, output_gain_db?});
        # failures/None mean "no personality delivery".
        self._personality_voice = personality_voice
        # Zero-arg resolver returning the persona context the
        # VocalizationEngine gates on: {profile_id, style, strength,
        # mood, is_adult, level}.
        self._persona_context = persona_context
        self.asset_dir = Path(asset_dir) if asset_dir else None
        self._persist = persist or (lambda: None)
        self.presets = VoicePresetStore(Path(preset_dir))
        self.cache = AudioCache(Path(cache_dir))
        self.filter = SpeechTextFilter()
        self._publish = publish or (lambda kind, payload: None)
        self._lock = threading.Lock()
        self._queue: deque[SpeechJob] = deque()
        self._active_job: SpeechJob | None = None
        self._worker: threading.Thread | None = None
        self._wake = threading.Event()
        self._streamers: dict[str, SentenceStreamer] = {}
        self._spoken_tasks: dict[str, int] = {}  # task_id -> next seq
        self._current_task: str = ""
        # After the startup greeting's audio publishes, hold every other
        # utterance until it finishes playing plus a settle gap — no
        # report may talk over the greeting.
        self._greeting_hold_until = 0.0
        self._greeting_settle_s = 5.0
        self._engines: dict[str, Any] = {}
        self._muted_at = 0.0
        self.segments: dict[str, Path] = {}  # seg_id -> wav path (recent)
        self._seg_order: deque[str] = deque(maxlen=200)
        # task_id -> ordered seg_ids — lets the 🔊 replay button re-serve
        # the exact clips a reply spoke with, instead of re-synthesizing
        # the text without the persona delivery plan (wrong pace/tone).
        self._task_segments: OrderedDict[str, list[str]] = OrderedDict()
        self.vocal = VocalizationEngine(
            adapter_for(getattr(config, "voice_engine", "kokoro")),
            publish=self._publish)

    # -- config helpers -----------------------------------------------------
    def enabled(self) -> bool:
        return bool(getattr(self.config, "voice_enabled", True))

    def muted(self) -> bool:
        return bool(getattr(self.config, "voice_muted", False))

    def mode(self) -> str:
        return getattr(self.config, "voice_mode", "responses")

    def preset_id(self) -> str:
        pid = getattr(self.config, "voice_preset_id", "")
        if pid:
            return pid
        listed = self.presets.list()
        return listed[0].id if listed else ""

    def current_preset(self) -> VoicePreset | None:
        pid = getattr(self.config, "voice_preset_id", "")
        p = self.presets.get(pid) if pid else None
        if p is None:
            p = self.presets.get("nexus-synthetic-isabella")
        if p is None:
            listed = self.presets.list()
            p = listed[0] if listed else None
        return p

    def engine(self, name: str = ""):
        name = name or getattr(self.config, "voice_engine", "kokoro")
        if name not in self._engines:
            eng = get_engine(name)
            if self.asset_dir is not None and hasattr(eng, "asset_dir"):
                eng.asset_dir = Path(self.asset_dir)
            if name == "chatterbox" and hasattr(eng, "runtime_dir"):
                eng.synth_timeout_s = float(getattr(
                    self.config, "voice_chatterbox_synth_timeout_s", 120.0))
                # Isolated runtime resolves like the asset dir: relative
                # paths anchor at the install root (asset_dir's parent).
                rd = str(getattr(self.config,
                                 "voice_chatterbox_runtime_dir",
                                 "runtime/voice/chatterbox"))
                rpath = Path(rd)
                if not rpath.is_absolute() and self.asset_dir is not None:
                    rpath = Path(self.asset_dir).parent.parent / rpath
                eng.runtime_dir = rpath
                eng.device_pref = str(getattr(
                    self.config, "voice_chatterbox_device", "auto"))
                eng.min_free_vram_mb = float(getattr(
                    self.config, "voice_chatterbox_min_free_vram_mb",
                    3200))
                eng.dtype_pref = str(getattr(
                    self.config, "voice_chatterbox_dtype", "bf16"))
            self._engines[name] = eng
            if name == (self.current_preset().engine
                        if self.current_preset() else ""):
                self._write_startup_sig(eng)
        return self._engines[name]

    def _startup_sig_raw(self, eng) -> str:
        preset = self.current_preset()
        # Preset CONTENT must salt the narrator cache: preset ids are
        # stable across tuning changes, so without the content hash a
        # clip rendered under an older recipe replays forever under the
        # same id (observed: pre-V7 nexus-isabella-chatterbox wavs kept
        # matching the post-V7 sig).
        phash = (AudioCache.preset_hash(preset.to_json())
                 if preset is not None else "")
        return "|".join([
            getattr(eng, "name", ""), getattr(eng, "version", ""),
            dsp.DSP_VERSION, preset.id if preset else "", phash])

    def _write_startup_sig(self, eng) -> None:
        """Publish the active engine signature for StartupNarrator.cs —
        it salts its per-line wav cache with this so switching engine /
        preset / DSP can never replay a stale clip of the wrong voice."""
        try:
            raw = self._startup_sig_raw(eng)
            import hashlib
            sig = hashlib.sha256(raw.encode()).hexdigest()[:12]
            self._last_sig_raw = raw
            target = Path(getattr(self.config, "voice_cache_dir",
                                  "data/voice/cache"))
            if not target.is_absolute() and self.asset_dir is not None:
                target = Path(self.asset_dir).parent.parent / target
            startup = target.parent / "startup"
            startup.mkdir(parents=True, exist_ok=True)
            preset = self.current_preset()
            (startup / "engine.json").write_text(json.dumps({
                "sig": sig, "engine": getattr(eng, "name", ""),
                "engine_version": getattr(eng, "version", ""),
                "preset_id": preset.id if preset else "",
            }), encoding="utf-8")
        except Exception:
            pass

    def _sync_adapter(self, engine_name: str = "") -> None:
        """Vocalization adapter follows the active preset's engine —
        enqueue-time resolve happens before _synthesize picks the engine,
        so the binding has to live here, not in _synthesize."""
        try:
            name = engine_name or (self.current_preset().engine
                                   if self.current_preset()
                                   else getattr(self.config, "voice_engine",
                                                "kokoro"))
            if name == getattr(self.vocal.adapter, "name", ""):
                return
            adapter = adapter_for(name)
            if (name == "chatterbox"
                    and hasattr(adapter, "supported")
                    and "chatterbox" in self._engines):
                caps = getattr(self._engines["chatterbox"],
                               "supported_tags", None)
                if caps:
                    adapter.supported = set(caps)
            self.vocal.adapter = adapter
        except Exception:
            pass

    # -- mute / playback control ------------------------------------------
    def repeat_last(self) -> bool:
        """Re-queue the most recently spoken utterance. Returns False
        when nothing has been spoken yet (or voice is muted/off —
        enqueue drops those anyway)."""
        task_id, text = getattr(self, "_last_spoken", (None, ""))
        # Re-publish the exact recorded segments when they still exist —
        # re-synthesizing the text would lose the persona delivery plan
        # (pace/tone/pauses) the reply originally spoke with.
        seg_ids = self.segments_for_task(task_id) if task_id else []
        if seg_ids:
            for seq, sid in enumerate(seg_ids):
                self._publish("voice", {
                    "event": "segment", "task_id": task_id, "seq": seq,
                    "segment_id": sid, "url": f"/api/voice/audio/{sid}"})
            return True
        if not text:
            return False
        return self.enqueue(f"repeat-{uuid.uuid4().hex[:8]}",
                            text, priority=True) is not None

    def set_muted(self, muted: bool) -> dict[str, Any]:
        self.config.voice_muted = bool(muted)
        try:
            self._persist()
        except Exception:
            pass
        if muted:
            self._muted_at = time.time()
            self.stop_all(reason="muted")
        self._publish("voice", {"event": "muted", "muted": bool(muted)})
        return {"ok": True, "muted": bool(muted)}

    def stop_all(self, reason: str = "stop") -> dict[str, Any]:
        """Cancel queue + active job; playback elements stop client-side on
        the voice 'stop' event."""
        with self._lock:
            for job in self._queue:
                job.cancelled = True
            self._queue.clear()
            if self._active_job is not None:
                self._active_job.cancelled = True
        self._publish("voice", {"event": "stop", "reason": reason})
        return {"ok": True, "stopped": True}

    def _persona_ctx(self) -> dict:
        try:
            return (self._persona_context() or {}
                    if callable(self._persona_context) else {})
        except Exception:
            return {}

    def resolve_speech(self, text: str, *, task_id: str = "") -> str:
        """filter → vocalization resolution → final TTS input. The direct
        paths (speak, preview, tools) share this so every spoken surface
        gets the same non-verbal handling."""
        self._sync_adapter()
        spoken = self.filter.filter(text)
        if not spoken:
            return ""
        return self.vocal.resolve(
            spoken, task_id=task_id or "manual",
            ctx=self._persona_ctx()).speech_text

    def begin_task(self, task_id: str, user_text: str = "") -> None:
        """A new user request / response starts: drop stale speech."""
        try:
            self.vocal.begin_task(task_id, user_text=user_text)
        except Exception:
            pass
        with self._lock:
            stale = task_id != self._current_task
            self._current_task = task_id
            self._streamers.setdefault(task_id, SentenceStreamer(self.filter))
            self._spoken_tasks.setdefault(task_id, 0)
            # Bound streamer bookkeeping for long sessions.
            if len(self._streamers) > 32:
                for k in list(self._streamers):
                    if k != task_id:
                        self._streamers.pop(k, None)
            if len(self._spoken_tasks) > 256:
                for k in list(self._spoken_tasks)[: len(self._spoken_tasks) - 128]:
                    self._spoken_tasks.pop(k, None)
        if stale:
            self.stop_all(reason="new_response")
        # Start warming the active preset's engine while the model is
        # still thinking/streaming — the ~9 s worker spawn overlaps the
        # turn instead of stalling the first spoken sentence. Warming
        # is async and bounded by the VRAM-pressure unload, so a turn
        # that ends up unspoken only costs a re-loadable worker.
        self._warm_on_first_speech()

    def _warm_engine(self) -> None:
        try:
            preset = self.current_preset()
            self.engine(preset.engine if preset is not None else "").load()
        except Exception:
            pass

    def _warm_on_first_speech(self) -> None:
        """Pre-warm the TTS engine for a task that will likely speak.

        Called at task start (the ~1–9 s worker spawn then overlaps
        model thinking/streaming) and defensively again at first
        enqueue. Guarded by enabled/muted/mode and a one-flight flag;
        the VRAM-pressure idle unloader reclaims a worker warmed for a
        turn that never speaks."""
        if getattr(self, "_warming", False):
            return
        if not (self.enabled() and not self.muted()
                and self.mode() in {"responses", "responses_activity"}):
            return
        try:
            # Warm the engine the active preset actually uses — warming
            # the config default instead loads the wrong stack entirely
            # (e.g. Kokoro while a Chatterbox preset still cold-starts
            # its worker at synthesis time).
            preset = self.current_preset()
            eng = self.engine(preset.engine if preset is not None else "")
            if getattr(eng, "_model", None) is not None or getattr(
                    eng, "_loaded", False):
                return
        except Exception:
            return
        self._warming = True
        def _run() -> None:
            try:
                self._warm_engine()
            finally:
                self._warming = False
        threading.Thread(target=_run,
                         name="nexus-voice-warm", daemon=True).start()

    def feed_token(self, task_id: str, delta: str) -> int:
        """Feed a token delta; returns number of sentences enqueued."""
        if not self.enabled() or self.muted() or self.mode() not in {
                "responses", "responses_activity"}:
            return 0
        streamer = self._streamers.get(task_id)
        if streamer is None:
            self.begin_task(task_id)
            streamer = self._streamers[task_id]
        n = 0
        sents = streamer.feed(delta)
        spans = streamer.pop_emit_spans()
        for i, sent in enumerate(sents):
            self.enqueue(task_id, sent,
                         raw_end=spans[i] if i < len(spans) else 0)
            n += 1
        return n

    def finish_task(self, task_id: str, final_text: str | None = None,
                    delivery: dict | None = None) -> None:
        """Flush the streamer tail; if nothing was emitted (non-streamed or
        fully skipped), fall back to a filtered one-shot of final_text.

        ``delivery`` is a SpeechDeliveryPlan dict from the persona speech
        genome — the voice layer consumes what the engine can express
        (``pace`` → job speed + register damp; ``energy``/``warmth``/
        ``emphasis_level``/``seriousness`` → bounded pitch/gain deltas;
        ``pause_hint`` → real clause-boundary pause; ``nonverbal_rate``
        → vocalization keep-probability). ``emphasis_spans`` has no
        engine control and stays a documented hint, never fabricated."""
        streamer = self._streamers.pop(task_id, None)
        pace = 1.0
        try:
            if isinstance(delivery, dict):
                pace = max(0.5, min(1.8,
                                    float(delivery.get("pace") or 1.0)))
                # Register-aware delivery: technical/formal registers read
                # a touch slower for clarity; casual stays untouched.
                if str(delivery.get("register") or "") in (
                        "technical", "formal"):
                    pace = max(0.5, min(1.8, pace * 0.97))
        except (TypeError, ValueError):
            pace = 1.0
        try:
            if self.enabled() and not self.muted():
                if streamer:
                    tail = streamer.flush()
                    spans = streamer.pop_emit_spans()
                    for i, sent in enumerate(tail):
                        self.enqueue(
                            task_id, sent, speed=pace, delivery=delivery,
                            raw_end=spans[i] if i < len(spans) else 0)
                # Count after flush: a reply that never crossed a sentence
                # boundary during feed still speaks via the flush tail —
                # checking before flush would re-speak the same text through
                # the final_text fallback (the "double voice" bug).
                n_emitted = streamer.emitted_count if streamer else 0
                if n_emitted == 0 and final_text and self.mode() in {
                        "responses", "responses_activity"}:
                    spoken = self.filter.filter(final_text)
                    if spoken:
                        for sent in _split_sentences(spoken):
                            for part in split_for_speech(sent):
                                # raw_end 0 signals "reveal everything
                                # remaining" — no stream positions exist.
                                self.enqueue(task_id, part,
                                             speed=pace,
                                             delivery=delivery,
                                             raw_end=-1)
        finally:
            try:
                self.vocal.end_task(task_id)
            except Exception:
                pass

    # -- queue --------------------------------------------------------------
    def enqueue(self, task_id: str, text: str, *,
                preset_id: str | None = None, speed: float = 1.0,
                delivery: dict | None = None,
                priority: bool = False,
                vocalize: bool = True,
                raw_end: int = 0) -> SpeechJob | None:
        """vocalize=False skips vocalization resolution — for
        system-authored notices whose persona lead-ins are already
        final text ("Oof — …" must not be re-detected and stripped)."""
        if not self.enabled() or self.muted():
            return None
        text = text.strip()
        if not text:
            return None
        if vocalize:
            try:
                self._sync_adapter(
                    self.presets.get(preset_id).engine
                    if preset_id and self.presets.get(preset_id) else "")
                vres = self.vocal.resolve(text, task_id=task_id,
                                          ctx=self._persona_ctx())
                text, events = vres.speech_text, vres.events
            except Exception:
                events = []
        else:
            events = []
        if not text.strip():
            return None
        text = _apply_pause_hint(text, delivery)
        # Skip the engine pre-warm when the utterance is already rendered —
        # replaying a cached wav must not spin up a ~2 GB GPU worker.
        probe_preset = (self.presets.get(preset_id)
                        if preset_id else None) or self.current_preset()
        if probe_preset is None or not self._cache_probe(
                text, probe_preset, speed, delivery=delivery):
            self._warm_on_first_speech()
        with self._lock:
            seq = self._spoken_tasks.get(task_id, 0)
            self._spoken_tasks[task_id] = seq + 1
            job = SpeechJob(task_id, seq, text,
                            preset_id or (self.current_preset().id
                                          if self.current_preset() else ""),
                            speed, delivery=delivery, raw_end=raw_end)
            job.events = events
            if priority:
                # Ahead of normal jobs but behind earlier priority jobs —
                # plain appendleft would reverse a burst of notices.
                idx = 0
                while (idx < len(self._queue)
                       and getattr(self._queue[idx], "priority", False)):
                    idx += 1
                self._queue.insert(idx, job)
                job.priority = True
            else:
                self._queue.append(job)
            self._ensure_worker()
        self._publish("voice", {"event": "queued", "task_id": task_id,
                                "seq": seq, "job_id": job.job_id})
        return job

    def speak_greeting(self, task_id: str, text: str, *,
                       speed: float = 1.0,
                       publish: bool = True) -> dict[str, Any] | None:
        """Synchronous greeting synthesis — returns the segment so the
        caller can hand its URL to the requesting client directly.

        The queued path alone published only an ephemeral bus segment:
        on app boot the page's voice EventSource was often still
        connecting when it fired, so the greeting was lost with no
        replay. Returning the URL makes delivery deterministic while the
        bus publish (same segment_id) keeps other pages in sync —
        clients dedupe by segment id.
        """
        if not self.enabled() or self.muted():
            return None
        preset = self.current_preset()
        if preset is None:
            return None
        self._sync_adapter(preset.engine)
        spoken = self.filter.filter(text)
        if not spoken:
            return None
        try:
            vres = self.vocal.resolve(spoken, task_id=task_id,
                                      ctx=self._persona_ctx())
        except Exception:
            vres = None
        spoken = getattr(vres, "speech_text", "") or spoken
        if not spoken.strip():
            return None
        pcm, sr, seg_path = self._synthesize(spoken, preset, speed)
        seg_id = self._register_segment(seg_path, task_id)
        payload = {
            "event": "segment", "task_id": task_id, "seq": 0,
            "segment_id": seg_id, "url": f"/api/voice/audio/{seg_id}",
            "seconds": round(pcm.shape[0] / sr, 2),
            "text": spoken,
        }
        events = getattr(vres, "events", None) or []
        if events:
            payload["gestures"] = [{**e, "utterance_id": seg_id}
                                   for e in events]
        with self._lock:
            if task_id.startswith("greet-"):
                # Same hold the queued greeting applies — nothing talks
                # over her hello.
                self._greeting_hold_until = (
                    time.monotonic()
                    + float(payload["seconds"]) + self._greeting_settle_s)
            self._last_spoken = (task_id, text)
        if publish:
            self._publish("voice", payload)
        return {"ok": True, "segment_id": seg_id, "url": payload["url"],
                "seconds": payload["seconds"]}

    def speak_text(self, text: str, *, preset_id: str | None = None,
                   speed: float = 1.0, auto_filter: bool = True,
                   task_id: str = "",
                   delivery: dict | None = None) -> dict[str, Any]:
        """Direct (non-queued) speak: filter → synth → returns audio id.
        Used by per-message replay, preview and tools. ``delivery``
        accepts a speech-genome delivery plan so previews can audition
        persona pacing/energy/warmth honestly."""
        if not self.enabled():
            raise VoiceEngineError("voice subsystem is disabled")
        preset = self.presets.get(preset_id) if preset_id else self.current_preset()
        if preset is None:
            raise VoiceEngineError("no voice preset configured")
        if auto_filter:
            spoken = self.resolve_speech(text, task_id=task_id or "manual")
        else:
            # Raw text still gets vocalization resolution — a hand-typed
            # "MMM" must never reach the synthesizer as spelled letters.
            spoken = self.vocal.resolve(
                text.strip(), task_id=task_id or "manual",
                ctx=self._persona_ctx()).speech_text
        if not spoken:
            raise VoiceEngineError("nothing speakable in the provided text")
        spoken = _apply_pause_hint(spoken, delivery)
        pcm, sr, seg = self._synthesize(spoken, preset, speed,
                                      delivery=delivery)
        seg_id = self._register_segment(seg, task_id or "manual")
        return {"ok": True, "segment_id": seg_id, "url": f"/api/voice/audio/{seg_id}",
                "seconds": round(pcm.shape[0] / sr, 2), "preset_id": preset.id,
                "text": spoken[:200]}

    # -- synthesis worker ----------------------------------------------------
    def _ensure_worker(self) -> None:
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._run_queue,
                                            name="nexus-voice", daemon=True)
            self._worker.start()

    def _run_queue(self) -> None:
        while True:
            with self._lock:
                job = self._queue.popleft() if self._queue else None
                self._active_job = job
            if job is None:
                with self._lock:
                    if not self._queue:
                        return
                continue
            if job.cancelled or self.muted():
                continue
            # HARD RULE: while a greeting is speaking — plus a settle gap
            # after it ends — every other utterance waits its turn.
            if not job.task_id.startswith("greet-"):
                with self._lock:
                    delay = self._greeting_hold_until - time.monotonic()
                if delay > 0:
                    time.sleep(min(delay, 30.0))
                    if job.cancelled or self.muted():
                        continue
            preset = self.presets.get(job.preset_id) or self.current_preset()
            if preset is None:
                continue
            try:
                pcm, sr, seg_path = self._synthesize(
                    job.text, preset, job.speed, delivery=job.delivery)
            except VoiceEngineError as exc:
                self._publish("voice", {"event": "error", "task_id": job.task_id,
                                        "seq": job.seq, "error": str(exc)[:200]})
                continue
            except Exception as exc:  # never kill the queue thread
                self._publish("voice", {"event": "error", "task_id": job.task_id,
                                        "seq": job.seq, "error": f"{exc}"[:200]})
                continue
            if job.cancelled or self.muted():
                continue
            seg_id = self._register_segment(seg_path, job.task_id)
            payload = {
                "event": "segment", "task_id": job.task_id, "seq": job.seq,
                "segment_id": seg_id, "url": f"/api/voice/audio/{seg_id}",
                "seconds": round(pcm.shape[0] / sr, 2),
                # Spoken text + raw-response offset for text/voice sync:
                # the client reveals display text up to raw_end when this
                # segment starts playing (-1 = reveal all remaining).
                "text": job.text,
                "raw_end": int(job.raw_end),
            }
            if job.events:
                # Gesture/vocalization metadata rides the segment event —
                # the utterance id is segment_id so a future avatar can
                # align gesture timing to playback (lip-sync).
                payload["gestures"] = [{**e, "utterance_id": seg_id}
                                       for e in job.events]
            self._publish("voice", payload)
            with self._lock:
                if job.task_id.startswith("greet-"):
                    # The client plays the segment on arrival — the hold
                    # covers its audio duration plus the settle gap.
                    self._greeting_hold_until = (
                        time.monotonic()
                        + float(payload.get("seconds") or 0)
                        + self._greeting_settle_s)
                # Track the last actually-spoken utterance so
                # "say that again" can replay it on demand.
                self._last_spoken = (job.task_id, job.text)

    def _apply_delivery(self, preset: VoicePreset, speed: float,
                        vmap: dict) -> tuple[VoicePreset, float]:
        """Fold the profile's personality delivery into (preset, speed).

        Rate rides the engine `speed` arg — preset.tempo stays as saved
        so the rate isn't applied twice through the DSP chain."""
        pid = vmap.get("preset_id")
        if pid:
            p2 = self.presets.get(str(pid))
            if p2 is not None:
                preset = p2
        # Delivery map values are DELTAS — they ride on top of the preset's
        # own pitch/gain so the chosen voice's signature is never wiped out.
        changes = {k: getattr(preset, k) + float(vmap[k])
                   for k in ("pitch_semitones", "output_gain_db")
                   if isinstance(vmap.get(k), (int, float)) and vmap[k]}
        if changes:
            raw = preset.as_dict()
            raw.update(changes)
            preset = VoicePreset.from_dict(raw)
        if isinstance(vmap.get("speed"), (int, float)) and vmap["speed"]:
            speed = max(0.5, min(2.0, float(speed) * float(vmap["speed"])))
        return preset, speed

    def _delivery_preset(self, preset: VoicePreset,
                         delivery: dict) -> VoicePreset:
        """Fold speech-genome delivery-plan characteristics the engine
        can express into a per-utterance preset variant.

        ``pace`` rides the job's speed (set in ``finish_task``, with a
        small register-aware damp for technical/formal); here
        ``energy``/``warmth``/``emphasis_level``/``seriousness`` become
        small bounded pitch/gain deltas around neutral so the persona
        voice signature stays intact. ``pause_hint`` inserts a real
        clause-boundary pause via ``_apply_pause_hint`` and
        ``nonverbal_rate`` scales vocalization keep-probability in the
        VocalizationEngine. ``emphasis_spans`` remains a documented
        hint (span-protection metadata) — Kokoro has no per-word
        emphasis control, so it is never faked."""
        if not isinstance(delivery, dict) or not delivery:
            return preset

        def _norm(key: str, default: float = 0.5) -> float:
            try:
                return max(0.0, min(1.0, float(delivery.get(key, default))))
            except (TypeError, ValueError):
                return default

        energy = _norm("energy")
        warmth = _norm("warmth")
        emphasis = _norm("emphasis_level", 0.0)
        d_pitch = ((energy - 0.5) * 1.0 - (warmth - 0.5) * 0.6
                   + emphasis * 0.2)
        d_gain = (energy - 0.5) * 4.0 + emphasis * 1.2
        # Seriousness-aware delivery: grave contexts soften and flatten
        # the voice a touch — bounded so the persona signature survives.
        try:
            if int(delivery.get("seriousness") or 0) >= 2:
                d_pitch -= 0.15
                d_gain -= 0.6
        except (TypeError, ValueError):
            pass
        d_pitch = max(-1.0, min(1.0, d_pitch))
        d_gain = max(-3.0, min(3.0, d_gain))
        if abs(d_pitch) < 0.05 and abs(d_gain) < 0.15:
            return preset
        raw = preset.as_dict()
        raw["pitch_semitones"] = (float(getattr(preset, "pitch_semitones", 0.0))
                                  + d_pitch)
        raw["output_gain_db"] = (float(getattr(preset, "output_gain_db", 0.0))
                                 + d_gain)
        return VoicePreset.from_dict(raw)

    def _synth_key(self, text: str, preset: VoicePreset, speed: float,
                   *, apply_personality: bool = True,
                   delivery: dict | None = None):
        """Resolve the cache key exactly as synthesis computes it — shared
        with enqueue's cache probe so a fully-cached utterance can skip the
        engine pre-warm instead of pinning ~2 GB VRAM to replay a wav."""
        if apply_personality:
            try:
                vmap = (self._personality_voice() or {}
                        if callable(self._personality_voice) else {})
            except Exception:
                vmap = {}
            if vmap:
                preset, speed = self._apply_delivery(preset, speed, vmap)
        preset = self._delivery_preset(preset, delivery or {})
        overrides = getattr(preset, "pronunciation_overrides", None) or {}
        if overrides:
            # Per-preset token rewrites — same expansion style as the
            # speech filter's acronym table. Longest-first so multi-word
            # or compound keys match before their prefixes.
            for src in sorted(overrides, key=len, reverse=True):
                text = re.sub(rf"\b{re.escape(str(src))}\b",
                              str(overrides[src]), text)
        engine = self.engine(preset.engine)
        if self._startup_sig_raw(engine) != getattr(
                self, "_last_sig_raw", None):
            self._write_startup_sig(engine)
        preset_json = preset.to_json()
        key = AudioCache.key(text, preset.engine,
                             f"{engine.version}|dsp{dsp.DSP_VERSION}",
                             preset.base_voice,
                             AudioCache.preset_hash(preset_json), speed)
        return engine, key, text, preset, speed, preset_json

    def _cache_probe(self, text: str, preset: VoicePreset, speed: float,
                     delivery: dict | None = None) -> bool:
        """True when this utterance is already rendered on disk. Probe
        failures answer False — warming anyway is the safe default."""
        try:
            _e, key, _t, _p, _s, _j = self._synth_key(
                text, preset, speed, delivery=delivery)
            return self.cache.get(key) is not None
        except Exception:
            return False

    def _synthesize(self, text: str, preset: VoicePreset, speed: float,
                    *, apply_personality: bool = True,
                    delivery: dict | None = None):
        """Full pipeline → stereo WAV on disk. Returns (pcm, sr, path)."""
        engine, key, text, preset, speed, preset_json = self._synth_key(
            text, preset, speed, apply_personality=apply_personality,
            delivery=delivery)
        hit = self.cache.get(key)
        if hit is not None:
            # Decode header for reported duration (avoid re-encode).
            import wave
            with wave.open(str(hit), "rb") as w:
                frames, sr = w.getnframes(), w.getframerate()
            pcm = Path(hit).read_bytes()  # placeholder shape for caller
            return _CachedAudio(frames), sr, hit
        fallback = False
        # Global loudness settings apply as preset overrides — the preset
        # stays the single source of truth for the DSP chain and
        # dsp.process keeps its stable signature. Applied before the
        # quality gate so scored trials equal the cached audio.
        import dataclasses as _dc
        overrides: dict[str, Any] = {}
        if not getattr(self.config, "voice_normalize_loudness", True):
            overrides["normalize_loudness"] = False
        _tgt = float(getattr(self.config, "voice_target_lufs", -14.0))
        if abs(_tgt + 14.0) > 0.01:
            overrides["loudness_target_lufs"] = _tgt
        if not getattr(self.config, "voice_limiter_enabled", True):
            overrides["limiter_enabled"] = False
        if overrides:
            preset = _dc.replace(preset, **overrides)
        # Stochastic-draw quality gate: Chatterbox sampling (temperature
        # 0.72) can land dark/reverberant/boomy renders — the audible
        # "barrel" — most often on short text. Each draw is scored
        # post-DSP against the measured golden-band (good live draws sit
        # ≥ ~3.4 kHz centroid, ≤0.55 echo-lag corr, ≤−16 dB 100–200 Hz
        # share — the golden measures ≈−21 dB there); rejected draws
        # re-generate up to `voice_chatterbox_quality_retries` times and
        # the best draw wins.
        retries = max(0, int(getattr(
            self.config, "voice_chatterbox_quality_retries", 3)))
        # Paralinguistic-tag segments legitimately fail the speech band —
        # a laugh measures "dark + echoey" (1591 Hz / 0.91 live) without
        # being a bad draw. Skip gating when tags are present.
        gate = (retries > 0 and preset.engine == "chatterbox"
                and "[" not in text)
        # Hallucination guard: stochastic draws occasionally drift off the
        # text entirely — spoken word salad passes every spectral gate but
        # runs far longer (rambling) or shorter (mumbled collapse) than the
        # sentence warrants. ~15 chars/sec is Chatterbox's observed cadence
        # at speed 1.0; ±loose bounds catch only true drift, never phrasing.
        expected_s = max(0.4, len(text) / 15.0) / max(0.5, speed)
        best: tuple[float, Any, int, Any] | None = None
        tries = 0
        drift_fails = 0
        try:
            for _ in range(1 + (retries if gate else 0)):
                audio, sr = engine.synthesize(
                    text, voice=preset.base_voice, speed=speed,
                    lang=_lang_tag(preset.language))
                tries += 1
                if not gate or audio.size < sr // 2:
                    best = (0.0, audio, sr, None)
                    break
                dur = audio.size / sr
                if dur > expected_s * 2.2 or dur < expected_s * 0.45:
                    drift_fails += 1
                    log.info("chatterbox draw %d rejected "
                             "(duration %.1fs vs expected ~%.1fs — "
                             "text drift) — redrawing",
                             tries, dur, expected_s)
                    continue
                trial = dsp.process(audio, sr, preset)
                mono = trial.mean(axis=1)
                centroid = dsp.spectral_centroid_hz(mono, sr)
                echo = dsp.echo_lag_corr(mono, sr)
                boom = dsp.band_share_db(mono, sr, 100.0, 200.0)
                # Best-of scoring must penalize every gated axis —
                # otherwise an all-fail round can keep the boomy draw.
                score = (centroid / 4400.0 - echo
                         - max(0.0, boom + 16.0) / 10.0)
                if best is None or score > best[0]:
                    best = (score, audio, sr, trial)
                if (centroid >= 3400.0 and echo <= 0.55
                        and boom <= -16.0):
                    break
                log.info("chatterbox draw %d rejected "
                         "(centroid %.0f Hz, echo %.2f, "
                         "low-band %.1f dB) — redrawing",
                         tries, centroid, echo, boom)
        except VoiceEngineError:
            # Chatterbox unavailable (runtime/model missing, worker
            # dead, VRAM floor not met) — degrade to the legacy engine
            # rather than dropping speech entirely.
            if preset.engine == "kokoro":
                raise
            log.warning("engine %s failed — falling back to kokoro",
                        preset.engine, exc_info=True)
            self._publish("voice", {
                "event": "engine_fallback", "engine": preset.engine,
                "fallback": "kokoro"})
            kok = self.engine("kokoro")
            fb_voice = preset.base_voice
            try:
                # The preset's voice id belongs to the failed engine —
                # isabella isn't a Kokoro voice. Map to the closest
                # Kokoro equivalent (bf_isabella is the reference's own
                # source voice) or Kokoro's first available voice.
                kok_ids = {str(v.get("id")) for v in kok.voices()}
                if fb_voice not in kok_ids:
                    fb_voice = ("bf_isabella" if "bf_isabella" in kok_ids
                                else next(iter(sorted(kok_ids)),
                                          fb_voice))
            except Exception:
                pass
            audio, sr = kok.synthesize(
                text, voice=fb_voice, speed=speed,
                lang=_lang_tag(preset.language))
            fallback = True
            best = None
        # Every draw drifted off-text — speaking it would be confident
        # gibberish. Degrade this utterance to the deterministic engine
        # instead of publishing a hallucination.
        if (best is None and not fallback and drift_fails >= tries
                and preset.engine != "kokoro"):
            log.warning("all %d chatterbox draws drifted off-text — "
                        "falling back to kokoro for this utterance",
                        drift_fails)
            self._publish("voice", {
                "event": "engine_fallback", "engine": preset.engine,
                "fallback": "kokoro", "reason": "text_drift"})
            kok = self.engine("kokoro")
            fb_voice = preset.base_voice
            try:
                kok_ids = {str(v.get("id")) for v in kok.voices()}
                if fb_voice not in kok_ids:
                    fb_voice = ("bf_isabella" if "bf_isabella" in kok_ids
                                else next(iter(sorted(kok_ids)),
                                          fb_voice))
            except Exception:
                pass
            audio, sr = kok.synthesize(
                text, voice=fb_voice, speed=speed,
                lang=_lang_tag(preset.language))
            fallback = True
        if tries > 1:
            self._publish("voice", {
                "event": "quality_redraw", "attempts": tries,
                "engine": preset.engine})
        if best is not None:
            audio, sr = best[1], best[2]
        stereo = (best[3] if best is not None and best[3] is not None
                  else dsp.process(audio, sr, preset))
        wav = dsp.wav_bytes(stereo, sr)
        # Fallback audio is cached under the *kokoro* engine key — a
        # degraded render must never masquerade as chatterbox output,
        # and it keeps its own lifetime so real chatterbox audio
        # replaces it naturally once the engine recovers.
        if fallback:
            key = AudioCache.key(
                text, "kokoro",
                f"{self.engine('kokoro').version}|dsp{dsp.DSP_VERSION}",
                preset.base_voice,
                AudioCache.preset_hash(preset_json), speed)
        path = self.cache.put(key, wav)
        return stereo, sr, path

    def _register_segment(self, path: Path, task_id: str) -> str:
        seg_id = uuid.uuid4().hex[:16]
        self.segments[seg_id] = path
        self._seg_order.append(seg_id)
        if task_id:
            ids = self._task_segments.setdefault(task_id, [])
            ids.append(seg_id)
            self._task_segments.move_to_end(task_id)
            while len(self._task_segments) > 64:
                self._task_segments.popitem(last=False)
        # Trim bookkeeping (files themselves are cache-managed).
        while len(self.segments) > 400:
            old = self._seg_order.popleft() if self._seg_order else None
            if old:
                self.segments.pop(old, None)
            else:
                break
        return seg_id

    def segment_path(self, seg_id: str) -> Path | None:
        return self.segments.get(seg_id)

    def segments_for_task(self, task_id: str) -> list[str]:
        """Ordered, still-resident segment ids for a response's speech
        task — the exact audio the reply played with, for honest replay."""
        ids = self._task_segments.get(str(task_id or "")) or []
        return [sid for sid in ids if sid in self.segments]

    # -- status / metrics ----------------------------------------------------
    def status(self) -> dict[str, Any]:
        preset = self.current_preset()
        eng = None
        try:
            # Report the engine the active preset actually uses — the
            # config default may differ when a preset pins its own.
            eng = self.engine(preset.engine if preset else "")
        except Exception:
            pass
        with self._lock:
            queue_len = len(self._queue)
            active = ({"task_id": self._active_job.task_id,
                       "seq": self._active_job.seq}
                      if self._active_job else None)
        return {
            "enabled": self.enabled(),
            "muted": self.muted(),
            "mode": self.mode(),
            "preset_id": preset.id if preset else "",
            "preset_name": preset.name if preset else "",
            "engine": eng.status() if eng else {"name": getattr(
                self.config, "voice_engine", "kokoro"), "loaded": False},
            "queue": queue_len,
            "active": active,
            "cache": self.cache.stats(),
            "speed": getattr(self.config, "voice_speed", 1.0),
            "volume": getattr(self.config, "voice_volume", 1.0),
        }

    # -- export ---------------------------------------------------------------
    def export_segment(self, seg_id: str, fmt: str, dest: Path) -> Path:
        """Export a cached segment. WAV = direct copy; MP3 = one final
        encode via ffmpeg (never an intermediate codec)."""
        src = self.segment_path(seg_id)
        if src is None or not src.exists():
            raise VoiceEngineError("unknown or expired segment")
        fmt = fmt.lower()
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if fmt == "wav":
            dest.write_bytes(src.read_bytes())
            return dest
        if fmt == "mp3":
            from localcodeagent.tools.media import find_ffmpeg, _run_ffmpeg
            exe = find_ffmpeg()
            if not exe:
                raise VoiceEngineError(
                    "ffmpeg is not installed — WAV export is always available")
            argv = [exe, "-y", "-i", str(src), "-codec:a", "libmp3lame",
                    "-q:a", "4", str(dest)]
            _run_ffmpeg(argv, dest.parent, timeout=60)
            return dest
        raise VoiceEngineError(f"unsupported export format: {fmt}")


class _CachedAudio:
    """Lightweight stand-in so callers can read shape[0] on cache hits."""
    def __init__(self, frames: int) -> None:
        self.shape = (frames,)


def _apply_pause_hint(text: str, delivery: dict | None) -> str:
    """pause_hint → one real mid-sentence pause. High pause density marks
    the strongest clause boundary (em-dash > semicolon > comma, nearest
    the middle) with an ellipsis the engine renders as an actual beat.
    Speech text only — display text and code/exact spans are never
    touched. At most one insertion per sentence."""
    if not isinstance(delivery, dict):
        return text
    try:
        hint = float(delivery.get("pause_hint") or 0.0)
    except (TypeError, ValueError):
        return text
    if hint < 0.55 or len(text) < 60:
        return text
    # Plain prose only — never disturb code-ish or numeric-dense text.
    if "`" in text or "{" in text or text.count("=") > 2:
        return text
    best = None
    for delim in (" — ", "; ", ", "):
        mid = len(text) / 2
        cands = [m.end() - len(delim) + 1 for m in
                 re.finditer(re.escape(delim), text)]
        # Only boundaries with a real clause on both sides.
        cands = [c for c in cands if c > 20 and len(text) - c > 20]
        if cands:
            best = min(cands, key=lambda c: abs(c - mid))
            break
    if best is None:
        return text
    return text[:best] + " …" + text[best:]


def _lang_tag(language: str) -> str:
    mapping = {"en-GB": "en-gb", "en-US": "en-us", "en-gb": "en-gb",
               "en-us": "en-us"}
    return mapping.get(language, language.lower()[:5] or "en-us")


def _split_sentences(text: str) -> list[str]:
    import re
    parts = re.split(r"(?<=[.!?…])\s+", text.strip())
    return [p.strip() for p in parts if p.strip()]
