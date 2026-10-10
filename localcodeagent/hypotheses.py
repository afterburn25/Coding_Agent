"""Hypothesis engine — first-class, persisted candidate explanations.

A hypothesis is never assumed: it carries a lifecycle, evidence for and
against, and (when known) a discriminating test — a cheap check that
splits competing explanations apart before any repair is attempted.

Lifecycle: proposed → testing → supported | weakened → confirmed |
rejected. Rejected hypotheses may be re-proposed (new evidence);
confirmed/rejected are otherwise terminal.

Every mutation appends evidence and persists — a hypothesis row is an
audit trail, not a scratch note.
"""
from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path
from typing import Any


HYP_STATUSES = {"proposed", "testing", "supported", "weakened",
                "confirmed", "rejected"}
_OPEN = {"proposed", "testing", "supported", "weakened"}
_TERMINAL = {"confirmed", "rejected"}

_HISTORY_BOUND = 400


class HypothesisStore:
    """Durable hypothesis rows keyed by incident/mission scope."""

    def __init__(self, path: Path, db: Any = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        from .state_db import DocStore
        self._doc = DocStore(db, self.path, domain="hypotheses")
        self.data = self._doc.load_json({"version": 1, "hypotheses": []})
        if not isinstance(self.data, dict):
            self.data = {"version": 1, "hypotheses": []}
        self.data.setdefault("hypotheses", [])

    def _save(self) -> None:
        self._doc.save_json(self.data)

    # -- reads --------------------------------------------------------
    def get(self, hid: str) -> dict | None:
        for h in self.data["hypotheses"]:
            if h.get("id") == hid:
                return dict(h)
        return None

    def list(self, *, incident_id: str = "", mission_id: str = "",
             status: str = "", open_only: bool = False) -> list[dict]:
        rows = self.data["hypotheses"]
        out = [dict(h) for h in rows
               if (not incident_id or h.get("incident_id") == incident_id)
               and (not mission_id or h.get("mission_id") == mission_id)
               and (not status or h.get("status") == status)
               and (not open_only or h.get("status") in _OPEN)]
        out.sort(key=lambda h: -float(h.get("confidence") or 0))
        return out

    def summary(self) -> dict:
        rows = self.data["hypotheses"]
        by = {s: 0 for s in HYP_STATUSES}
        for h in rows:
            by[h.get("status", "proposed")] = by.get(
                h.get("status", "proposed"), 0) + 1
        return {"total": len(rows), "open": sum(by[s] for s in _OPEN),
                "by_status": by}

    # -- mutations ----------------------------------------------------
    def propose(self, statement: str, *, kind: str = "",
                confidence: float = 0.3, source: str = "analysis",
                incident_id: str = "", mission_id: str = "",
                test: dict | None = None,
                evidence_for: list | None = None) -> dict:
        now = time.time()
        h = {
            "id": f"hyp-{uuid.uuid4().hex[:10]}",
            "statement": str(statement)[:500],
            "kind": str(kind)[:80],
            "confidence": round(min(max(float(confidence), 0.0), 0.99), 3),
            "source": str(source)[:60],
            "incident_id": str(incident_id)[:60],
            "mission_id": str(mission_id)[:60],
            "status": "proposed",
            # The discriminating test — a check whose outcome separates
            # this explanation from its competitors. `distinguishes`
            # lists which OTHER hypothesis kinds a result rules out.
            "test": dict(test) if isinstance(test, dict) else {},
            "test_result": {},
            "evidence_for": [str(e)[:300]
                             for e in (evidence_for or [])][:20],
            "evidence_against": [],
            "created_at": now, "updated_at": now,
            "history": [{"ts": now, "event": "proposed",
                         "detail": str(statement)[:200]}],
        }
        with self._lock:
            self.data["hypotheses"].append(h)
            self.data["hypotheses"] = self.data["hypotheses"][
                -_HISTORY_BOUND:]
            self._save()
        return dict(h)

    def upsert_for_incident(self, incident_id: str,
                            hypotheses: list[dict]) -> list[dict]:
        """Mirror diagnostician output into first-class rows, deduped by
        (incident, kind). Existing open rows keep their evidence."""
        out = []
        with self._lock:
            existing = {(h.get("incident_id"), h.get("kind")): h
                        for h in self.data["hypotheses"]
                        if h.get("status") in _OPEN}
            for hyp in hypotheses[:8]:
                kind = str(hyp.get("kind") or "")
                key = (incident_id, kind)
                if key in existing:
                    h = existing[key]
                    # Fresh diagnosis strengthens or weakens the row.
                    h["confidence"] = round(min(
                        max(float(hyp.get("confidence") or 0), 0.0),
                        0.99), 3)
                    h["updated_at"] = time.time()
                    out.append(dict(h))
                    continue
                row = self.propose(
                    str(hyp.get("detail") or hyp.get("kind") or ""),
                    kind=kind,
                    confidence=float(hyp.get("confidence") or 0.3),
                    source=str(hyp.get("source") or "diagnostician"),
                    incident_id=incident_id,
                    evidence_for=[str(e) for e in
                                  (hyp.get("evidence") or [])])
                existing[key] = self.get(row["id"]) or row
                out.append(row)
            self._save()
        return out

    def _mutate(self, hid: str, fn) -> dict | None:
        with self._lock:
            for h in self.data["hypotheses"]:
                if h.get("id") == hid:
                    fn(h)
                    h["updated_at"] = time.time()
                    hist = h.setdefault("history", [])
                    hist.append({"ts": h["updated_at"],
                                 "event": h["status"],
                                 "detail": str(fn.__doc__ or "")[:200]})
                    del hist[:-80]
                    self._save()
                    return dict(h)
        return None

    def set_status(self, hid: str, status: str, *,
                   detail: str = "") -> dict | None:
        if status not in HYP_STATUSES:
            return None
        def _f(h):
            h["status"] = status
        _f.__doc__ = detail or status
        return self._mutate(hid, _f)

    def add_evidence(self, hid: str, *, supporting: bool,
                     detail: str, ref: str = "") -> dict | None:
        """Evidence moves confidence and, while open, status:
        supporting → supported once confidence ≥0.6; against → weakened."""
        def _f(h):
            entry = {"detail": str(detail)[:300],
                     "ref": str(ref)[:200], "ts": time.time()}
            if supporting:
                h.setdefault("evidence_for", []).append(entry)
                h["evidence_for"] = h["evidence_for"][-20:]
                h["confidence"] = round(
                    min(0.99, float(h.get("confidence") or 0) + 0.08), 3)
                if h.get("status") in _OPEN \
                        and h["confidence"] >= 0.6:
                    h["status"] = "supported"
            else:
                h.setdefault("evidence_against", []).append(entry)
                h["evidence_against"] = \
                    h["evidence_against"][-20:]
                h["confidence"] = round(
                    max(0.01, float(h.get("confidence") or 0) - 0.10), 3)
                if h.get("status") in _OPEN:
                    h["status"] = "weakened"
        _f.__doc__ = f"evidence: {'for' if supporting else 'against'}"
        return self._mutate(hid, _f)

    def record_test(self, hid: str, *, passed: bool,
                    output: str = "", test_name: str = "") -> dict | None:
        """Persist a discriminating-test outcome. Pass → supported
        (confidence bump); fail → weakened. The result stays on the row
        so later investigations can see *why* it moved."""
        def _f(h):
            h["test_result"] = {
                "ran_at": time.time(), "passed": bool(passed),
                "test": str(test_name or h.get("test", {}).get("name")
                          or "")[:200],
                "output": str(output)[:1000]}
            if h.get("status") in _OPEN:
                if passed:
                    h["status"] = "supported"
                    h["confidence"] = round(
                        min(0.99, float(h.get("confidence") or 0)
                            + 0.12), 3)
                else:
                    h["status"] = "weakened"
                    h["confidence"] = round(
                        max(0.01, float(h.get("confidence") or 0)
                            - 0.15), 3)
        _f.__doc__ = f"test {'passed' if passed else 'failed'}"
        return self._mutate(hid, _f)

    def confirm(self, hid: str, *, evidence: str = "") -> dict | None:
        def _f(h):
            h["status"] = "confirmed"
            h["confidence"] = max(float(h.get("confidence") or 0), 0.9)
            if evidence:
                h.setdefault("evidence_for", []).append(
                    {"detail": str(evidence)[:300],
                     "ref": "resolution", "ts": time.time()})
        _f.__doc__ = "confirmed"
        return self._mutate(hid, _f)

    def reject(self, hid: str, *, reason: str = "") -> dict | None:
        def _f(h):
            h["status"] = "rejected"
            h["confidence"] = min(float(h.get("confidence") or 0), 0.1)
            if reason:
                h.setdefault("evidence_against", []).append(
                    {"detail": str(reason)[:300],
                     "ref": "resolution", "ts": time.time()})
        _f.__doc__ = "rejected"
        return self._mutate(hid, _f)

    # -- discriminating tests -----------------------------------------
    def pick_discriminating(self, incident_id: str = "") -> dict | None:
        """Choose the open hypothesis whose declared test discriminates
        the most competitors — the cheapest way to shrink the space
        before any repair is attempted."""
        open_rows = [h for h in self.list(incident_id=incident_id,
                                          open_only=True)
                     if (h.get("test") or {}).get("name")]
        if not open_rows:
            return None
        best, best_score = None, -1
        for h in open_rows:
            rules_out = set((h.get("test") or {}).get("distinguishes")
                            or [])
            others = {r.get("kind") for r in open_rows if r["id"]
                      != h["id"]}
            score = len(rules_out & others) \
                + len(rules_out - others) * 0.5
            if score > best_score:
                best, best_score = h, score
        return best
