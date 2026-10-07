"""TTS provider abstraction. The rest of Nexus Core only sees this.

    engine = registry.get("kokoro")
    pcm, sr = engine.synthesize("Hello.", voice="bf_isabella", speed=1.0)

Providers are lazily loaded, CPU-first, and report readiness via status().
Adding Piper or another engine means implementing this interface — nothing
else in the app imports provider internals.
"""
from __future__ import annotations

from typing import Any, Callable


class VoiceEngineError(RuntimeError):
    """Engine failed to load or synthesize — recoverable; chat must survive."""


class TTSEngine:
    """Provider contract. Implementations: KokoroEngine, future Piper, ..."""

    name: str = "base"
    version: str = "0"
    sample_rate: int = 24000

    def available(self) -> bool:
        """True when model assets + dependencies are present."""
        raise NotImplementedError

    def load(self) -> None:
        """Warm the model into memory. Idempotent."""
        raise NotImplementedError

    def unload(self) -> None:
        """Release model memory when idle — keeps overnight sessions lean."""
        raise NotImplementedError

    def synthesize(self, text: str, *, voice: str, speed: float = 1.0,
                   lang: str = "en-us"):
        """Return (float32 mono PCM np.ndarray, sample_rate)."""
        raise NotImplementedError

    def voices(self) -> list[dict[str, Any]]:
        """Installed base voices: [{id, label, lang, gender}]."""
        raise NotImplementedError

    def status(self) -> dict[str, Any]:
        raise NotImplementedError


_ENGINES: dict[str, Callable[[], TTSEngine]] = {}


def register_engine(name: str, factory: Callable[[], TTSEngine]) -> None:
    _ENGINES[name] = factory


def get_engine(name: str) -> TTSEngine:
    factory = _ENGINES.get(name)
    if factory is None:
        raise VoiceEngineError(f"unknown TTS engine: {name!r}")
    return factory()


def engine_names() -> list[str]:
    return sorted(_ENGINES)


def _bootstrap() -> None:
    try:
        from .kokoro import KokoroEngine
        register_engine("kokoro", KokoroEngine)
    except Exception:
        pass
    try:
        from .chatterbox import ChatterboxEngine
        register_engine("chatterbox", ChatterboxEngine)
    except Exception:
        pass


_bootstrap()
