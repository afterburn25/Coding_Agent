"""Causal memory — mechanism-level records, not just symptoms.

Each record captures a resolved failure's full causal chain:

    symptom  →  root cause  →  mechanism  →  fix  →  verification

When a similar symptom appears later, `priors()` returns ranked
hypothesis dicts shaped like `Diagnostician` output — seeded *as
candidates to be tested*, never as assumed causes. Confidence scales
with symptom similarity and how often that cause has held up, so a
single old record can never outrank fresh deterministic evidence.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import Any

from .fsutil import atomic_write_text

_TOKEN_RE = re.compile(r"[a-z0-9_]+")


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall((text or "").lower()))


def _similarity(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


_HISTORY_BOUND = 400


class CausalMemory:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.data = json.loads(
                self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {"version": 1, "records": []}
        self.data.setdefault("records", [])

    def _save(self) -> None:
        atomic_write_text(
            self.path,
            json.dumps(self.data, indent=2, ensure_ascii=False,
                       default=str))

    # ------------------------------------------------------------------
    def record(self, symptom: str, *, root_cause: str, mechanism: str,
               fix: str | list = "", verification: str = "",
               subsystem: str = "", incident_id: str = "",
               success: bool = True) -> dict:
        """Store a causal chain. `fix` may be the repair-procedure step
        list; it is flattened to a bounded string for retrieval."""
        fix_s = "; ".join(str(s)[:120] for s in fix[:8]) \
            if isinstance(fix, list) else str(fix)[:500]
        row = {
            "id": f"caus-{uuid.uuid4().hex[:10]}",
            "symptom": str(symptom)[:500],
            "root_cause": str(root_cause)[:200],
            "mechanism": str(mechanism)[:600],
            "fix": fix_s,
            "verification": str(verification)[:400],
            "subsystem": str(subsystem)[:80],
            "incident_id": str(incident_id)[:60],
            "success": bool(success),
            "recurrence_hits": 0,
            "ts": time.time(),
        }
        self.data["records"].append(row)
        self.data["records"] = self.data["records"][-_HISTORY_BOUND:]
        self._save()
        return dict(row)

    def get(self, rid: str) -> dict | None:
        for r in self.data["records"]:
            if r.get("id") == rid:
                return dict(r)
        return None

    def list(self, *, subsystem: str = "", limit: int = 50) -> list[dict]:
        rows = [r for r in self.data["records"]
                if not subsystem or r.get("subsystem") == subsystem]
        return [dict(r) for r in rows[-limit:]]

    # ------------------------------------------------------------------
    def priors(self, symptom: str, *, subsystem: str = "",
               limit: int = 3) -> list[dict]:
        """Rank past causal records by symptom similarity — returned as
        hypothesis-shaped dicts (kind/detail/confidence/evidence) so the
        diagnostician's output can merge them directly. Prior causes are
        ranked candidates, NOT assumed answers."""
        scored = []
        for r in self.data.get("records", []):
            sim = _similarity(symptom,
                              f"{r.get('symptom','')} "
                              f"{r.get('root_cause','')}")
            if subsystem and r.get("subsystem") == subsystem:
                sim = min(1.0, sim + 0.15)
            if sim < 0.35:
                continue
            # Reliability: each prior recurrence that held adds trust;
            # a record that later failed pulls the prior down.
            base = 0.55 if r.get("success") else 0.3
            conf = round(min(0.85, sim * base
                             + min(int(r.get("recurrence_hits") or 0),
                                   5) * 0.02), 3)
            scored.append((conf, r, sim))
        scored.sort(key=lambda t: -t[0])
        out = []
        for conf, r, sim in scored[:limit]:
            r["recurrence_hits"] = int(r.get("recurrence_hits") or 0) + 1
            out.append({
                "kind": r.get("root_cause") or "prior",
                "detail": f"Prior incident {r.get('incident_id') or r.get('id')}: "
                          f"{r.get('mechanism', '')[:160]}",
                "confidence": conf,
                "evidence": [f"similar symptom (sim={sim:.2f})",
                             f"prior fix: {r.get('fix','')[:120]}"],
                "repair_kind": "",
                "source": "causal_memory",
                "causal_id": r.get("id"),
            })
        if out:
            self._save()
        return out

    def summary(self) -> dict:
        rows = self.data.get("records", [])
        return {"total": len(rows),
                "resolved": sum(1 for r in rows if r.get("success")),
                "failed": sum(1 for r in rows if not r.get("success"))}
