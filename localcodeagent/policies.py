"""Resource modes, expiring temporary policies, offline mode, and
project egress rules.

`ResourcePolicies` is the durable store (`data/policies.json`) for:

- **resource mode** — performance/balanced/quiet/battery (conservative
  is kept as a quiet alias for backward compat). Each mode carries
  concrete knobs: worker concurrency, background jobs, GPU yielding,
  warm-model behavior, max model tier.
- **temporary overrides** — natural-language requests like "pause
  background work for an hour" become bounded policies with
  `expires_at`; expiry restores prior behavior automatically — never a
  silent permanent change.
- **offline mode** — a persisted flag the autonomy policy consults;
  network actions deny while it's on.
- **project egress policy** — `local_only` / `network_read_allowed` /
  `restricted` / `custom` per project; workers inherit it.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any

from .fsutil import atomic_write_text

RESOURCE_MODES = {"performance", "balanced", "quiet", "battery",
                  "conservative"}

MODE_KNOBS = {
    "performance": {"max_workers": 8, "background_jobs": True,
                    "gpu_yield": False, "warm_models": True,
                    "max_model_tier": "any"},
    "balanced": {"max_workers": 4, "background_jobs": True,
                 "gpu_yield": False, "warm_models": True,
                 "max_model_tier": "any"},
    "conservative": {"max_workers": 2, "background_jobs": True,
                     "gpu_yield": True, "warm_models": True,
                     "max_model_tier": "any"},
    "quiet": {"max_workers": 1, "background_jobs": False,
              "gpu_yield": True, "warm_models": False,
              "max_model_tier": "any"},
    "battery": {"max_workers": 1, "background_jobs": False,
                "gpu_yield": True, "warm_models": False,
                "max_model_tier": "small"},
}

EGRESS_MODES = {"local_only", "network_read_allowed", "restricted",
                "custom"}

# Autonomy action classes that need the network — denied in offline
# mode or under a local_only project egress.
NETWORK_ACTIONS = {"research", "browser", "git_push", "create_pr",
                   "packages", "outbound_message"}

_TTL_RE = re.compile(
    r"for (?:the next |an? )?(\d+(?:\.\d+)?)?\s*"
    r"(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?)", re.I)
_TTL_WORD = {"second": 1, "sec": 1, "minute": 60, "min": 60,
             "hour": 3600, "hr": 3600, "day": 86400}


def _parse_ttl(text: str) -> float:
    """Natural duration → seconds. 'tonight' ≈ 8h; default 0 = session."""
    m = _TTL_RE.search(text)
    if m:
        n = float(m.group(1) or 1)
        unit = m.group(2).lower().rstrip("s")
        return n * _TTL_WORD.get(unit, 60)
    if re.search(r"\btonight\b", text, re.I):
        return 8 * 3600
    if re.search(r"\bthis (hour|afternoon|evening)\b", text, re.I):
        return 3600
    return 0.0


class ResourcePolicies:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        try:
            self.data = json.loads(
                self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {"version": 1, "mode": "balanced",
                         "offline": False, "overrides": [],
                         "egress": {}}
        self.data.setdefault("overrides", [])
        self.data.setdefault("egress", {})

    def _save(self) -> None:
        atomic_write_text(
            self.path,
            json.dumps(self.data, indent=2, ensure_ascii=False,
                       default=str))

    # -- resource mode --------------------------------------------------
    def mode(self) -> str:
        return str(self.data.get("mode") or "balanced")

    def set_mode(self, mode: str) -> bool:
        if mode not in RESOURCE_MODES:
            return False
        self.data["mode"] = mode
        self._save()
        return True

    def knobs(self) -> dict:
        return dict(MODE_KNOBS.get(self.mode(), MODE_KNOBS["balanced"]))

    # -- temporary overrides --------------------------------------------
    def add_override(self, key: str, value: Any, *,
                     ttl_seconds: float = 0.0, source: str = "",
                     description: str = "") -> dict:
        """A bounded policy. ttl=0 means 'this session' (still not
        permanent — cleared on restart only if marked session-scoped)."""
        now = time.time()
        row = {"id": f"pol-{uuid_like()}", "key": str(key)[:80],
               "value": value,
               "source": str(source)[:80],
               "description": str(description)[:300],
               "created_at": now,
               "expires_at": now + ttl_seconds if ttl_seconds else 0.0,
               "revoked": False}
        with self._lock:
            self.data["overrides"].append(row)
            self.data["overrides"] = self.data["overrides"][-100:]
            self._save()
        return dict(row)

    def revoke(self, pid: str) -> dict | None:
        with self._lock:
            for o in self.data["overrides"]:
                if o.get("id") == pid:
                    o["revoked"] = True
                    self._save()
                    return dict(o)
        return None

    def active_overrides(self) -> list[dict]:
        now = time.time()
        return [dict(o) for o in self.data["overrides"]
                if not o.get("revoked")
                and (not o.get("expires_at") or o["expires_at"] > now)]

    def effective(self, key: str) -> Any:
        """Latest active value for a key, or None."""
        val = None
        for o in self.active_overrides():
            if o.get("key") == key:
                val = o.get("value")
        return val

    # -- offline ----------------------------------------------------------
    def is_offline(self) -> bool:
        return bool(self.data.get("offline"))

    def set_offline(self, on: bool) -> None:
        self.data["offline"] = bool(on)
        self._save()

    # -- project egress -----------------------------------------------------
    def set_egress(self, project_id: str, mode: str,
                   *, note: str = "") -> dict | None:
        if mode not in EGRESS_MODES:
            return None
        self.data["egress"][str(project_id)] = {
            "mode": mode, "note": str(note)[:300],
            "set_at": time.time()}
        self._save()
        return dict(self.data["egress"][str(project_id)],
                    project_id=project_id)

    def egress_for(self, project_id: str) -> dict:
        row = self.data["egress"].get(str(project_id))
        if not row:
            return {"project_id": str(project_id),
                    "mode": "network_read_allowed"}  # default
        return dict(row, project_id=str(project_id))

    def network_allowed(self, *, project_id: str = "",
                        action: str = "") -> tuple[bool, str]:
        """Combined gate: offline mode denies all network actions;
        local_only project egress denies network; restricted allows
        reads but denies writes (push/PR/message)."""
        if self.is_offline():
            return False, "offline mode is active"
        if project_id:
            mode = self.egress_for(project_id)["mode"]
            if mode == "local_only":
                return False, "project egress is local_only"
            if mode == "restricted" and action in {
                    "git_push", "create_pr", "outbound_message",
                    "packages"}:
                return False, "project egress is restricted"
            if mode == "custom" and action in NETWORK_ACTIONS:
                # custom is declared but has no per-action rules yet —
                # fail closed (reads denied too) rather than silently
                # allowing everything.
                return False, ("project egress is custom (no rules "
                               "defined — denying network actions)")
        return True, ""

    # -- natural language ---------------------------------------------------
    def parse_request(self, text: str) -> dict:
        """Translate a natural-language resource request into concrete
        bounded policy changes. Returns what was applied — never a
        silent permanent mutation."""
        t = str(text or "")
        low = t.lower()
        ttl = _parse_ttl(low)
        applied: list[dict] = []
        out: dict[str, Any] = {"applied": applied, "mode": "",
                               "offline": None, "notes": []}

        def add(key: str, value: Any, desc: str) -> None:
            applied.append(self.add_override(
                key, value, ttl_seconds=ttl, source="user_request",
                description=desc))

        if re.search(r"pause|stop|hold|suspend", low) and \
                re.search(r"background", low):
            add("background_jobs", False, "user asked to pause background work")
            out["notes"].append("background jobs paused")
        if re.search(r"run quiet|quiet(?:ly)?\b|keep it quiet", low):
            self.set_mode("quiet")
            out["mode"] = "quiet"
        if re.search(r"battery", low):
            self.set_mode("battery")
            out["mode"] = "battery"
        if re.search(r"\bperformance\b|full speed|max power", low):
            self.set_mode("performance")
            out["mode"] = "performance"
        if re.search(r"\boffline\b|disconnect|no network", low):
            self.set_offline(True)
            out["offline"] = True
        if re.search(r"\bonline\b|back online|network on", low):
            self.set_offline(False)
            out["offline"] = False
        m = (re.search(r"(?:prioriti[sz]e|focus (?:resources )?on)"
                       r"\s+(?:to\s+|on\s+)?(\w[\w-]*)", low)
             or re.search(r"give (\w[\w-]*(?:\s+\w[\w-]*)?)\s+priority",
                          low))
        if m:
            what = m.group(1).strip()[:40]
            add("priority", what, f"user asked to prioritize {what}")
            out["notes"].append(f"priority={what}")
        m = re.search(r"(?:don'?t|do not|no|avoid|skip)\s+"
                      r"(?:use|load|run|touch)?\s*(?:the\s+)?"
                      r"(\w[\w.-]*(?:b|model|model[s]?))\b", low)
        if m:
            deny = m.group(1).strip()[:60]
            add("model_deny", deny, f"user asked not to use {deny}")
            out["notes"].append(f"model_deny={deny}")
        if ttl:
            out["notes"].append(f"expires in {int(ttl)}s")
        if not applied and not out["mode"] and out["offline"] is None:
            out["notes"].append("no policy directives recognized")
        return out

    def summary(self) -> dict:
        return {"mode": self.mode(), "knobs": self.knobs(),
                "offline": self.is_offline(),
                "active_overrides": self.active_overrides(),
                "egress": dict(self.data["egress"])}


def uuid_like() -> str:
    import uuid
    return uuid.uuid4().hex[:8]
