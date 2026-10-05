"""Canonical Nexus visual identity — the assistant's face.

The attached portrait is the source of truth for who Nexus looks like.
It lives at ``web/assets/nexus-portrait.{webp,png,jpg}`` (checked in or
dropped into the deployment). Derivatives (circle-cropped chat avatar,
larger hero render) are generated from it — never a different face.

If no portrait asset exists yet we fall back to the Nexus Core emblem so
no random character or broken image ever appears in her place.

Semantic expression states (``neutral``, ``friendly``, ``happy``,
``focused``, ``concerned``, ``playful``, ``confident``) are exposed for a
future subtle-animation layer — gesture SSE events already map onto
``body[data-gesture]``; this module reports which semantic state is active
so a renderer can pick a portrait variant when one exists.
"""
from __future__ import annotations

import io
import time
from pathlib import Path
from typing import Any

try:
    from PIL import Image, ImageDraw
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False

PORTRAIT_NAMES = ("nexus-portrait.webp", "nexus-portrait.png",
                  "nexus-portrait.jpg")
FALLBACK_NAME = "nexus-core-icon.png"
CACHE_DIR = "nexus_avatar"

# Face-focused crop for chat avatars. The canonical portrait is a bust
# render — face in the upper-center — so a plain center-crop leaves the
# avatar mostly neck/shoulders/background. Crop a square biased toward
# the face instead, matching how a user profile photo is framed.
FACE_CROP_SCALE = 0.72   # crop square side, as a fraction of min(w, h)
FACE_CENTER_Y = 0.36     # face center, as a fraction of image height
DERIV_VERSION = 2        # bump when the crop recipe changes (cache stamp)

# Semantic states a future animation/variant layer may key on. Keep this
# vocabulary stable — UI/CSS and the gesture engine already speak it.
EXPRESSION_STATES = (
    "neutral", "friendly", "happy", "focused",
    "concerned", "playful", "confident",
)

# Runtime presentation states. These describe observable activity only;
# they are deliberately separate from semantic expression states.
ACTIVITY_STATES = ("idle", "listening", "thinking", "speaking")

# Gesture events → expression hint (bounded, conservative map).
GESTURE_EXPRESSION = {
    "small_smile": "friendly",
    "amused_expression": "happy",
    "playful_expression": "playful",
    "focused_expression": "focused",
    "concerned_expression": "concerned",
    "confident_expression": "confident",
    "nod": "confident",
    "small_nod": "friendly",
    "head_tilt": "playful",
    "eyebrow_raise": "playful",
    "shake_head": "concerned",
    "small_head_shake": "concerned",
    "eyes_widen": "playful",
    "subtle_exhale": "neutral",
    "small_frown": "concerned",
    "eye_narrow": "concerned",
    "smirk": "playful",
    "soft_gaze": "friendly",
    "bright_smile": "happy",
    "slow_blink": "neutral",
    "slight_lean": "focused",
    "contented_expression": "friendly",
    "wince": "concerned",
    "look_away": "neutral",
    "soft_smile": "friendly",
}


