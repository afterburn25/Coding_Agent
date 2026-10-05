"""Photorealistic image-model fleet — declarative profiles + request routing.

The fleet is a small, curated set of SDXL photoreal checkpoints with
distinct roles. Models are *described* here; whether one can actually run
is decided at request time by matching the fleet spec against the models a
backend reports as installed (InvokeAI's model registry today).

Source metadata was verified against Hugging Face (repo API + paths-info)
on 2026-10-05 — sizes and SHA-256 digests are real upstream values, not
estimates. Licenses are per-model; InvokeAI's own license does not apply.
"""
from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# Fleet catalog — each entry is a model *spec*, not an install record.
# `match` substrings recognize an installed backend row (case-insensitive).
# `invokeai_source` is the `source=` argument InvokeAI's
# /api/v2/models/install accepts. Direct resolve/main URLs are used —
# the "repo::file" pin form downloads into a folder that InvokeAI's
# model identifier cannot classify (registers as type "unknown"),
# verified on InvokeAI 6.14.2.
# ---------------------------------------------------------------------------

FLEET: tuple[dict[str, Any], ...] = (
    {
        "id": "juggernaut-xl-v9",
        "display_name": "Juggernaut XL v9",
        "role": "general_photoreal",
        "base": "sdxl",
        "match": ["juggernaut-xl-v9", "juggernaut_xl", "juggernaut xl",
                  "juggernaut"],
        "invokeai_source": ("https://huggingface.co/RunDiffusion/Juggernaut-XL-v9"
                            "/resolve/main/"
                            "Juggernaut-XL_v9_RunDiffusionPhoto_v2.safetensors"),
        "source_repo": "RunDiffusion/Juggernaut-XL-v9",
        "source_file": "Juggernaut-XL_v9_RunDiffusionPhoto_v2.safetensors",
        "size_bytes": 7105348188,
        "sha256": ("c9e3e68f89b8e38689e1097d4be4573cf308de4e3fd044c64ca69"
                   "7bdb4aa8bca"),
        "license_name": "CreativeML OpenRAIL-M",
        "homepage": "https://huggingface.co/RunDiffusion/Juggernaut-XL-v9",
        "version": "v9 (RunDiffusionPhoto v2)",
        "strengths": ["full scenes", "people in environments",
                      "full-body composition", "architecture with people",
                      "outdoor realism", "mixed compositions",
                      "general photoreal fallback"],
        "weaknesses": ["beauty/face retouch detail vs portrait specialists"],
        "supported_operations": ["text_to_image", "edit_image", "inpaint",
                                 "variation"],
        "preferred_resolution": [1024, 1024],
        "sampling": {"steps": 30, "guidance": 5.0, "sampler": "dpmpp_2m_sde",
                     "scheduler": "karras"},
        "estimated_vram_gb": 10.0,
        "estimated_ram_gb": 16.0,
        "adult_capable": False,
        "restriction_status": "unknown_capability",
        # Trait weights — the classifier maps request characteristics onto
        # these keys; highest total wins routing.
        "traits": {"photoreal": 2, "general": 2, "scene": 3,
                   "environment": 3, "full_body": 3, "people": 2,
                   "architecture": 2, "outdoor": 3, "product": 1,
                   "landscape": 3, "portrait": 1, "glamour": 0,
                   "face": 0, "editorial": 1, "body": 1},
    },
    {
        "id": "realvisxl-v5",
        "display_name": "RealVisXL V5.0",
        "role": "glamour_photoreal",
        "base": "sdxl",
        "match": ["realvisxl_v5", "realvisxl-v5", "realvisxl v5",
                  "realvis"],
        "invokeai_source": ("https://huggingface.co/SG161222/RealVisXL_V5.0"
                            "/resolve/main/RealVisXL_V5.0_fp16.safetensors"),
        "source_repo": "SG161222/RealVisXL_V5.0",
        "source_file": "RealVisXL_V5.0_fp16.safetensors",
        "size_bytes": 6938065488,
        "sha256": ("6a35a7855770ae9820a3c931d4964c3817b6d9e3c6f9c4dabb5b"
                   "3a94e5643b80"),
        "license_name": "OpenRAIL++",
        "homepage": "https://huggingface.co/SG161222/RealVisXL_V5.0",
        "version": "V5.0 (fp16)",
        "strengths": ["glamour photography", "skin detail",
                      "boudoir-style adult imagery (clearly adult only)",
                      "body-focused composition",
                      "dramatic studio photography"],
        "weaknesses": ["wide environmental scenes vs Juggernaut"],
        "supported_operations": ["text_to_image", "edit_image", "inpaint",
                                 "variation"],
        "preferred_resolution": [1024, 1024],
        "sampling": {"steps": 30, "guidance": 5.0, "sampler": "dpmpp_2m_sde",
                     "scheduler": "karras"},
        "estimated_vram_gb": 10.0,
        "estimated_ram_gb": 16.0,
        "adult_capable": True,
        "restriction_status": "adult_capable",
        "traits": {"photoreal": 2, "general": 1, "scene": 1,
                   "environment": 0, "full_body": 2, "people": 2,
                   "architecture": 0, "outdoor": 1, "product": 0,
                   "landscape": 0, "portrait": 1, "glamour": 3,
                   "face": 1, "editorial": 2, "body": 3, "skin": 3,
                   "adult": 3},
    },
    {
        "id": "cyberrealistic-xl-v9",
        "display_name": "CyberRealistic XL",
        "role": "portrait_photoreal",
        "base": "sdxl",
        "match": ["cyberrealistic", "cyberrealisticxl", "cyber realistic"],
        "invokeai_source": ("https://huggingface.co/cyberdelia/"
                            "CyberRealisticXL/resolve/main/"
                            "CyberRealisticXLPlay_V9.0_FP16.safetensors"),
        "source_repo": "cyberdelia/CyberRealisticXL",
        "source_file": "CyberRealisticXLPlay_V9.0_FP16.safetensors",
        "size_bytes": 6938040682,
        "sha256": ("4f2dc6418c8d93737e2d0a5f022707977c783851e79ccd23a"
                   "ed1fda8397ac10e"),
        "license_name": "CreativeML OpenRAIL-M",
        "homepage": "https://huggingface.co/cyberdelia/CyberRealisticXL",
        "version": "V9.0 (fp16)",
        "strengths": ["portraits", "close-ups", "headshots",
                      "beauty photography", "face fidelity",
                      "editorial/fashion portraiture",
                      "profile/identity imagery"],
        "weaknesses": ["wide scenes and architecture vs Juggernaut"],
        "supported_operations": ["text_to_image", "edit_image", "inpaint",
                                 "variation"],
        "preferred_resolution": [1024, 1024],
        "sampling": {"steps": 30, "guidance": 5.0, "sampler": "dpmpp_2m_sde",
                     "scheduler": "karras"},
        "estimated_vram_gb": 10.0,
        "estimated_ram_gb": 16.0,
        "adult_capable": False,
        "restriction_status": "unknown_capability",
        "traits": {"photoreal": 2, "general": 1, "scene": 0,
                   "environment": 0, "full_body": 1, "people": 1,
                   "architecture": 0, "outdoor": 0, "product": 0,
                   "landscape": 0, "portrait": 3, "glamour": 1,
                   "face": 3, "editorial": 2, "body": 1, "skin": 1,
                   "beauty": 3, "identity": 2},
    },
)

