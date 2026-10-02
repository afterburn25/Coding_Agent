"""Procedural repair memory — durable record of which repairs worked
and which failed for a given failure signature.

Backed by a plain JSON document (same atomic-write convention as the
autonomy stores). Two lists per signature:

    procedures — attempted repairs with outcome + evidence
    suppressed — signatures whose last procedures keep failing (the
                 coordinator still shows them but will not auto-retry)

A procedure that resolved the incident is *proven*; one that failed is
remembered too, so Nexus does not loop a known-bad fix.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from ..fsutil import atomic_write_text


def _similar(a: str, b: str) -> float:
    """Cheap token-overlap similarity for signature matching."""
    ta, tb = set(a.split(":")[-1].split()), set(b.split(":")[-1].split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


class RepairMemory:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.data = json.loads(
                self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {"version": 1, "procedures": []}
        self.data.setdefault("procedures", [])

    def save(self) -> None:
        atomic_write_text(
            self.path,
            json.dumps(self.data, indent=2, ensure_ascii=False,
                       default=str))

    # ------------------------------------------------------------------
    def record(self, signature: str, *, kind: str, steps: list[str],
               success: bool, confidence: float, detail: str = "",
               duration_s: float = 0.0) -> dict:
        proc = {"signature": signature, "kind": kind,
                "steps": [str(s)[:300] for s in steps][:20],
                "success": bool(success),
                "confidence": round(float(confidence), 3),
                "detail": str(detail)[:500],
                "duration_s": round(float(duration_s), 1),
                "ts": time.time(), "uses": 0}
        self.data["procedures"].append(proc)
        # bound: keep newest 400
        self.data["procedures"] = self.data["procedures"][-400:]
        self.save()
        return proc

    def recall(self, signature: str, *, min_confidence: float = 0.5,
               limit: int = 3) -> list[dict]:
        """Rank past procedures for this signature (exact match first,
        then fuzzy). Successful procedures outrank failures; failures are
        returned flagged so callers can avoid repeating them."""
        scored = []
        for p in self.data.get("procedures", []):
            if p.get("signature") == signature:
                sim = 1.0
            else:
                sim = _similar(p.get("signature", ""), signature)
                if sim < 0.6:
                    continue
            rank = sim * (1.0 if p.get("success") else -0.5) \
                + min(p.get("uses", 0), 10) * 0.01
            scored.append((rank, dict(p, similarity=round(sim, 2))))
        scored.sort(key=lambda t: -t[0])
        return [p for _, p in scored[:limit] if p["confidence"]
                >= min_confidence or not p.get("success")]

    def best_fix(self, signature: str) -> dict | None:
        """Highest-ranked *successful* procedure, if any."""
        for p in self.recall(signature):
            if p.get("success"):
                return p
        return None

    def known_bad(self, signature: str, kind: str) -> bool:
        """True when the same repair kind failed ≥2× for this signature —
        the coordinator should escalate instead of repeating it."""
        fails = sum(1 for p in self.data.get("procedures", [])
                    if p.get("signature") == signature
                    and p.get("kind") == kind and not p.get("success"))
        return fails >= 2
