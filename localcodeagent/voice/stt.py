"""Two-way voice: speech-to-text capture + conversational sessions.

Mic capture uses ``sounddevice`` (optional). Transcription backends:
``vosk`` (streaming partials) or ``faster-whisper`` (chunked). When
neither dependency is installed the session reports ``available: false``
instead of pretending.

A ``VoiceSession`` wires capture → STT partials → caller callback
(usually the chat pipeline) → streaming TTS via VoiceManager, with
barge-in (user speech interrupts playback) and latency telemetry.
"""
from __future__ import annotations

import queue
import threading
import time
from pathlib import Path
from typing import Any, Callable

SAMPLE_RATE = 16000


def list_input_devices() -> list[dict[str, Any]]:
    """Enumerate capture devices when sounddevice is installed."""
    try:
        import sounddevice as sd  # type: ignore
        return [{"index": i, "name": d["name"],
                 "channels": d["max_input_channels"],
                 "sample_rate": d["default_samplerate"]}
                for i, d in enumerate(sd.query_devices())
                if d["max_input_channels"] > 0]
    except Exception:
        return []


class SttEngine:
    """Transcription backend interface."""

    name = "stt"
    streaming = False

    def available(self) -> bool:
        return False

    def accept_pcm(self, pcm: bytes) -> str | None:
        """Feed audio; return partial/final text when streaming."""
        return None

    def transcribe_file(self, path: Path) -> dict[str, Any]:
        return {"ok": False, "error": "backend unavailable"}

    def reset(self) -> None:
        pass


class VoskEngine(SttEngine):
    name = "vosk"
    streaming = True

    def __init__(self, model_path: Path | str) -> None:
        self._model_path = str(model_path)
        self._rec = None
        try:
            import vosk  # type: ignore
            self._rec = vosk.KaldiRecognizer(
                vosk.Model(self._model_path), SAMPLE_RATE)
        except Exception:
            self._rec = None

    def available(self) -> bool:
        return self._rec is not None

    def accept_pcm(self, pcm: bytes) -> str | None:
        if self._rec is None:
            return None
        import json
        if self._rec.AcceptWaveform(pcm):
            return json.loads(self._rec.Result()).get("text") or None
        return json.loads(self._rec.PartialResult()).get("partial") or None

    def reset(self) -> None:
        try:
            self._rec.Reset()  # type: ignore
        except Exception:
            pass


class FasterWhisperEngine(SttEngine):
    name = "faster-whisper"
    streaming = False

    def __init__(self, model_size: str = "base") -> None:
        self._model = None
        try:
            from faster_whisper import WhisperModel  # type: ignore
            self._model = WhisperModel(model_size, device="cpu",
                                       compute_type="int8")
        except Exception:
            self._model = None

    def available(self) -> bool:
        return self._model is not None

    def transcribe_file(self, path: Path) -> dict[str, Any]:
        if self._model is None:
            return {"ok": False, "error": "faster-whisper not installed"}
        t0 = time.time()
        segments, info = self._model.transcribe(str(path))
        text = " ".join(s.text for s in segments).strip()
        return {"ok": True, "text": text,
                "language": getattr(info, "language", ""),
                "latency_s": round(time.time() - t0, 3)}


class VoiceSession:
    """mic → STT → respond callback → streaming TTS, with barge-in.

    The session owns a capture thread; `on_utterance(text)` is the
    caller's Nexus response hook. TTS output flows through the existing
    VoiceManager so voice selection/streaming/replay is reused.
    """

    def __init__(self, stt: SttEngine, voice_manager: Any,
                 *, device: int | None = None,
                 on_utterance: Callable[[str], None] | None = None,
                 on_partial: Callable[[str], None] | None = None) -> None:
        self.stt = stt
        self.voice = voice_manager
        self.device = device
        self.on_utterance = on_utterance or (lambda t: None)
        self.on_partial = on_partial or (lambda t: None)
        self._audio_q: queue.Queue[bytes] = queue.Queue(maxsize=200)
        self._running = False
        self._thread: threading.Thread | None = None
        self.metrics = {"utterances": 0, "interruptions": 0,
                        "capture_errors": 0, "last_stt_ms": 0.0}

    def available(self) -> bool:
        return self.stt.available() and bool(list_input_devices())

    def start(self) -> dict[str, Any]:
        if not self.stt.available():
            return {"ok": False, "error": "no STT backend available"}
        if self._running:
            return {"ok": True, "already": True}
        try:
            import sounddevice as sd  # noqa: F401
        except ImportError:
            return {"ok": False, "error": "sounddevice not installed"}
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return {"ok": True, "session": "listening"}

    def _loop(self) -> None:
        import sounddevice as sd

        def cb(indata, frames, _t, status):
            if status:
                self.metrics["capture_errors"] += 1
            try:
                self._audio_q.put_nowait(bytes(indata))
            except queue.Full:
                pass

        try:
            with sd.RawInputStream(samplerate=SAMPLE_RATE, blocksize=4000,
                                   dtype="int16", channels=1,
                                   device=self.device, callback=cb):
                silence = 0
                while self._running:
                    try:
                        pcm = self._audio_q.get(timeout=0.2)
                    except queue.Empty:
                        continue
                    t0 = time.time()
                    text = self.stt.accept_pcm(pcm)
                    self.metrics["last_stt_ms"] = round(
                        (time.time() - t0) * 1000, 1)
                    if not text:
                        continue
                    # Barge-in: user speaking kills current playback.
                    if getattr(self.voice, "is_playing", lambda: False)():
                        self.voice.stop_all("barge-in")
                        self.metrics["interruptions"] += 1
                    if self.stt.streaming and len(text) < 200:
                        self.on_partial(text)
                    else:
                        self.metrics["utterances"] += 1
                        self.on_utterance(text)
        except Exception:
            self._running = False

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None

    def status(self) -> dict[str, Any]:
        return {"running": self._running,
                "stt_backend": self.stt.name,
                "stt_available": self.stt.available(),
                "streaming": self.stt.streaming,
                "device": self.device,
                "metrics": dict(self.metrics)}