FLEET_BY_ID: dict[str, dict[str, Any]] = {m["id"]: m for m in FLEET}

# Fallback preference when the routed model is unavailable: Juggernaut is
# the generalist — every specialist degrades to it, then to whatever else
# is installed.
FALLBACK_ORDER = {
    "cyberrealistic-xl-v9": ["juggernaut-xl-v9"],
    "realvisxl-v5": ["juggernaut-xl-v9"],
    "juggernaut-xl-v9": ["cyberrealistic-xl-v9", "realvisxl-v5"],
}


def fleet_for_model_name(name: str) -> dict[str, Any] | None:
    """Match an installed backend model name/key to a fleet entry."""
    text = str(name or "").lower()
    for spec in FLEET:
        if any(marker in text for marker in spec["match"]):
            return spec
    return None


# ---------------------------------------------------------------------------
# Request classification — trait extraction from the prompt. Pure heuristic
# keyword scoring; results feed fleet routing, never safety decisions.
# ---------------------------------------------------------------------------

_TRAIT_PATTERNS: dict[str, tuple[str, ...]] = {
    "portrait": ("portrait", "close-up", "closeup", "close up", "headshot",
                 "head shot", "head and shoulders", "mugshot", "selfie",
                 "profile picture", "profile image", "avatar of"),
    "face": ("face", "facial", "beauty", "eyes", "skin texture",
             "skin detail", "makeup", "retouch"),
    "beauty": ("beauty", "glamour model face", "editorial face"),
    "glamour": ("glamour", "boudoir", "lingerie", "sensual", "seductive",
                "swimsuit", "bikini", "intimate", "pin-up", "pinup",
                "playboy", "bedroom photo"),
    "body": ("body", "figure", "physique", "curves", "topless", "nude",
             "naked", "abs", "muscle"),
    "adult": ("nsfw", "nude", "naked", "topless", "explicit", "erotic",
              "boudoir", "lingerie", "seductive", "sensual", "intimate"),
    "full_body": ("full body", "full-body", "head to toe", "standing",
                  "walking", "full length", "full-length"),
    "scene": ("scene", "wide shot", "cityscape", "skyline", "street",
              "environment", "background", "setting", "room", "interior",
              "crowd", "times square", "downtown", "cinematic"),
    "environment": ("environment", "landscape", "scenery", "outdoor",
                    "outdoors", "nature", "forest", "beach", "mountain",
                    "architecture", "building", "interior", "room"),
    "architecture": ("architecture", "building", "skyscraper", "interior",
                     "cathedral", "bridge", "room"),
    "outdoor": ("outdoor", "outdoors", "street", "park", "beach", "city",
                "night", "skyline", "downtown", "nature"),
    "landscape": ("landscape", "vista", "panorama", "scenery", "horizon"),
    "product": ("product", "packshot", "catalog", "advertisement"),
    "people": ("person", "people", "woman", "man", "girl", "boy", "lady",
               "couple", "crowd", "model", "portrait", "figure", "worker",
               "soldier", "dancer"),
    "editorial": ("editorial", "fashion", "magazine", "vogue", "studio",
                  "professional photo", "photoshoot"),
    "identity": ("profile", "identity", "passport", "reference image",
                 "same person", "consistent character"),
}

