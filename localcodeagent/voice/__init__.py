"""Local-first text-to-speech and voice-preset subsystem.

Pipeline: TTS engine (Kokoro by default) → float32 PCM → preset DSP chain
(shaping + parallel synthetic layers + limiter) → WAV bytes / cache /
playback queue. See docs/VOICE_SYSTEM.md.
"""
from .types import VoicePreset, SpeechMode  # noqa: F401
from .presets import VoicePresetStore, OFFICIAL_PRESET_ID  # noqa: F401
