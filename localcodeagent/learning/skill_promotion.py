"""Experience → skill promotion (Parts 36–37).

A procedure that keeps verifying across varied episodes becomes a SKILL
CANDIDATE. Promotion to a real installable SkillRegistry package always
requires explicit user approval — one accidental workflow never becomes
permanent automation silently.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path

from .procedures import STATUS_VERIFIED

# Procedure must be verified AND succeed this often across episodes.
_SKILL_MIN_SUCCESSES = 5

_NAME_RE = re.compile(r"[^a-z0-9-]+")


def _skill_name(text: str) -> str:
    name = _NAME_RE.sub("-", (text or "").lower()).strip("-")[:48]
    return name or f"learned-skill-{uuid.uuid4().hex[:6]}"


class SkillPromotionEngine:
    def __init__(self, path: Path, *, procedures, registry=None,
                 db: Any = None) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.procedures = procedures
        self.registry = registry   # SkillRegistry — set post-construction
        self._lock = threading.RLock()
        from ..state_db import DocStore
        self._doc = DocStore(db, self.path, domain="learning")
        self._data: dict = {"version": 1, "proposals": []}
        self._load()

    def _load(self) -> None:
        raw = self._doc.load_json(None)
        if isinstance(raw, dict) and isinstance(raw.get("proposals"), list):
            self._data["proposals"] = raw["proposals"]

    def _save(self) -> None:
        self._doc.save_json(self._data)

    def eligible(self) -> list[dict]:
        """Verified procedures with enough success to propose as skills."""
        out = []
        for p in self.procedures.list(status=STATUS_VERIFIED):
            if (p.get("success_count") or 0) >= _SKILL_MIN_SUCCESSES:
                out.append(p)
        return out

    def propose(self, procedure_id: str, *,
                description: str = "") -> dict | None:
        """Stage a skill candidate for user approval."""
        proc = self.procedures.get(procedure_id)
        if not proc or proc.get("status") != STATUS_VERIFIED:
            return None
        if (proc.get("success_count") or 0) < _SKILL_MIN_SUCCESSES:
            return None
        with self._lock:
            for pr in self._data["proposals"]:
                if pr.get("procedure_id") == procedure_id and \
                        pr.get("status") == "pending":
                    return dict(pr)
            steps = proc.get("steps") or []
            instructions = [
                f"# {proc.get('name')}",
                "",
                f"Use this learned procedure for: {proc.get('problem_signature')}",
                "",
                "## Steps",
                *[f"{i+1}. {s}" for i, s in enumerate(steps)],
            ]
            if proc.get("verification"):
                instructions += ["", "## Verification",
                                 *[f"- {v}" for v in proc["verification"]]]
            spec = {
                "name": _skill_name(str(proc.get("name") or "")),
                "version": "1.0.0",
                "description": (description or
                                f"Learned procedure: {proc.get('problem_signature')}")[:300],
                "author": "nexus-learning",
                "capabilities": ["learned-procedure"],
                "permissions": [],      # instructions only — no broad grants
                "instructions": "\n".join(instructions)[:4000],
                "examples": [],
            }
            proposal = {
                "id": f"scand-{uuid.uuid4().hex[:10]}",
                "procedure_id": procedure_id,
                "spec": spec,
                "status": "pending",
                "created": time.time(),
            }
            self._data["proposals"].append(proposal)
            self._save()
            return dict(proposal)

    def _row(self, cid: str) -> dict | None:
        for pr in self._data["proposals"]:
            if pr.get("id") == cid:
                return pr
        return None

    def approve(self, candidate_id: str, staging_dir: Path) -> dict:
        """User-approved promotion → write package → install via the
        real SkillRegistry (permission-gated there too)."""
        with self._lock:
            prop = self._row(candidate_id)
            if not prop or prop.get("status") != "pending":
                return {"ok": False, "error": "no pending proposal"}
            prop = dict(prop)
        if self.registry is None:
            return {"ok": False, "error": "skill registry unavailable"}
        spec = dict(prop["spec"])
        stage = Path(staging_dir) / spec["name"]
        stage.mkdir(parents=True, exist_ok=True)
        (stage / "skill.json").write_text(
            json.dumps(spec, indent=2, ensure_ascii=False), encoding="utf-8")
        out = self.registry.install(stage)
        with self._lock:
            row = self._row(candidate_id)
            if row is not None:
                row["status"] = "installed" if out.get("ok") else "failed"
                row["install_result"] = out
                self._save()
        return out

    def reject(self, candidate_id: str) -> dict | None:
        with self._lock:
            row = self._row(candidate_id)
            if not row:
                return None
            row["status"] = "rejected"
            self._save()
            return dict(row)

    def pending(self) -> list[dict]:
        with self._lock:
            return [dict(p) for p in self._data["proposals"]
                    if p.get("status") == "pending"]

    def summary(self) -> dict:
        with self._lock:
            rows = list(self._data["proposals"])
        return {"proposals": len(rows),
                "pending": sum(1 for p in rows if p.get("status") == "pending"),
                "installed": sum(1 for p in rows if p.get("status") == "installed"),
                "eligible": len(self.eligible())}