_REQUEST_IS_PHOTOREAL = ("photo", "photoreal", "realistic", "real life",
                       "real-life", "hyperreal", "cinematic photo",
                       "dslr", "35mm", "shot on", "photograph")
_REQUEST_IS_ILLUSTRATIVE = ("anime", "cartoon", "illustration", "painting",
                            "watercolor", "sketch", "comic", "pixel art",
                            "stylized", "cel shading", "vector")


def classify_request_traits(prompt: str) -> dict[str, Any]:
    """Extract routing traits from a natural-language image request.

    Returns {"traits": {trait: weight}, "photoreal": bool, "adult": bool,
    "illustrative": bool}. Adult is a routing hint only — the safety
    policy still gates content separately."""
    text = f" {(prompt or '').lower()} "
    traits: dict[str, int] = {}
    for trait, markers in _TRAIT_PATTERNS.items():
        hits = 0
        for m in markers:
            # Multi-word markers match literally; single words need
            # boundaries so "abs" doesn't fire on "absolute".
            if (" " in m and m in text) or \
                    (" " not in m and re.search(rf"\b{re.escape(m)}\b", text)):
                hits += 1
        if hits:
            traits[trait] = min(3, hits)
    photoreal = bool(traits.get("portrait") or traits.get("glamour")) or \
        any(m in text for m in _REQUEST_IS_PHOTOREAL)
    illustrative = any(m in text for m in _REQUEST_IS_ILLUSTRATIVE)
    return {
        "traits": traits,
        "photoreal": photoreal and not illustrative,
        "illustrative": illustrative,
        "adult": bool(traits.get("adult")),
    }


def score_fleet_model(spec: dict[str, Any], traits: dict[str, int]) -> int:
    """Weighted trait match — mirrors the worked example in the spec."""
    weights = spec.get("traits") or {}
    return sum(weights.get(t, 0) * int(w) for t, w in traits.items())


def rank_fleet(traits: dict[str, int],
               installed_fleet_ids: set[str]) -> list[tuple[int, dict]]:
    """Score fleet specs, best first; installed entries sort above missing
    ones at equal score so an absent model never wins by keyword alone."""
    rows = []
    for spec in FLEET:
        score = score_fleet_model(spec, traits)
        if score <= 0:
            continue
        rows.append((score + (1000 if spec["id"] in installed_fleet_ids else 0),
                     spec))
    rows.sort(key=lambda r: (-r[0], r[1]["id"]))
    return rows
