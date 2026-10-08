from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VOICE_DIR = ROOT / "localcodeagent" / "voice" / "chatterbox_voices" / "isabella"
PRESET_PATH = ROOT / "localcodeagent" / "voice" / "official" / "nexus-isabella-chatterbox.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_isabella_chatterbox_uses_dry_reference_source() -> None:
    meta = _load(VOICE_DIR / "voice.json")
    assert meta["reference"] == "reference-source.mp3"
    assert meta["reference_source"] == "reference-source.mp3"
    assert "dry" in meta["provenance"].lower() or "unprocessed" in meta["provenance"].lower()
    ref = VOICE_DIR / meta["reference"]
    assert ref.exists()
    assert ref.stat().st_size > 100_000


def test_isabella_chatterbox_avoids_barrel_comb_filter_chain() -> None:
    p = _load(PRESET_PATH)
    assert p["stereo_width"] == 0.0
    assert p["ambience_ms"] == 0.0
    assert p["neural"]["enabled"] is False
    assert p["micro"]["enabled"] is False
    assert p["glass"]["delay_ms"] == 0
    assert p["glass"]["echo"] == 0.0
    assert p["highpass_hz"] >= 80.0
    low_mid_cuts = [
        band for band in p["eq"]
        if 250 <= float(band["freq_hz"]) <= 500 and float(band["gain_db"]) <= -2.5
    ]
    assert low_mid_cuts, "Isabella needs an explicit low-mid boxiness cut"


def test_isabella_chatterbox_is_intentionally_louder_but_limited() -> None:
    p = _load(PRESET_PATH)
    assert p["normalize_loudness"] is True
    assert -13.0 <= float(p["loudness_target_lufs"]) <= -12.0
    assert p["limiter_enabled"] is True
    assert 0.88 <= float(p["limiter_ceiling"]) <= 0.90
