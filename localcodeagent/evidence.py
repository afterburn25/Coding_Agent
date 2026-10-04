"""Shared evidence board — workers publish structured findings, others
retrieve them without inheriting whole conversations.

Entries carry a `kind` (file/test_result/log/telemetry/web/screenshot/
benchmark/git/finding/hypothesis/question), a `claim`, provenance
(`source` worker/agent), `confidence`, and refs. When two entries
disagree, `contradict()` links them — both are marked, neither is
silently preferred, and `resolve()` records *how* the contradiction was
settled. Unresolved questions stay visible instead of evaporating.
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .fsutil import atomic_write_text

EVIDENCE_KINDS = {
    "file", "test_result", "log", "telemetry", "web", "screenshot",
    "benchmark", "git", "finding", "hypothesis", "question",
    "note",
}
ENTRY_STATUSES = {"open", "confirmed", "contradicted", "resolved",
                  "withdrawn"}

_TOKEN_RE = re.compile(r"[a-z0-9_]+")
_HISTORY_BOUND = 500


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall((text or "").lower()))


class EvidenceBoard:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        try:
            self.data = json.loads(
                self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {"version": 1, "entries": []}
        self.data.setdefault("entries", [])

    def _save(self) -> None:
        atomic_write_text(
            self.path,
            json.dumps(self.data, indent=2, ensure_ascii=False,
                       default=str))

    def _row(self, eid: str) -> dict | None:
        for e in self.data["entries"]:
            if e.get("id") == eid:
                return e
        return None

    # -- publish ------------------------------------------------------
    def post(self, claim: str, *, kind: str = "note",
             source: str = "", confidence: float = 0.5,
             refs: list | None = None, mission_id: str = "",
             tags: list | None = None,
             contradicts: str = "") -> dict:
        now = time.time()
        e = {
            "id": f"ev-{uuid.uuid4().hex[:10]}",
            "kind": kind if kind in EVIDENCE_KINDS else "note",
            "claim": str(claim)[:600],
            "source": str(source)[:80],
            "confidence": round(min(max(float(confidence), 0.0), 1.0), 3),
            "refs": [str(r)[:300] for r in (refs or [])][:12],
            "tags": [str(t)[:40] for t in (tags or [])][:12],
            "mission_id": str(mission_id)[:60],
            "status": "open",
            "contradictions": [],      # [{with, note, ts}]
            "resolution": "",
            "created_at": now, "updated_at": now,
        }
        with self._lock:
            self.data["entries"].append(e)
            self.data["entries"] = self.data["entries"][-_HISTORY_BOUND:]
            self._save()
        if contradicts:
            self.contradict(e["id"], contradicts)
        return dict(e)

    # -- contradiction handling ---------------------------------------
    def contradict(self, eid: str, other_id: str,
                   note: str = "") -> dict | None:
        """Link two disagreeing entries. Both become `contradicted` —
        the disagreement is surfaced, never silently resolved."""
        with self._lock:
            a, b = self._row(eid), self._row(other_id)
            if a is None or b is None or eid == other_id:
                return None
            link_a = {"with": other_id, "note": str(note)[:300],
                      "ts": time.time()}
            link_b = {"with": eid, "note": str(note)[:300],
                      "ts": time.time()}
            for ent, link in ((a, link_a), (b, link_b)):
                if all(c.get("with") != link["with"]
                       for c in ent.setdefault("contradictions", [])):
                    ent["contradictions"].append(link)
                ent["status"] = "contradicted"
                ent["updated_at"] = time.time()
            self._save()
            return dict(a)

    def resolve(self, eid: str, *, resolution: str,
                winner_id: str = "", resolver: str = "") -> dict | None:
        """Settle a contradicted entry. The winner (if given) is
        confirmed; the entry carries the resolution rationale so the
        *how* is auditable. Linked contradictions clear on both sides."""
        with self._lock:
            e = self._row(eid)
            if e is None:
                return None
            e["status"] = "confirmed" if winner_id == eid else "resolved"
            e["resolution"] = str(resolution)[:400]
            e["resolved_by"] = str(resolver)[:80]
            e["updated_at"] = time.time()
            linked = [c.get("with") for c in
                      e.get("contradictions") or []]
            e["contradictions"] = []
            for other_id in linked:
                other = self._row(other_id)
                if other is None:
                    continue
                other["contradictions"] = [
                    c for c in other.get("contradictions", [])
                    if c.get("with") != eid]
                if other_id == winner_id:
                    other["status"] = "confirmed"
                elif not other["contradictions"]:
                    other["status"] = "open"
                other["updated_at"] = time.time()
            if winner_id and winner_id != eid \
                    and self._row(winner_id):
                # Winner becomes confirmed unless the caller is
                # resolving that entry itself (handled above).
                self._row(winner_id)["status"] = "confirmed"
                self._row(winner_id)["resolution"] = str(
                    resolution)[:400]
                self._row(winner_id)["updated_at"] = time.time()
            self._save()
            return dict(e)

    def withdraw(self, eid: str, *, reason: str = "") -> dict | None:
        with self._lock:
            e = self._row(eid)
            if e is None:
                return None
            e["status"] = "withdrawn"
            e["resolution"] = str(reason)[:400]
            e["updated_at"] = time.time()
            self._save()
            return dict(e)

    # -- retrieval ----------------------------------------------------
    def get(self, eid: str) -> dict | None:
        r = self._row(eid)
        return dict(r) if r else None

    def search(self, query: str = "", *, kinds: list | None = None,
               mission_id: str = "", status: str = "",
               limit: int = 20) -> list[dict]:
        """Token-overlap retrieval — workers pull *relevant* entries
        rather than whole boards."""
        q = _tokens(query)
        kset = set(kinds or [])
        out = []
        for e in self.data["entries"]:
            if kset and e.get("kind") not in kset:
                continue
            if mission_id and e.get("mission_id") != mission_id:
                continue
            if status and e.get("status") != status:
                continue
            score = 0.0
            if q:
                hay = _tokens(f"{e.get('claim','')} "
                              f"{' '.join(e.get('tags') or [])} "
                              f"{e.get('source','')}")
                inter = len(q & hay)
                if not inter:
                    continue
                score = inter / len(q | hay)
            out.append((score, e))
        out.sort(key=lambda t: (-t[0], -t[1].get("updated_at", 0)))
        return [dict(e) for _, e in out[:limit]]

    def list(self, *, mission_id: str = "", status: str = "",
             limit: int = 100) -> list[dict]:
        rows = [e for e in self.data["entries"]
                if (not mission_id or e.get("mission_id") == mission_id)
                and (not status or e.get("status") == status)]
        return [dict(e) for e in rows[-limit:]]

    def contradictions(self) -> list[dict]:
        return [dict(e) for e in self.data["entries"]
                if e.get("status") == "contradicted"]

    def questions(self) -> list[dict]:
        return [dict(e) for e in self.data["entries"]
                if e.get("kind") == "question"
                and e.get("status") in {"open", "contradicted"}]

    def summary(self) -> dict:
        rows = self.data["entries"]
        by = {s: 0 for s in ENTRY_STATUSES}
        kinds: dict[str, int] = {}
        for e in rows:
            by[e.get("status", "open")] = by.get(
                e.get("status", "open"), 0) + 1
            kinds[e.get("kind", "note")] = kinds.get(
                e.get("kind", "note"), 0) + 1
        return {"total": len(rows), "by_status": by,
                "by_kind": kinds,
                "open_questions": len(self.questions()),
                "contradictions": len(self.contradictions())}
