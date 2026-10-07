"""Chatterbox Turbo provider — drives an isolated runtime subprocess.

Torch + chatterbox-tts are far too heavy to freeze into the backend, so
the engine spawns a persistent worker (``chatterbox_worker.py``) under
the bundled venv at ``runtime/voice/chatterbox`` and talks JSONL over
its stdio. The rest of Nexus Core sees only the TTSEngine interface —
same as Kokoro.

Layout:
    runtime/voice/chatterbox/     isolated venv (torch+CUDA, chatterbox-tts)
    models/voice/chatterbox/      turbo model snapshot (sha-verified)
    models/voice/chatterbox/voices/<id>/voice.json + reference.wav

Failure policy: a missing venv/model or a dead worker raises
VoiceEngineError — VoiceManager's queue reports it and keeps chat alive.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import wave
from pathlib import Path
from typing import Any

import numpy as np

from .engine import TTSEngine, VoiceEngineError

log = logging.getLogger(__name__)

_WORKER = Path(__file__).resolve().parent / "chatterbox_worker.py"

# GPU policy: Turbo wants ~2.9 GB; stay clear unless this much is free so
# the LLM/image lanes keep their VRAM headroom.
DEFAULT_MIN_FREE_VRAM_MB = 3200
# Cold load ~16 s; steady-state synthesis is sub-realtime on CUDA but
# slow on CPU — bound a single utterance request.
SYNTH_TIMEOUT_S = 240.0
LOAD_TIMEOUT_S = 180.0


def _venv_python(runtime_dir: Path) -> Path:
    """Worker interpreter — provisioned standalone (python.exe at root)
    or a dev-built venv (Scripts/python.exe)."""
    from .chatterbox_runtime import runtime_python
    return runtime_python(runtime_dir) or (
        runtime_dir / "Scripts" / "python.exe"
        if os.name == "nt" else runtime_dir / "bin" / "python")


def _read_wav_mono(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        n = w.getnframes()
        ch = w.getnchannels()
        raw = w.readframes(n)
    pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        pcm = pcm.reshape(-1, ch).mean(axis=1)
    return np.ascontiguousarray(pcm), sr


class _WorkerPump:
    """Background drains for a worker's stdout/stderr. stdout carries the
    JSONL protocol; stderr carries tqdm/warnings/diagnostics which we log
    at debug level — keeping the two streams apart means worker noise can
    never corrupt a response line."""

    def __init__(self, proc: subprocess.Popen) -> None:
        self.q: queue.Queue[str] = queue.Queue()
        self.proc = proc
        self._out = threading.Thread(target=self._pump_out, daemon=True)
        self._err = threading.Thread(target=self._pump_err, daemon=True)
        self._out.start()
        self._err.start()

    def _pump_out(self) -> None:
        try:
            assert self.proc.stdout is not None
            for ln in self.proc.stdout:
                self.q.put(ln)
        except Exception:
            pass

    def _pump_err(self) -> None:
        try:
            assert self.proc.stderr is not None
            for ln in self.proc.stderr:
                ln = ln.rstrip()
                if ln:
                    log.debug("chatterbox worker: %s", ln)
        except Exception:
            pass

    def readline(self, timeout: float) -> str | None:
        try:
            return self.q.get(timeout=max(0.05, timeout))
        except queue.Empty:
            return None


class ChatterboxEngine(TTSEngine):
    """Chatterbox Turbo behind a subprocess worker. Lazy-spawned on the
    first request (or explicit load()); ``unload()`` kills the worker and
    returns its VRAM/RAM to the machine."""

    name = "chatterbox"
    version = "0.1.7/turbo-749d1c1"
    sample_rate = 24000

    def __init__(self, asset_dir: Path | None = None,
                 runtime_dir: Path | None = None,
                 device: str = "auto",
                 min_free_vram_mb: float = DEFAULT_MIN_FREE_VRAM_MB) -> None:
        self.asset_dir = Path(asset_dir) if asset_dir else Path("models/voice")
        self.runtime_dir = (Path(runtime_dir) if runtime_dir
                            else Path("runtime/voice/chatterbox"))
        self.device_pref = device          # auto | cpu | cuda
        self.dtype_pref = "bf16"           # bf16 halves T3's 1.9 GB; s3gen/ve stay fp32
        self.min_free_vram_mb = float(min_free_vram_mb)
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._loaded = False
        self._last_used = 0.0
        self._device = ""
        self._load_time_s = 0.0
        self._sr = self.sample_rate
        self._prepared: set[str] = set()   # voice_ids with conds prepared
        self._tmp_dir: Path | None = None
        self._last_status: dict[str, Any] = {}
        self._reader: _WorkerPump | None = None
        self.synth_timeout_s = SYNTH_TIMEOUT_S
        self.supported_tags: frozenset[str] | None = None

    # -- paths -------------------------------------------------------------
    @property
    def model_dir(self) -> Path:
        return self.asset_dir / "chatterbox"

    @property
    def voices_dir(self) -> Path:
        """User-installed cloned voices live next to the model."""
        return self.model_dir / "voices"

    @property
    def official_voices_dir(self) -> Path:
        """Shipped canonical voices (Isabella) — packaged with the app,
        like official presets."""
        return Path(__file__).resolve().parent / "chatterbox_voices"

    def voice_dir(self, voice_id: str) -> Path:
        vid = str(voice_id or "isabella")
        user = self.voices_dir / vid
        if user.exists():
            return user
        return self.official_voices_dir / vid

    def voice_meta(self, voice_id: str) -> dict[str, Any]:
        meta_path = self.voice_dir(voice_id) / "voice.json"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
        return meta

    # -- voice import ----------------------------------------------------
    #
    # Chatterbox is zero-shot — "training" a voice is registering a
    # validated reference clip; no fine-tuning exists to wire up.

    _IMPORT_ID_RE = re.compile(r"[^a-z0-9_-]+")

    def _to_wav(self, src: Path, tmp_dir: Path) -> Path:
        """Non-WAV sources decode through ffmpeg (a provisioned tool);
        WAV passes through untouched."""
        if src.suffix.lower() == ".wav":
            return src
        ffmpeg = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
        if not ffmpeg:
            raise VoiceEngineError(
                f"{src.suffix or 'that file'} needs conversion to WAV — "
                "install ffmpeg (provisioning plan) or supply a .wav")
        tmp_dir.mkdir(parents=True, exist_ok=True)
        out = tmp_dir / f"import-{src.stem[:40]}.wav"
        proc = subprocess.run(
            [ffmpeg, "-y", "-i", str(src), "-ac", "1", "-ar", "24000",
             "-f", "wav", str(out)],
            capture_output=True, text=True, timeout=120)
        if proc.returncode != 0 or not out.exists():
            raise VoiceEngineError(
                "ffmpeg could not decode the reference audio: "
                + (proc.stderr or "")[-200:])
        return out

    def import_voice(self, voice_id: str, source_path: str | Path, *,
                     name: str = "", description: str = "",
                     language: str = "en", gender: str = "",
                     overwrite: bool = False) -> dict[str, Any]:
        """Register a cloned voice from a reference audio file.

        Validates the clip (duration/clipping/silence), canonicalizes it
        (mono 24 kHz, loudness-normalized, peak-safe) into the user
        voices dir, and writes a voice.json. Originals are never
        modified and a half-finished import never leaves a voice dir."""
        from .reference import prepare_reference, validate_reference

        vid = self._IMPORT_ID_RE.sub("-", str(voice_id or "").lower()) \
            .strip("-_")
        if not vid:
            return {"ok": False, "error": "voice_id is required "
                    "(letters, digits, - and _)"}
        dest = self.voices_dir / vid
        if dest.exists() and not overwrite:
            return {"ok": False, "error":
                    f"voice '{vid}' already exists — pass overwrite "
                    "to replace it"}
        src = Path(str(source_path or "").strip())
        if not src.is_file():
            return {"ok": False, "error": f"source audio not found: {src}"}
        tmp = dest.parent / f".importing-{vid}"
        try:
            wav = self._to_wav(src, tmp)
            rep = validate_reference(wav)
            if not rep.ok:
                return {"ok": False,
                        "error": "reference failed validation: "
                                 + "; ".join(rep.errors),
                        "report": rep.as_dict()}
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)
            dest.mkdir(parents=True, exist_ok=True)
            prepared, rep = prepare_reference(wav, dest)
            ref = dest / "reference.wav"
            if prepared.name != ref.name:
                if ref.exists():
                    ref.unlink()
                prepared.replace(ref)
            meta = {
                "id": vid,
                "name": str(name or vid.replace("-", " ").title()),
                "engine": "chatterbox",
                "language": str(language or "en"),
                "gender": str(gender or ""),
                "description": str(description or
                                   "Imported cloned voice"),
                "reference": "reference.wav",
                "reference_sha256": rep.sha256,
                "provenance": f"imported from {src.name}",
                "exaggeration": 0.5, "temperature": 0.72,
                "top_p": 0.95, "top_k": 1000,
                "repetition_penalty": 1.2, "normalization": True,
            }
            (dest / "voice.json").write_text(
                json.dumps(meta, indent=2), encoding="utf-8")
            return {"ok": True, "voice": meta,
                    "warnings": rep.warnings}
        except VoiceEngineError as exc:
            return {"ok": False, "error": str(exc)}
        except (ValueError, OSError) as exc:
            return {"ok": False, "error": str(exc)[:300]}
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # -- availability --------------------------------------------------------
    def available(self) -> bool:
        if not _venv_python(self.runtime_dir).exists():
            return False
        required = ("ve.safetensors", "t3_turbo_v1.safetensors",
                    "s3gen_meanflow.safetensors", "conds.pt",
                    "tokenizer_config.json", "vocab.json", "merges.txt")
        return all((self.model_dir / f).exists() for f in required)

    # -- worker lifecycle ---------------------------------------------------
    def _spawn(self) -> None:
        py = _venv_python(self.runtime_dir)
        if not py.exists():
            raise VoiceEngineError(
                f"Chatterbox runtime not found at {self.runtime_dir} — "
                "run voice setup/provisioning")
        if not _WORKER.exists():
            # Frozen builds ship the worker via --add-data; a missing file
            # here means packaging dropped it — fail loudly, not with a
            # dead-process mystery two calls later.
            raise VoiceEngineError(
                f"Chatterbox worker script missing at {_WORKER}")
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env.setdefault("HF_HUB_OFFLINE", "1")
        env.setdefault("TRANSFORMERS_OFFLINE", "1")
        if self._tmp_dir is not None:
            try:
                import shutil
                shutil.rmtree(self._tmp_dir, ignore_errors=True)
            except Exception:
                pass
        self._tmp_dir = Path(tempfile.mkdtemp(prefix="nexus-cb-"))
        try:
            self._proc = subprocess.Popen(
                [str(py), "-u", "-P", str(_WORKER)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True,
                encoding="utf-8", errors="replace",
                env=env,
                creationflags=(subprocess.CREATE_NO_WINDOW
                               if os.name == "nt" else 0))
        except Exception as exc:
            raise VoiceEngineError(
                f"Chatterbox worker failed to spawn: {exc}") from exc
        self._reader = _WorkerPump(self._proc)

    def _request(self, payload: dict, timeout: float, *,
                 touch: bool = False) -> dict:
        """One request → one response line. A dead/misbehaving worker is
        killed so the next call respawns cleanly."""
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                self._spawn()
            proc = self._proc
            reader = self._reader
            assert proc is not None and proc.stdin and reader is not None
            try:
                proc.stdin.write(json.dumps(payload) + "\n")
                proc.stdin.flush()
            except (OSError, ValueError) as exc:
                self._kill_locked()
                raise VoiceEngineError(
                    f"Chatterbox worker write failed: {exc}") from exc

            deadline = time.monotonic() + timeout
            while True:
                line = reader.readline(deadline - time.monotonic())
                if line is None or line == "":
                    if proc.poll() is not None:
                        self._kill_locked()
                        raise VoiceEngineError(
                            "Chatterbox worker exited unexpectedly")
                    if time.monotonic() >= deadline:
                        self._kill_locked()
                        raise VoiceEngineError(
                            f"Chatterbox worker timed out after {timeout:.0f}s")
                    continue
                line = line.strip()
                if not line.startswith("{"):
                    # stdout stays protocol-clean by design; skip any
                    # stray non-JSON line defensively rather than failing.
                    continue
                try:
                    resp = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if resp.get("ok"):
                    # Only real speech work extends the idle-unload lease —
                    # introspection (status/capabilities) must not reset the
                    # timer or UI status polls would pin ~2 GB of VRAM
                    # forever.
                    if touch:
                        self._last_used = time.time()
                    return resp
                raise VoiceEngineError(str(resp.get("error") or
                                           "chatterbox request failed"))

    @staticmethod
    def _close_pipes(proc: subprocess.Popen) -> None:
        # kill()/wait() leave the parent's stdin/stdout/stderr wrappers
        # open — close them so teardown doesn't leak fds (the pump threads
        # treat the resulting ValueError as end-of-stream and exit).
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if pipe is not None:
                    pipe.close()
            except Exception:
                pass

    def _kill_locked(self) -> None:
        proc, self._proc = self._proc, None
        self._reader = None
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
            self._close_pipes(proc)
        self._loaded = False
        self._prepared.clear()

    # -- TTSEngine -----------------------------------------------------------
    def load(self) -> None:
        if self._loaded:
            return
        if not self.available():
            raise VoiceEngineError(
                "Chatterbox runtime or model assets missing — "
                "run voice setup/provisioning")
        resp = self._request(
            {"cmd": "load", "device": self.device_pref,
             "model_dir": str(self.model_dir), "dtype": self.dtype_pref,
             "min_free_vram_mb": self.min_free_vram_mb},
            timeout=LOAD_TIMEOUT_S)
        self._loaded = True
        self._last_used = time.time()
        self._device = str(resp.get("device") or "cpu")
        self._load_time_s = float(resp.get("load_s") or 0.0)
        self._sr = int(resp.get("sr") or self.sample_rate)
        self._last_status = resp
        tags = resp.get("tags")
        if isinstance(tags, list):
            self.supported_tags = frozenset(str(t) for t in tags)

    def unload(self) -> None:
        with self._lock:
            proc = self._proc
            if proc is not None and proc.poll() is None:
                try:
                    proc.stdin.write(json.dumps({"cmd": "shutdown"}) + "\n")
                    proc.stdin.flush()
                    proc.wait(timeout=8)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            if proc is not None:
                self._close_pipes(proc)
            self._proc = None
            self._reader = None
            self._loaded = False
            self._prepared.clear()
        if self._tmp_dir is not None:
            try:
                import shutil
                shutil.rmtree(self._tmp_dir, ignore_errors=True)
            except Exception:
                pass
            self._tmp_dir = None

    def _prepare(self, voice_id: str) -> dict[str, Any]:
        meta = self.voice_meta(voice_id)
        if voice_id in self._prepared:
            return meta
        ref = meta.get("reference") or "reference.wav"
        ref_path = self.voice_dir(voice_id) / ref
        if not ref_path.exists():
            raise VoiceEngineError(
                f"Chatterbox voice {voice_id!r} has no reference audio at "
                f"{ref_path} — run voice reference provisioning")
        self._request({"cmd": "prepare_voice", "voice_id": voice_id,
                       "reference": str(ref_path),
                       "exaggeration": float(meta.get("exaggeration", 0.5)),
                       "norm_loudness": bool(meta.get("normalization", True))},
                      timeout=120.0, touch=True)
        self._prepared.add(voice_id)
        return meta

    def synthesize(self, text: str, *, voice: str = "isabella",
                   speed: float = 1.0, lang: str = "en-gb"):
        self.load()
        voice_id = str(voice or "isabella")
        meta = self._prepare(voice_id)
        out = (self._tmp_dir or Path(tempfile.gettempdir())) / \
            f"cb-{uuid.uuid4().hex[:12]}.wav"
        resp = self._request({
            "cmd": "synthesize", "text": text, "voice_id": voice_id,
            "temperature": float(meta.get("temperature", 0.72)),
            "top_p": float(meta.get("top_p", 0.95)),
            "top_k": int(meta.get("top_k", 1000)),
            "repetition_penalty": float(meta.get("repetition_penalty", 1.2)),
            "out": str(out)}, timeout=self.synth_timeout_s, touch=True)
        pcm, sr = _read_wav_mono(out)
        try:
            out.unlink(missing_ok=True)
        except OSError:
            pass
        from . import dsp
        # Job speed maps to tempo (pitch preserved) — Chatterbox has no
        # native rate control.
        if abs(speed - 1.0) > 0.02:
            pcm = dsp.pitch_tempo(pcm, sr, 0.0,
                                  max(0.5, min(2.0, speed)),
                                  formant_preserve=0.8)
        pcm = dsp.trim_tail_artifact(pcm, sr)
        self._last_status = resp
        return pcm, sr

    def synthesize_stream(self, text: str, *, voice: str = "isabella",
                          speed: float = 1.0, lang: str = "en-gb"):
        """Turbo has no streaming API — yield the single utterance; the
        manager's sentence splitting already pipelines playback."""
        yield self.synthesize(text, voice=voice, speed=speed, lang=lang)

    def capabilities(self) -> dict[str, Any]:
        """Probe the live tokenizer: which paralinguistic/emotion tags the
        shipped runtime actually encodes as dedicated tokens."""
        self.load()
        return self._request({"cmd": "capabilities"}, timeout=30.0)

    def voices(self) -> list[dict[str, Any]]:
        out: dict[str, dict] = {}
        # User-installed voices shadow official ones — same preference
        # voice_dir() applies at synthesis time, so the listing shows
        # the metadata actually in effect.
        for root, official in ((self.voices_dir, False),
                               (self.official_voices_dir, True)):
            try:
                for d in sorted(root.iterdir()):
                    if not d.is_dir() or d.name in out:
                        continue
                    meta = self.voice_meta(d.name)
                    ref = d / str(meta.get("reference") or "reference.wav")
                    out[d.name] = {
                        "id": d.name,
                        "label": meta.get("name") or d.name.title(),
                        "lang": meta.get("language") or "en-GB",
                        "gender": meta.get("gender") or "female",
                        "installed": ref.exists(),
                        "official": official,
                        "description": meta.get("description") or "",
                    }
            except OSError:
                pass
        if not out:
            out["isabella"] = {"id": "isabella", "label": "Isabella",
                               "lang": "en-GB", "gender": "female",
                               "installed": False}
        return list(out.values())

    def status(self) -> dict[str, Any]:
        st: dict[str, Any] = {
            "name": self.name,
            "version": self.version,
            "loaded": self._loaded,
            "worker_alive": bool(self._proc and self._proc.poll() is None),
            "device": self._device,
            "device_pref": self.device_pref,
            "load_time_s": round(self._load_time_s, 3),
            "runtime_dir": str(self.runtime_dir),
            "model_dir": str(self.model_dir),
            "available": self.available(),
            "voices": self.voices(),
            "supported_tags": (sorted(self.supported_tags)
                               if self.supported_tags else []),
            "model": "ResembleAI/chatterbox-turbo",
            "model_revision": "749d1c1a",
        }
        if self._loaded and self._proc and self._proc.poll() is None:
            try:
                st.update(self._request({"cmd": "status"}, timeout=10.0))
            except VoiceEngineError:
                st["worker_alive"] = False
        return st
