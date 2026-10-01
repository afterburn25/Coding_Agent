"""Kokoro provider — wraps kokoro_onnx behind the TTSEngine interface.

Model: Kokoro-82M (Apache-2.0), ONNX build distributed with kokoro-onnx
0.6.1. Runs locally on ONNX Runtime; CPU is the default policy so voice
never competes with coding models for VRAM. 24 kHz float32 output.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

from . import assets
from .engine import TTSEngine, VoiceEngineError

DEFAULT_BASE_VOICE = "bf_isabella"


def _default_asset_dir() -> Path:
    # Models next to the install root (models/voice); VoiceManager passes the
    # configured voice_assets_dir explicitly, so this is only a fallback for
    # standalone engine use.
    return Path("models/voice")


class KokoroEngine(TTSEngine):
    name = "kokoro"
    version = "0.6.1/kokoro-82m-v1.0"
    sample_rate = 24000

    def __init__(self, asset_dir: Path | None = None) -> None:
        self.asset_dir = Path(asset_dir) if asset_dir else _default_asset_dir()
        self._model = None
        self._lock = threading.Lock()
        self._loaded_at = 0.0
        self._load_time_s = 0.0
        self._synth_calls = 0
        self._synth_audio_s = 0.0
        self._synth_cpu_s = 0.0

    # -- lifecycle ------------------------------------------------------
    def available(self) -> bool:
        st = assets.asset_status(self.asset_dir)
        return all(v["present"] and v["verified"] for v in st.values())

    def load(self) -> None:
        with self._lock:
            if self._model is not None:
                return
            t0 = time.monotonic()
            try:
                from kokoro_onnx import Kokoro  # lazy import
            except Exception as exc:
                raise VoiceEngineError(
                    f"kokoro-onnx is not installed ({exc}); install the voice "
                    "runtime from the Voice page") from exc
            missing = [n for n, s in assets.asset_status(self.asset_dir).items()
                       if not s["verified"]]
            if missing:
                raise VoiceEngineError(
                    f"voice assets missing or unverified: {', '.join(missing)} — "
                    "run Voice setup to download them")
            try:
                self._model = Kokoro(
                    str(self.asset_dir / "kokoro-v1.0.onnx"),
                    str(self.asset_dir / "voices-v1.0.bin"),
                )
            except Exception as exc:
                raise VoiceEngineError(f"Kokoro failed to load: {exc}") from exc
            self._load_time_s = time.monotonic() - t0
            self._loaded_at = time.time()

    def unload(self) -> None:
        with self._lock:
            self._model = None
            self._loaded_at = 0.0

    # -- synthesis --------------------------------------------------------
    def synthesize(self, text: str, *, voice: str = DEFAULT_BASE_VOICE,
                   speed: float = 1.0, lang: str = "en-gb"):
        self.load()
        text = text.strip()
        if not text:
            raise VoiceEngineError("empty text")
        t0 = time.monotonic()
        try:
            audio, sr = self._model.create(text, voice=voice, speed=speed,
                                           lang=lang)
        except Exception as exc:
            raise VoiceEngineError(f"Kokoro synthesis failed: {exc}") from exc
        dt = time.monotonic() - t0
        audio = np.asarray(audio, dtype=np.float32)
        self._synth_calls += 1
        self._synth_audio_s += audio.size / sr
        self._synth_cpu_s += dt
        return audio, sr

    def synthesize_stream(self, text: str, *, voice: str = DEFAULT_BASE_VOICE,
                          speed: float = 1.0, lang: str = "en-gb"):
        """Yield (pcm, sr) per Kokoro sentence chunk — used for streaming
        playback. Falls back to single-shot for short text."""
        self.load()
        try:
            stream = self._model.create_stream(text, voice=voice, speed=speed,
                                               lang=lang)
        except Exception as exc:
            raise VoiceEngineError(f"Kokoro stream failed: {exc}") from exc
        import asyncio

        async def _collect():
            out = []
            async for chunk in stream:
                out.append(chunk)
            return out

        chunks = asyncio.run(_collect()) if not isinstance(stream, list) else stream
        for audio, sr in chunks:
            yield np.asarray(audio, dtype=np.float32), sr

    # -- info ---------------------------------------------------------------
    def voices(self) -> list[dict[str, Any]]:
        try:
            self.load()
        except VoiceEngineError:
            return [{"id": DEFAULT_BASE_VOICE, "label": "Isabella",
                     "lang": "en-GB", "gender": "female", "installed": False}]
        out = []
        for name in sorted(self._model.voices):
            out.append({
                "id": name,
                "label": name.split("_", 1)[-1].replace("_", " ").title(),
                "lang": _voice_lang(name),
                "gender": "female" if name[1] == "f" else "male",
                "installed": True,
            })
        return out

    def status(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "loaded": self._model is not None,
            "load_time_s": round(self._load_time_s, 3),
            "asset_dir": str(self.asset_dir),
            "assets": assets.asset_status(self.asset_dir),
            "synth_calls": self._synth_calls,
            "synth_audio_s": round(self._synth_audio_s, 2),
            "synth_cpu_s": round(self._synth_cpu_s, 2),
            "rtf": round(self._synth_cpu_s / self._synth_audio_s, 3)
            if self._synth_audio_s else None,
            "upstream": assets.UPSTREAM,
        }


_LANG_PREFIX = {"a": "en-US", "b": "en-GB", "e": "es", "f": "fr",
                "h": "hi", "i": "it", "j": "ja", "p": "pt-BR", "z": "zh"}


def _voice_lang(voice_id: str) -> str:
    return _LANG_PREFIX.get(voice_id[:1], "")
