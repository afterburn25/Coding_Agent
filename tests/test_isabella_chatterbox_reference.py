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
    assert not (VOICE_DIR / "reference.wav").exists(), (
        "Do not condition Chatterbox from the old processed V6 reference"
    )


def test_isabella_v7_applies_v6_character_once_after_dry_clone() -> None:
    p = _load(PRESET_PATH)
    assert p["target_signature"] == "approved-v7-v6-character-louder"
    assert float(p["synthetic"]) == 0.8
    assert p["neural"]["enabled"] is True
    assert p["glass"]["enabled"] is True
    assert p["micro"]["enabled"] is True
    # Original V6 character is allowed once post-generation. Barrel prevention
    # now comes from the DRY conditioning reference and centered output rather
    # than deleting the character the user approved.
    total_parallel_mix = (
        float(p["neural"]["mix"])
        + float(p["glass"]["mix"])
        + float(p["micro"]["mix"])
    )
    assert total_parallel_mix <= 0.40
    assert p["stereo_width"] == 0.0
    assert p["ambience_ms"] == 0.0


def test_isabella_v7_preserves_approved_tonal_shape() -> None:
    p = _load(PRESET_PATH)
    assert 1.1 <= float(p["pitch_semitones"]) <= 1.4
    assert 0.95 <= float(p["tempo"]) <= 1.01
    low_cut = [b for b in p["eq"] if 180 <= float(b["freq_hz"]) <= 300 and float(b["gain_db"]) <= -2.5]
    presence = [b for b in p["eq"] if 3000 <= float(b["freq_hz"]) <= 4200 and float(b["gain_db"]) >= 5.0]
    air = [b for b in p["eq"] if 6500 <= float(b["freq_hz"]) <= 8500 and float(b["gain_db"]) >= 2.5]
    assert low_cut and presence and air


def test_isabella_chatterbox_is_intentionally_louder_but_limited() -> None:
    p = _load(PRESET_PATH)
    assert p["normalize_loudness"] is True
    assert -13.0 <= float(p["loudness_target_lufs"]) <= -12.0
    assert p["limiter_enabled"] is True
    assert 0.88 <= float(p["limiter_ceiling"]) <= 0.90
