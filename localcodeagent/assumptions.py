"""Assumption Ledger — every meaningful plan assumption is a durable,
inspectable record, not an implicit hope inside a prompt.

An assumption row captures the claim, the work it underpins
(``dependents``: decisions/tasks/requirements/missions/procedures),
and a lifecycle state. When evidence invalidates an assumption the
ledger reports exactly which work depended on it, so re-evaluation is
targeted instead of a rebuild-everything or worse — silently building
on a false premise.

States:
    untested    — recorded, no evidence yet
    supported   — some evidence agrees
    verified    — directly tested and held
    invalidated — evidence contradicts it
    superseded  — replaced by a newer assumption (linked)

The ``weakest()`` probe feeds the unknown-unknown check: before a
high-impact decision, the assumptions with the least evidence are the
targeted-investigation list.
"""
from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path
from typing import Any

ASSUMPTION_STATES = {
    "untested", "supported", "verified", "invalidated", "superseded",
}
_BOUND = 500


class AssumptionLedger:
    """Durable store at ``data/assumptions.json`` (DocStore-backed)."""

    def __init__(self, path: Path | str, db: Any = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        from .state_db import DocStore
        self._doc = DocStore(db, self.path, domain="assumptions")
        self.data = self._doc.load_json(
            {"version": 1, "assumptions": []})
        if not isinstance(self.data, dict):
            self.data = {"version": 1, "assumptions": []}
        self.data.setdefault("assumptions", [])

    def _save(self) -> None:
        self._doc.save_json(self.data)

    # ------------------------------------------------------------------
    def add(self, text: str, *, scope_type: str = "global",
            scope_id: str = "",
            dependents: dict | None = None,
            confidence: float = 0.5,
            test: str = "") -> dict:
        """Record an assumption. ``dependents`` maps kind → [ids] so
        invalidation later reports exactly what is at risk."""
        now = time.time()
        deps = {"decisions": [], "requirements": [], "missions": [],
                "tasks": [], "procedures": []}
        for k, v in (dependents or {}).items():
            deps.setdefault(k, [])
            deps[k].extend(str(i) for i in (v or []))
        row = {
            "id": f"asm-{uuid.uuid4().hex[:10]}",
            "text": str(text)[:500],
            "scope": {"type": str(scope_type or "global"),
                      "id": str(scope_id or "")},
            "state": "untested",
            "confidence": round(max(0.0, min(0.99, float(confidence))), 3),
            "test": str(test)[:300],
            "dependents": deps,
            "evidence": [],
            "history": [{"ts": now, "state": "untested",
                         "detail": "recorded"}],
            "superseded_by": "",
            "created_at": now, "updated_at": now,
        }
        with self._lock:
            self.data["assumptions"].append(row)
            self.data["assumptions"] = self.data["assumptions"][-_BOUND:]
            self._save()
        return dict(row)

    def _row(self, aid: str) -> dict | None:
        for r in self.data["assumptions"]:
            if r.get("id") == aid:
                return r
        return None

    def get(self, aid: str) -> dict | None:
        with self._lock:
            r = self._row(aid)
            return dict(r) if r else None

    # ------------------------------------------------------------------
    def set_state(self, aid: str, state: str, *, evidence: str = "",
                  ref: str = "") -> dict | None:
        """Move an assumption along its lifecycle with provenance."""
        if state not in ASSUMPTION_STATES:
            return None
        with self._lock:
            r = self._row(aid)
            if r is None:
                return None
            r["state"] = state
            r["updated_at"] = time.time()
            if evidence:
                r["evidence"].append({"ts": time.time(),
                                      "detail": str(evidence)[:300],
                                      "ref": str(ref)[:200],
                                      "state": state})
                r["evidence"] = r["evidence"][-30:]
            r["history"].append({"ts": time.time(), "state": state,
                                 "detail": str(evidence)[:200]})
            r["history"] = r["history"][-40:]
            # Evidence direction nudges confidence; verification and
            # invalidation are terminal for it.
            if state == "verified":
                r["confidence"] = max(r["confidence"], 0.9)
            elif state == "invalidated":
                r["confidence"] = min(r["confidence"], 0.05)
            elif state == "supported":
                r["confidence"] = min(0.85, r["confidence"] + 0.1)
            self._save()
            return dict(r)

    def supersede(self, aid: str, new_text: str,
                  **kw: Any) -> dict | None:
        """Replace an assumption with a refined one — the old row is
        marked superseded and links to its replacement."""
        with self._lock:
            old = self._row(aid)
            if old is None:
                return None
        new = self.add(new_text,
                       scope_type=old["scope"]["type"],
                       scope_id=old["scope"]["id"],
                       dependents=old.get("dependents"),
                       confidence=kw.get("confidence",
                                         old.get("confidence", 0.5)),
                       test=kw.get("test", ""))
        with self._lock:
            old["state"] = "superseded"
            old["superseded_by"] = new["id"]
            old["updated_at"] = time.time()
            old["history"].append({"ts": time.time(),
                                   "state": "superseded",
                                   "detail": f"replaced by {new['id']}"})
            self._save()
        return dict(new)

    def link(self, aid: str, kind: str, ref_id: str) -> dict | None:
        """Register additional work that depends on this assumption."""
        with self._lock:
            r = self._row(aid)
            if r is None:
                return None
            deps = r.setdefault("dependents", {})
            deps.setdefault(kind, [])
            if ref_id not in deps[kind]:
                deps[kind].append(str(ref_id))
                deps[kind] = deps[kind][-30:]
            r["updated_at"] = time.time()
            self._save()
            return dict(r)

    # ------------------------------------------------------------------
    def invalidate(self, aid: str, *, evidence: str = ""
                   ) -> dict | None:
        """Mark the assumption false and report what depended on it —
        the targeted re-evaluation list, not a blind rebuild."""
        row = self.set_state(aid, "invalidated", evidence=evidence)
        if row is None:
            return None
        affected = {k: v for k, v in (row.get("dependents") or {}).items()
                    if v}
        return {"invalidated": row, "dependents": affected}

    # -- queries -------------------------------------------------------

    def list(self, *, scope_type: str = "", scope_id: str = "",
             state: str = "", limit: int = 100) -> list[dict]:
        with self._lock:
            rows = [dict(r) for r in self.data["assumptions"]
                    if (not scope_type or r["scope"]["type"] == scope_type)
                    and (not scope_id or r["scope"]["id"] == scope_id)
                    and (not state or r.get("state") == state)]
        return rows[-limit:]

    def weakest(self, *, scope_type: str = "", scope_id: str = "",
                limit: int = 5) -> list[dict]:
        """Unknown-unknown probe: live assumptions ranked by least
        evidence — the targeted-investigation list before a decision."""
        live = self.list(scope_type=scope_type, scope_id=scope_id)
        live = [r for r in live
                if r.get("state") in ("untested", "supported")]
        live.sort(key=lambda r: (len(r.get("evidence") or []),
                                 r.get("confidence", 0.5)))
        return live[:limit]

    def summary(self) -> dict:
        with self._lock:
            rows = list(self.data["assumptions"])
        by_state: dict[str, int] = {}
        for r in rows:
            s = r.get("state") or "?"
            by_state[s] = by_state.get(s, 0) + 1
        return {"total": len(rows), "by_state": by_state,
                "untested": by_state.get("untested", 0),
                "invalidated": by_state.get("invalidated", 0)}
