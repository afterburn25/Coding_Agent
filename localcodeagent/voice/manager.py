"""VoiceManager — single point for synthesis, queueing, playback state,
muting and metrics. Frontend plays served WAVs; the manager tracks response
and segment ids so stale/dup speech is suppressed across reconnects.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Callable

from . import dsp
from .cache import AudioCache
from .engine import VoiceEngineError, get_engine
from .presets import VoicePresetStore
from .speech_filter import SpeechTextFilter
from .streamer import SentenceStreamer, split_for_speech
from .types import VoicePreset


class SpeechJob:
    __slots__ = ("job_id", "task_id", "seq", "text", "preset_id", "speed",
                 "cancelled", "created_at")

    def __init__(self, task_id: str, seq: int, text: str, preset_id: str,
                 speed: float) -> None:
        self.job_id = uuid.uuid4().hex[:16]
        self.task_id = task_id
        self.seq = seq
        self.text = text
        self.preset_id = preset_id
        self.speed = speed
        self.cancelled = False
        self.created_at = time.time()


class VoiceManager:
    """Owns engines, presets, cache, the synthesis queue and spoken state."""

    def __init__(self, config, preset_dir: Path, cache_dir: Path,
                 publish: Callable[[str, dict], None] | None = None,
                 asset_dir: Path | None = None,
                 persist: Callable[[], None] | None = None) -> None:
        self.config = config
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
        self._engines: dict[str, Any] = {}
        self._muted_at = 0.0
        self.segments: dict[str, Path] = {}  # seg_id -> wav path (recent)
        self._seg_order: deque[str] = deque(maxlen=200)

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
            self._engines[name] = eng
        return self._engines[name]

    # -- mute / playback control ------------------------------------------
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

    def begin_task(self, task_id: str) -> None:
        """A new user request / response starts: drop stale speech."""
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
        # Pre-warm the TTS engine while the text response streams in, so the
        # first emitted sentence doesn't pay the ~1s model-load cost after an
        # idle unload. load() is idempotent and lock-guarded.
        if (self.enabled() and not self.muted()
                and self.mode() in {"responses", "responses_activity"}):
            threading.Thread(target=self._warm_engine,
                             name="nexus-voice-warm", daemon=True).start()

    def _warm_engine(self) -> None:
        try:
            self.engine().load()
        except Exception:
            pass

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
        for sent in streamer.feed(delta):
            self.enqueue(task_id, sent)
            n += 1
        return n

    def finish_task(self, task_id: str, final_text: str | None = None) -> None:
        """Flush the streamer tail; if nothing was emitted (non-streamed or
        fully skipped), fall back to a filtered one-shot of final_text."""
        streamer = self._streamers.pop(task_id, None)
        if not self.enabled() or self.muted():
            return
        n_emitted = streamer.emitted_count if streamer else 0
        if streamer:
            for sent in streamer.flush():
                self.enqueue(task_id, sent)
        if n_emitted == 0 and final_text and self.mode() in {
                "responses", "responses_activity"}:
            spoken = self.filter.filter(final_text)
            if spoken:
                for sent in _split_sentences(spoken):
                    for part in split_for_speech(sent):
                        self.enqueue(task_id, part)

    # -- queue --------------------------------------------------------------
    def enqueue(self, task_id: str, text: str, *,
                preset_id: str | None = None, speed: float = 1.0,
                priority: bool = False) -> SpeechJob | None:
        if not self.enabled() or self.muted():
            return None
        text = text.strip()
        if not text:
            return None
        with self._lock:
            seq = self._spoken_tasks.get(task_id, 0)
            self._spoken_tasks[task_id] = seq + 1
            job = SpeechJob(task_id, seq, text,
                            preset_id or (self.current_preset().id
                                          if self.current_preset() else ""),
                            speed)
            if priority:
                self._queue.appendleft(job)
            else:
                self._queue.append(job)
            self._ensure_worker()
        self._publish("voice", {"event": "queued", "task_id": task_id,
                                "seq": seq, "job_id": job.job_id})
        return job

    def speak_text(self, text: str, *, preset_id: str | None = None,
                   speed: float = 1.0, auto_filter: bool = True,
                   task_id: str = "") -> dict[str, Any]:
        """Direct (non-queued) speak: filter → synth → returns audio id.
        Used by per-message replay, preview and tools."""
        if not self.enabled():
            raise VoiceEngineError("voice subsystem is disabled")
        preset = self.presets.get(preset_id) if preset_id else self.current_preset()
        if preset is None:
            raise VoiceEngineError("no voice preset configured")
        spoken = self.filter.filter(text) if auto_filter else text.strip()
        if not spoken:
            raise VoiceEngineError("nothing speakable in the provided text")
        pcm, sr, seg = self._synthesize(spoken, preset, speed)
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
            preset = self.presets.get(job.preset_id) or self.current_preset()
            if preset is None:
                continue
            try:
                pcm, sr, seg_path = self._synthesize(job.text, preset, job.speed)
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
            self._publish("voice", {
                "event": "segment", "task_id": job.task_id, "seq": job.seq,
                "segment_id": seg_id, "url": f"/api/voice/audio/{seg_id}",
                "seconds": round(pcm.shape[0] / sr, 2),
            })

    def _synthesize(self, text: str, preset: VoicePreset, speed: float):
        """Full pipeline → stereo WAV on disk. Returns (pcm, sr, path)."""
        engine = self.engine(preset.engine)
        preset_json = preset.to_json()
        key = AudioCache.key(text, preset.engine, engine.version,
                             preset.base_voice,
                             AudioCache.preset_hash(preset_json), speed)
        hit = self.cache.get(key)
        if hit is not None:
            # Decode header for reported duration (avoid re-encode).
            import wave
            with wave.open(str(hit), "rb") as w:
                frames, sr = w.getnframes(), w.getframerate()
            pcm = Path(hit).read_bytes()  # placeholder shape for caller
            return _CachedAudio(frames), sr, hit
        audio, sr = engine.synthesize(text, voice=preset.base_voice,
                                      speed=speed,
                                      lang=_lang_tag(preset.language))
        stereo = dsp.process(audio, sr, preset)
        wav = dsp.wav_bytes(stereo, sr)
        path = self.cache.put(key, wav)
        return stereo, sr, path

    def _register_segment(self, path: Path, task_id: str) -> str:
        seg_id = uuid.uuid4().hex[:16]
        self.segments[seg_id] = path
        self._seg_order.append(seg_id)
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

    # -- status / metrics ----------------------------------------------------
    def status(self) -> dict[str, Any]:
        preset = self.current_preset()
        eng = None
        try:
            eng = self.engine()
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


def _lang_tag(language: str) -> str:
    mapping = {"en-GB": "en-gb", "en-US": "en-us", "en-gb": "en-gb",
               "en-us": "en-us"}
    return mapping.get(language, language.lower()[:5] or "en-us")


def _split_sentences(text: str) -> list[str]:
    import re
    parts = re.split(r"(?<=[.!?…])\s+", text.strip())
    return [p.strip() for p in parts if p.strip()]
