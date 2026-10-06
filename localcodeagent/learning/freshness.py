"""Freshness policy (Parts 26–28).

Different knowledge decays at different rates. The policy assigns each
record a freshness class → validity window. Stale knowledge is MARKED
(EXPIRED), never silently deleted, and revalidated through the normal
research path when it matters.
"""

from __future__ import annotations

import time

from . import taxonomy as t

# freshness_class -> default validity window in seconds. None = does not
# decay on a schedule (still revalidatable on contradiction).
FRESHNESS_WINDOWS = {
    "stable": None,            # pure math, historical facts
    "slow": 365 * 24 * 3600,   # language fundamentals, core algorithms
    "moderate": 90 * 24 * 3600,  # framework APIs, library behavior
    "fast": 7 * 24 * 3600,     # releases, drivers, package versions
    "volatile": 3600,          # prices, news, weather
}

# Keyword → freshness class. Order matters: first match wins; most
# volatile classes are checked before slower ones.
_CLASS_SIGNALS = (
    ("volatile", ("price", "cost", "weather", "news", "stock", "score",
                  "today", "right now", "breaking")),
    ("fast", ("latest", "newest", "release", "version", "driver",
              "package", "changelog", "download", "update")),
    ("moderate", ("api", "framework", "library", "sdk", "config",
                  "syntax", "deprecat", "feature")),
    ("slow", ("algorithm", "language", "protocol", "standard",
              "theorem", "history", "specification")),
    ("stable", ("math", "proof", "constant", "definition", "axiom")),
)


class FreshnessPolicy:
    def __init__(self, windows: dict | None = None) -> None:
        self.windows = dict(FRESHNESS_WINDOWS)
        if windows:
            self.windows.update(windows)

    def classify(self, text: str, default: str = "moderate") -> str:
        low = (text or "").lower()
        for cls, signals in _CLASS_SIGNALS:
            if any(s in low for s in signals):
                return cls
        return default

    def window(self, freshness_class: str) -> float | None:
        return self.windows.get(freshness_class)

    def valid_until(self, freshness_class: str, *, now: float | None = None,
                    valid_from: float | None = None) -> float | None:
        win = self.window(freshness_class)
        if win is None:
            return None
        return (valid_from if valid_from is not None else (now or time.time())) + win

    def is_stale(self, record: dict, *, now: float | None = None) -> bool:
        """A record is stale when its validity window has elapsed."""
        state = record.get("promotion") or record.get("state") or ""
        if state in (t.EXPIRED, t.SUPERSEDED):
            return True
        # KnowledgeMemory-style TTL fields take precedence when present.
        if record.get("stale"):
            return True
        vu = record.get("valid_until") or record.get("expires_at")
        if vu is None:
            fclass = record.get("freshness_class")
            vf = record.get("last_verified") or record.get("created") or record.get("updated")
            if fclass and vf:
                vu = self.valid_until(fclass, valid_from=float(vf))
        return vu is not None and float(now or time.time()) > float(vu)

    def mark_expired(self, record: dict, *, now: float | None = None) -> dict:
        """Mark (do not delete) a record as expired."""
        record = dict(record)
        record["promotion"] = t.EXPIRED
        record["expired_at"] = now or time.time()
        return record
