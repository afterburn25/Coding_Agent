"""Promotion pipeline — changes move through a verified stage chain.

    candidate → simulate → targeted_tests → regression_tests
              → review → canary → promote

A candidate records *which* stages its risk tier requires
(`risk.REQUIRED_STAGES`). Stages can only be marked passed in order —
you cannot record a review for a candidate that never simulated — and
`promote()` refuses while any required stage is unpassed. Nothing here
runs the stages itself; this is the durable, enforced gate that
execution layers (sandbox, self-repair, missions) must satisfy before
their change may land.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .fsutil import atomic_write_text
from .risk import REQUIRED_STAGES, RISKS, STAGES, classify_action

CAND_STATUSES = {"candidate", "in_progress", "promoted", "rejected"}

_HISTORY_BOUND = 300


class PromotionPipeline:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        try:
            self.data = json.loads(
                self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {"version": 1, "candidates": []}
        self.data.setdefault("candidates", [])

    def _save(self) -> None:
        atomic_write_text(
            self.path,
            json.dumps(self.data, indent=2, ensure_ascii=False,
                       default=str))

    # -- reads --------------------------------------------------------
    def get(self, cid: str) -> dict | None:
        for c in self.data["candidates"]:
            if c.get("id") == cid:
                return dict(c)
        return None

    def list(self, *, status: str = "", mission_id: str = "",
             limit: int = 50) -> list[dict]:
        rows = [c for c in self.data["candidates"]
                if (not status or c.get("status") == status)
                and (not mission_id or c.get("mission_id") == mission_id)]
        return [dict(c) for c in rows[-limit:]]

    def summary(self) -> dict:
        rows = self.data["candidates"]
        by = {s: 0 for s in CAND_STATUSES}
        for c in rows:
            by[c.get("status", "candidate")] = by.get(
                c.get("status", "candidate"), 0) + 1
        return {"total": len(rows), "by_status": by}

    # -- mutations ----------------------------------------------------
    def create(self, description: str, *, action: str = "",
               risk: str = "", target: str = "", source: str = "",
               mission_id: str = "", context: dict | None = None) -> dict:
        cls = classify_action(action or description, target=target,
                              context=context)
        tier = risk if risk in RISKS else cls["risk"]
        now = time.time()
        cand = {
            "id": f"cand-{uuid.uuid4().hex[:10]}",
            "description": str(description)[:400],
            "action": str(action)[:80],
            "target": str(target)[:200],
            "risk": tier,
            "risk_reasons": cls["reasons"],
            "required_stages": list(REQUIRED_STAGES[tier]),
            "stages": {},          # stage → {ok, detail, evidence, ts}
            "status": "candidate",
            "source": str(source)[:60],
            "mission_id": str(mission_id)[:60],
            "created_at": now, "updated_at": now,
            "history": [{"ts": now, "event": "candidate",
                         "detail": f"risk={tier}"}],
        }
        with self._lock:
            self.data["candidates"].append(cand)
            self.data["candidates"] = self.data["candidates"][
                -_HISTORY_BOUND:]
            self._save()
        return dict(cand)

    def _stage_blocked_reason(self, cand: dict, stage: str) -> str:
        if stage not in STAGES:
            return f"unknown stage '{stage}'"
        if cand.get("status") in {"promoted", "rejected"}:
            return f"candidate already {cand['status']}"
        required = cand.get("required_stages") or []
        if stage not in required:
            return ""      # extra verification is always allowed
        # In-order gate: every earlier required stage must be passed.
        for earlier in required[:required.index(stage)]:
            if not (cand.get("stages", {}).get(earlier) or {}).get("ok"):
                return f"stage '{earlier}' has not passed yet"
        return ""

    def record_stage(self, cid: str, stage: str, *, ok: bool,
                     detail: str = "", evidence: str = "") -> dict | None:
        """Record a stage outcome. Returns the candidate, or None when
        the stage write is refused (unknown candidate, out-of-order
        stage, or stage not required for this risk tier)."""
        with self._lock:
            for c in self.data["candidates"]:
                if c.get("id") != cid:
                    continue
                reason = self._stage_blocked_reason(c, stage)
                if reason:
                    return None
                c.setdefault("stages", {})[stage] = {
                    "ok": bool(ok), "detail": str(detail)[:400],
                    "evidence": str(evidence)[:400],
                    "ts": time.time()}
                c["status"] = "in_progress"
                c["updated_at"] = time.time()
                c.setdefault("history", []).append(
                    {"ts": c["updated_at"], "event": stage,
                     "detail": ("passed" if ok else "failed")
                               + (f" — {detail}" if detail else "")})
                c["history"] = c["history"][-80:]
                self._save()
                return dict(c)
        return None

    def ready(self, cand: dict) -> tuple[bool, list[str]]:
        """(can_promote, missing_stages)."""
        missing = [s for s in (cand.get("required_stages") or [])
                   if not (cand.get("stages", {}).get(s) or {}).get("ok")]
        return (not missing, missing)

    def promote(self, cid: str, *, actor: str = "nexus",
                evidence: str = "") -> dict | None:
        """Terminal stage. Refuses (None) while any required stage is
        unpassed — no partial promotions."""
        with self._lock:
            for c in self.data["candidates"]:
                if c.get("id") != cid:
                    continue
                ok, missing = self.ready(c)
                if not ok or c.get("status") in {"promoted", "rejected"}:
                    return None
                c["status"] = "promoted"
                c["promoted_at"] = time.time()
                c["promoted_by"] = str(actor)[:60]
                c["updated_at"] = time.time()
                c.setdefault("history", []).append(
                    {"ts": c["promoted_at"], "event": "promoted",
                     "detail": str(evidence)[:300] or "all stages passed"})
                c["history"] = c["history"][-80:]
                self._save()
                return dict(c)
        return None

    def reject(self, cid: str, *, reason: str = "",
               actor: str = "nexus") -> dict | None:
        with self._lock:
            for c in self.data["candidates"]:
                if c.get("id") == cid:
                    c["status"] = "rejected"
                    c["updated_at"] = time.time()
                    c.setdefault("history", []).append(
                        {"ts": c["updated_at"], "event": "rejected",
                         "detail": str(reason)[:300]})
                    c["history"] = c["history"][-80:]
                    self._save()
                    return dict(c)
        return None