class NexusAvatar:
    """Resolves and derives the canonical Nexus portrait."""

    def __init__(self, web_root: Path, cache_root: Path | None = None) -> None:
        self.web_root = Path(web_root)
        self.cache_root = (Path(cache_root) if cache_root
                           else self.web_root / "assets" / CACHE_DIR)
        self._portrait: Path | None = None
        self._portrait_mtime = 0.0
        self._expression = "neutral"
        self._expression_until = 0.0
        self._activity = "idle"
        self._activity_until = 0.0

    # -- source -----------------------------------------------------------

    def portrait(self) -> Path | None:
        """The canonical portrait file, or None if not provided yet."""
        assets = self.web_root / "assets"
        for name in PORTRAIT_NAMES:
            p = assets / name
            if p.is_file():
                return p
        return None

    def _refresh(self) -> Path | None:
        p = self.portrait()
        if p is not None:
            try:
                self._portrait_mtime = p.stat().st_mtime
            except OSError:
                pass
        self._portrait = p
        return p

    # -- derivatives --------------------------------------------------------

    def avatar(self, size: int = 64) -> tuple[Path, str] | None:
        """Circle-cropped WebP derivative at ``size`` px (cached by
        mtime). Returns (path, mime) or the fallback emblem."""
        src = self._refresh()
        if src is None:
            fb = self.web_root / "assets" / FALLBACK_NAME
            return (fb, "image/png") if fb.is_file() else None
        if not _HAS_PIL:
            mime = {"webp": "image/webp", "png": "image/png",
                    "jpg": "image/jpeg"}.get(src.suffix.lstrip(".").lower(),
                                             "image/png")
            return src, mime
        self.cache_root.mkdir(parents=True, exist_ok=True)
        size = max(16, min(1024, int(size)))
        out = self.cache_root / f"portrait-{size}.webp"
        stamp = f"v{DERIV_VERSION}-{self._portrait_mtime}-{size}"
        stamp_path = self.cache_root / f"portrait-{size}.stamp"
        try:
            if out.is_file() and stamp_path.is_file() and \
                    stamp_path.read_text() == stamp:
                return out, "image/webp"
            im = Image.open(src).convert("RGB")
            w, h = im.size
            side = min(w, h)
            crop = int(side * FACE_CROP_SCALE)
            if crop < side:
                cx, cy = w // 2, int(h * FACE_CENTER_Y)
                left = min(max(0, cx - crop // 2), w - crop)
                top = min(max(0, cy - crop // 2), h - crop)
                im = im.crop((left, top, left + crop, top + crop))
            else:
                im = im.crop(((w - side) // 2, (h - side) // 2,
                              (w + side) // 2, (h + side) // 2))
            im = im.resize((size, size), Image.LANCZOS)
            mask = Image.new("L", (size, size), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
            rgba = im.convert("RGBA")
            rgba.putalpha(mask)
            rgba.save(out, "WEBP", quality=88)
            stamp_path.write_text(stamp)
            return out, "image/webp"
        except Exception:
            mime = {"webp": "image/webp", "png": "image/png",
                    "jpg": "image/jpeg"}.get(src.suffix.lstrip(".").lower(),
                                             "image/png")
            return src, mime

    # -- expression state ---------------------------------------------------

    def set_expression(self, state: str, *, hold_s: float = 6.0) -> bool:
        """Semantic state hook for the future animation layer — bounded
        hold so the face settles back to neutral."""
        if state not in EXPRESSION_STATES:
            return False
        self._expression = state
        self._expression_until = time.time() + max(0.5, float(hold_s))
        return True

    def on_gesture(self, gesture: str) -> None:
        hint = GESTURE_EXPRESSION.get(str(gesture or ""))
        if hint:
            self.set_expression(hint)

    def expression(self) -> str:
        if self._expression != "neutral" and \
                time.time() > self._expression_until:
            self._expression = "neutral"
        return self._expression

    # -- activity state -----------------------------------------------------

    def set_activity(self, state: str, *, hold_s: float = 6.0) -> bool:
        """Bounded observable activity for the avatar renderer."""
        if state not in ACTIVITY_STATES:
            return False
        self._activity = state
        self._activity_until = time.time() + max(0.5, float(hold_s))
        return True

    def note_utterance(self, seconds: float) -> bool:
        """Approximate lip-sync timing from an utterance duration."""
        try:
            duration = float(seconds)
        except (TypeError, ValueError):
            return False
        if duration <= 0:
            return False
        return self.set_activity(
            "speaking", hold_s=min(30.0, max(0.8, duration + 0.35)))

    def on_voice_event(self, payload: dict | None) -> None:
        """Consume non-sensitive voice lifecycle events for avatar state."""
        p = payload or {}
        event = str(p.get("event") or "")
        if event == "segment":
            self.note_utterance(p.get("seconds"))
        elif event in {"queued", "started"}:
            self.set_activity("thinking", hold_s=12.0)
        elif event in {"stop", "muted", "error", "cancelled"}:
            self.set_activity("idle", hold_s=1.0)

    def activity(self) -> str:
        if self._activity != "idle" and \
                time.time() > self._activity_until:
            self._activity = "idle"
        return self._activity

    def status(self) -> dict[str, Any]:
        src = self._refresh()
        return {
            "portrait": src.name if src else None,
            "canonical": bool(src),
            "fallback": None if src else FALLBACK_NAME,
            "expression": self.expression(),
            "activity": self.activity(),
            "states": list(EXPRESSION_STATES),
            "activities": list(ACTIVITY_STATES),
            "derivatives": sorted(
                p.name for p in self.cache_root.glob("portrait-*.webp"))
            if self.cache_root.is_dir() else [],
        }
