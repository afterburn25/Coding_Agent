"""Freshness classes and TTL handling for stored answers."""

from __future__ import annotations

import time

FRESHNESS_CLASSES = (
    "static",
    "user_defined",
    "application_state",
    "configuration_dependent",
    "repository_dependent",
    "time_sensitive",
    "live",
)

# Default TTLs in seconds (None = no time-based expiry).
DEFAULT_TTL_SECONDS = {
    "static": None,
    "user_defined": None,
    "application_state": None,       # dependency-driven, not time-driven
    "configuration_dependent": None, # dependency-driven
    "repository_dependent": None,    # dependency-driven
    "time_sensitive": 72 * 3600,
    "live": 0,
}


def expiry_for(freshness: str, now: float | None = None) -> float | None:
    ttl = DEFAULT_TTL_SECONDS.get(freshness)
    if ttl in (None, 0):
        return None if ttl is None else (now or time.time())
    return (now or time.time()) + ttl


def is_expired(row: dict, now: float | None = None) -> bool:
    expires = row.get("expires_at")
    if expires is None:
        return False
    return float(expires) <= (now or time.time())


def infer_freshness(question: str, cacheability: str) -> str:
    """Best-effort freshness class for an automatically learned answer."""
    q = question.lower()
    if cacheability == "live":
        return "live"
    if cacheability in {"task_specific", "transformation", "contextual"}:
        return "time_sensitive"
    if any(
        term in q
        for term in (
            "installed model", "which model", "what model", "default model",
            "what checkpoint", "which checkpoint", "version of nexus",
            "nexus version", "what version", "installed", "selected runtime",
        )
    ):
        return "application_state"
    if any(
        term in q
        for term in (
            "where is", "which file", "what file", "in the repo", "in this repo",
            "codebase", "repository", "src/", ".py", ".js", ".ts", ".cs",
        )
    ):
        return "repository_dependent"
    if any(term in q for term in ("config", "setting", "configured", "option")):
        return "configuration_dependent"
    if any(
        term in q
        for term in ("latest", "recent", "news", "today", "this week", "currently")
    ):
        return "time_sensitive"
    return "static"
