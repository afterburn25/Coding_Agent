"""Release Candidate mode + scorecard.

`RCManager` (`data/rc.json`) drives the RC workflow:

- `enter(label)` freezes non-essential change paths — while active,
  `AutonomyPolicy` denies `self_development` (feature work on the
  product itself) and `packages` (dependency upgrades).
- `run_scorecard()` executes the registered domain checks and renders
  the release verdict. Every section is one of `pass` / `fail` /
  `skip` / `unknown`; a `fail` *or* `unknown` in a blocking section
  means the release is NOT ready. "Skip" is only valid when a check
  was explicitly skipped with a note — never a silent omission.
- Scorecards persist (bounded history) so the evidence trail survives
  restarts.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .fsutil import atomic_write_text

# Blocking sections — a fail or unknown in any of these blocks release.
SECTIONS = [
    "functional_tests", "performance", "installer", "upgrade",
    "profiles", "voice", "images", "workers", "memory", "self_repair",
]

# Frozen while RC is active — autonomous work in these classes denies.
RC_FROZEN_ACTIONS = {"self_development", "packages"}


class RCManager:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._checks: dict[str, Callable[[], dict]] = {}
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {"version": 1, "active": False, "label": "",
                         "since": 0.0, "manual": {}, "scorecards": []}
        self.data.setdefault("manual", {})
        self.data.setdefault("scorecards", [])

    def _save(self) -> None:
        atomic_write_text(self.path, json.dumps(
            self.data, indent=2, ensure_ascii=False, default=str))

    # -- lifecycle ---------------------------------------------------------
    def is_active(self) -> bool:
        return bool(self.data.get("active"))

    def enter(self, label: str = "") -> dict:
        self.data["active"] = True
        self.data["label"] = str(label)[:120]
        self.data["since"] = time.time()
        self.data["manual"] = {}
        self._save()
        return self.status()

    def exit(self) -> dict:
        self.data["active"] = False
        self._save()
        return self.status()

    # -- checks --------------------------------------------------------------
    def register_check(self, section: str,
                       fn: Callable[[], dict]) -> None:
        """Live check: returns {status, detail?, blocking?}."""
        if section in SECTIONS:
            self._checks[section] = fn

    def record_section(self, section: str, status: str,
                       *, detail: str = "", blocking: bool = True,
                       evidence: str = "") -> dict | None:
        """Persist a manual/external check result for a section."""
        if section not in SECTIONS or status not in {
                "pass", "fail", "skip"}:
            return None
        row = {"status": status, "detail": str(detail)[:400],
               "blocking": bool(blocking),
               "evidence": str(evidence)[:400],
               "recorded_at": time.time()}
        self.data["manual"][section] = row
        self._save()
        return dict(row, section=section)

    # -- scorecard -------------------------------------------------------------
    def _evaluate(self, section: str) -> dict:
        """Live check wins; else manual record; else unknown."""
        fn = self._checks.get(section)
        if fn is not None:
            try:
                out = dict(fn() or {})
                status = out.get("status")
                if status not in {"pass", "fail", "skip"}:
                    status = "unknown"
                return {"section": section, "status": status,
                        "detail": str(out.get("detail") or "")[:400],
                        "blocking": bool(out.get("blocking", True)),
                        "source": "live"}
            except Exception as exc:
                return {"section": section, "status": "fail",
                        "detail": f"check raised: {exc}"[:200],
                        "blocking": True, "source": "live"}
        man = self.data["manual"].get(section)
        if man:
            return dict(man, section=section, source="manual")
        return {"section": section, "status": "unknown", "detail": "",
                "blocking": True, "source": "none"}

    def run_scorecard(self) -> dict:
        sections = {s: self._evaluate(s) for s in SECTIONS}
        blocking = [s for s, r in sections.items()
                    if r["blocking"] and r["status"] in {"fail",
                                                         "unknown"}]
        known_issues = [f"{s}: {r['detail'] or r['status']}"
                        for s, r in sections.items()
                        if not r["blocking"] and r["status"] == "fail"]
        verdict = "PASS" if not blocking else "FAIL"
        card = {"id": f"rc-{uuid.uuid4().hex[:10]}",
                "created_at": time.time(),
                "label": self.data.get("label") or "",
                "verdict": verdict,
                "sections": sections,
                "blocking_issues": [
                    f"{s}: {sections[s]['detail'] or sections[s]['status']}"
                    for s in blocking],
                "known_issues": known_issues}
        with self._lock:
            self.data["scorecards"].append(card)
            self.data["scorecards"] = self.data["scorecards"][-20:]
            self._save()
        return card

    def status(self) -> dict:
        last = self.data["scorecards"][-1] if self.data["scorecards"] else None
        return {"active": self.is_active(),
                "label": self.data.get("label") or "",
                "since": self.data.get("since") or 0.0,
                "sections": SECTIONS,
                "frozen_actions": sorted(RC_FROZEN_ACTIONS)
                if self.is_active() else [],
                "last_scorecard": last,
                "scorecards": len(self.data["scorecards"])}
