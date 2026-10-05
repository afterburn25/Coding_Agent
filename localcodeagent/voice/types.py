"""Serializable voice-preset schema (version 1).

A preset is a nondestructive DSP recipe on top of a TTS engine + base
voice — never a retrained model. All fields carry safe defaults so old
presets keep loading when the schema grows.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

SCHEMA_VERSION = 1

PREVIEW_PHRASE = "Nexus Core online. I've completed the analysis and everything is ready."


@dataclass(slots=True)
class EQBand:
    freq_hz: float
    gain_db: float
    q: float = 1.0


@dataclass(slots=True)
class CompressorSpec:
    threshold_db: float = -18.0
    ratio: float = 3.0
    attack_ms: float = 5.0
    release_ms: float = 80.0
    makeup_db: float = 1.5


@dataclass(slots=True)
class NeuralLayer:
    enabled: bool = True
    hpf_hz: float = 700.0
    lpf_hz: float = 6900.0
    bit_depth: float = 9.0
    wet: float = 0.78
    anti_alias: float = 0.58
    sample_reduction: float = 1.55
    delay_ms: float = 1.8
    decay: float = 0.28
    mod_rate_hz: float = 0.48
    am_rate_hz: float = 38.0
    am_depth: float = 0.16
    mix: float = 0.24


@dataclass(slots=True)
class GlassLayer:
    enabled: bool = True
    pitch_factor: float = 1.245
    formant_preserve: float = 0.8
    hpf_hz: float = 1900.0
    lpf_hz: float = 9200.0
    delay_ms: float = 8.0
    echo: float = 0.10
    mix: float = 0.085


@dataclass(slots=True)
class MicroLayer:
    enabled: bool = True
    pitch_factor: float = 0.965
    formant_preserve: float = 0.8
    hpf_hz: float = 900.0
    lpf_hz: float = 4800.0
    bit_depth: float = 12.0
    wet: float = 0.30
    delay_ms: float = 15.0
    mix: float = 0.06


@dataclass(slots=True)
class VoicePreset:
    id: str
    name: str
    engine: str = "kokoro"
    base_voice: str = "bf_isabella"
    language: str = "en-GB"
    description: str = ""
    schema_version: int = SCHEMA_VERSION
    official: bool = False

    # Voice character
    pitch_semitones: float = 0.0
    tempo: float = 1.0
    formant_preserve: float = 0.4  # 0 = formants move with pitch, 1 = locked
    eq: list[EQBand] = field(default_factory=list)
    exciter: float = 0.0          # 0..1 harmonic-exciter amount
    compression: CompressorSpec = field(default_factory=CompressorSpec)

    # Synthetic parallel layers
    neural: NeuralLayer = field(default_factory=NeuralLayer)
    glass: GlassLayer = field(default_factory=GlassLayer)
    micro: MicroLayer = field(default_factory=MicroLayer)
    stereo_width: float = 1.0     # 1 = normal, 1.5 = V6 target

    # Natural<->Synthetic master blend: 0 = raw base voice, 1 = full preset
    synthetic: float = 0.8

    # Identity + delivery metadata — descriptive fields surfaced by the
    # API/UI and readable by prompts. pronunciation_overrides is the one
    # field that actively rewrites tokens pre-synthesis (same expansion
    # style as the speech filter's acronym table, authored per preset).
    provenance: str = ""          # e.g. "kokoro:bf_isabella + neural/glass/micro"
    version: str = "1.0"
    cadence: str = ""             # e.g. "measured", "brisk"
    energy: str = ""              # e.g. "calm", "bright"
    warmth: str = ""              # e.g. "warm", "cool"
    formality: str = ""           # e.g. "conversational", "formal"
    emotion_range: str = ""       # e.g. "steady", "expressive"
    pronunciation_overrides: dict[str, str] = field(default_factory=dict)

    # Output
    limiter_ceiling: float = 0.89
    output_gain_db: float = 0.0
    ambience_ms: float = 0.0      # short room-style tail
    highpass_hz: float = 60.0
    lowpass_hz: float = 0.0       # 0 = no extra LPF

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "VoicePreset":
        def build(cls_, value):
            if not isinstance(value, dict):
                return cls_()
            allowed = getattr(cls_, "__dataclass_fields__", {})
            kwargs = {k: v for k, v in value.items() if k in allowed}
            if cls_ is CompressorSpec:
                return CompressorSpec(**kwargs)
            if cls_ is NeuralLayer:
                return NeuralLayer(**kwargs)
            if cls_ is GlassLayer:
                return GlassLayer(**kwargs)
            if cls_ is MicroLayer:
                return MicroLayer(**kwargs)
            return cls_(**kwargs)

        allowed = cls.__dataclass_fields__
        kwargs: dict[str, Any] = {k: v for k, v in raw.items() if k in allowed}
        kwargs["eq"] = [
            EQBand(**{k: v for k, v in b.items() if k in EQBand.__dataclass_fields__})
            for b in raw.get("eq", []) if isinstance(b, dict)
        ]
        kwargs["compression"] = build(CompressorSpec, raw.get("compression"))
        kwargs["neural"] = build(NeuralLayer, raw.get("neural"))
        kwargs["glass"] = build(GlassLayer, raw.get("glass"))
        kwargs["micro"] = build(MicroLayer, raw.get("micro"))
        return cls(**kwargs)

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2, ensure_ascii=False)


class SpeechMode:
    OFF = "off"
    RESPONSES = "responses"          # default — read assistant replies
    ACTIVITY = "responses_activity"  # replies + brief milestones
    MANUAL = "manual"                # per-message speaker only

    ALL = {OFF, RESPONSES, ACTIVITY, MANUAL}
