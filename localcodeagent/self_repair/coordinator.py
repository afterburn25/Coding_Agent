"""SelfRepairCoordinator — the bounded state machine that carries an
incident from detection to resolution, needs_human, or rollback.

Pipeline per the self-repair contract:

    detected → collecting → localizing → diagnosing → planning
             → patching → testing → reviewing → canary → promoting
             → resolved | rolled_back | needs_human | abandoned

Two repair paths:

- **operational** — deterministic fixers (restart, quarantine+rebuild,
  port reclaim, config blacklist…) registered by server wiring. These
  run directly, are fully testable, and are preferred whenever the
  diagnosis says the fault is environmental/config/runtime-state.

- **code** — a candidate patch is generated inside an isolated git
  worktree by an injected `patch_generator` (production: the coding
  model via AgentOrchestrator; tests: a fault-injection fake), verified
  by targeted + regression test runs in the worktree, independently
  reviewed, optionally canaried, then promoted only when every gate
  passes and `auto_promote`/permissions allow. Otherwise the fully
  diagnosed candidate waits at `needs_human` with all evidence attached.

Everything is injected: no LLM, no subprocess, and no live service is
required to drive the state machine in tests.
"""
from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .models import (REPAIR_STATES, OPEN_REPAIR_STATES,
                     TERMINAL_REPAIR_STATES, new_incident, transition,
                     budget_exceeded)
from .detector import Detector
from .localizer import Localizer
from .diagnosis import Diagnostician
from .repair_memory import RepairMemory
from .patcher import Patcher
from .verifier import run_unittest, targeted_tests_for
from .rollback import Rollback
from .canary import Canary

# Secret-looking fragments are stripped before any incident field or
# log excerpt is persisted or shown to a model.
_SECRET_RX = re.compile(
    r"(sk-[A-Za-z0-9_\-]{8,}|Bearer\s+\S+|"
    r"(?:api[_-]?key|token|secret|password|passwd)\s*[:=]\s*\S+)",
    re.I)


def redact(text: str) -> str:
    return _SECRET_RX.sub("[redacted]", str(text))


class SelfRepairCoordinator:
    def __init__(self, store, repo_root: Path, *,
                 state_root: Path | None = None,
                 localizer: Localizer | None = None,
                 diagnostician: Diagnostician | None = None,
                 fixers: dict[str, Callable[[dict], dict]] | None = None,
                 patch_generator: Callable[[dict, Path], dict] | None = None,
                 mission_status: Callable[[str], str] | None = None,
                 reviewer: Callable[[dict, Path, str], dict] | None = None,
                 canary: Canary | None = None,
                 collectors: dict[str, Callable[[], Any]] | None = None,
                 verify_timeout_s: float = 300.0,
                 regression_suite: str | None = None,
                 auto_promote: bool = False,
                 commit_on_promote: bool = True,
                 canary_required_for: set[str] | None = None,
                 min_promote_confidence: float = 0.6,
                 is_blocked: Callable[[], bool] | None = None,
                 audit: Callable[..., None] | None = None,
                 emit: Callable[[dict], None] | None = None,
                 notify: Callable[[str, str, str], None] | None = None,
                 on_resumed: Callable[[dict], None] | None = None,
                 on_resolved: Callable[[dict], None] | None = None,
                 researcher: Callable[[dict], dict | None] | None = None,
                 eval_recorder: Callable[[dict], None] | None = None,
                 hypotheses: Any = None,
                 causal: Any = None,
                 decisions: Any = None) -> None:
        self._store = store                     # AutonomyStore
        self.repo_root = Path(repo_root)
        self.state_root = Path(state_root or
                               getattr(store, "root", repo_root))
        self.localizer = localizer or Localizer(self.repo_root)
        self.diagnostician = diagnostician or Diagnostician()
        self.memory = RepairMemory(
            self.state_root / "repair_memory.json",
            db=getattr(store, "db", None))
        # Phase-2 cognitive stores — hypothesis lifecycle, causal
        # memory, decision journal. All optional: an unwired coordinator
        # behaves exactly as before.
        self.hypotheses = hypotheses
        self.causal = causal
        self.decisions = decisions
        self.patcher = Patcher(self.repo_root)
        self.rollback = Rollback(self.repo_root, self.state_root)
        self.canary = canary or Canary()
        self.fixers = dict(fixers or {})
        self.patch_generator = patch_generator
        self._mission_status = mission_status
        self.reviewer = reviewer or self._default_review
        self.collectors = dict(collectors or {})
        self.verify_timeout_s = verify_timeout_s
        self.regression_suite = regression_suite   # e.g. "tests"
        self.auto_promote = bool(auto_promote)
        self.commit_on_promote = bool(commit_on_promote)
        self.canary_required_for = canary_required_for or set()
        self.min_promote_confidence = float(min_promote_confidence)
        self._is_blocked = is_blocked or (lambda: False)
        self._audit = audit or (lambda kind, **kw: None)
        self._emit = emit or (lambda payload: None)
        self._notify = notify or (lambda level, title, detail: None)
        self._on_resumed = on_resumed
        # Post-resolution hook — the server sweeps stale error evidence
        # (crash history, failure telemetry) so fixed faults can't
        # re-trigger the detectors that filed this incident.
        self._on_resolved = on_resolved
        # External evidence gatherer — consulted when diagnosis is weak
        # (unknown hypothesis or low confidence). Attached as evidence;
        # never fabricates a hypothesis or inflates confidence.
        self._researcher = researcher
        # Evaluation Lab sink — terminal incidents record their
        # verification evidence as eval runs so repairs are comparable
        # over time (suite="self_repair", subject=signature).
        self._eval_recorder = eval_recorder
        self._lock = threading.RLock()
        self.detector = Detector(self._rows)

    # ------------------------------------------------------------------
    # storage

    def _rows(self) -> list[dict]:
        return self._store.repairs.data.setdefault("repairs", [])

    def _save(self) -> None:
        self._store.repairs.save()

    def get(self, incident_id: str) -> dict | None:
        for r in self._rows():
            if r.get("id") == incident_id:
                return r
        return None

    def list(self, *, include_terminal: bool = True) -> list[dict]:
        with self._lock:
            rows = [dict(r) for r in self._rows()
                    if include_terminal
                    or r.get("state") not in TERMINAL_REPAIR_STATES]
        rows.sort(key=lambda r: -(r.get("updated_at") or 0))
        return rows

    def summary(self) -> dict[str, Any]:
        rows = self.list()
        by_state: dict[str, int] = {}
        for r in rows:
            s = str(r.get("state"))
            by_state[s] = by_state.get(s, 0) + 1
        return {"incidents": len(rows), "by_state": by_state,
                "open": sum(1 for r in rows
                            if r.get("state") in OPEN_REPAIR_STATES),
                "auto_resolved": sum(
                    1 for r in rows if r.get("state") == "resolved")}

    # ------------------------------------------------------------------
    # intake

    def report_failure(self, *, source: str, error_message: str,
                       exc_type: str = "", subsystem: str = "",
                       stack_trace: str = "", **kw) -> tuple[dict | None, str]:
        """Main entry point — every subsystem funnels failures here."""
        incident, disp = self.detector.ingest(
            source=source, error_message=redact(error_message),
            exc_type=exc_type, subsystem=subsystem,
            stack_trace=redact(stack_trace), **kw)
        if incident is None:
            return None, disp
        with self._lock:
            if disp == "new":
                self._rows().append(incident)
            self._save()
        self._audit("repair_" + disp, incident=incident["id"],
                    signature=incident["signature"],
                    severity=incident["severity"])
        self._emit({"type": "repair_incident", "incident_id": incident["id"],
                    "disposition": disp, "severity": incident["severity"]})
        if incident["severity"] == "critical":
            self._notify("failure",
                         f"Critical failure: {incident['error_class']}",
                         incident["error_message"][:300])
        return incident, disp

    # ------------------------------------------------------------------
    # stage drivers

    def _set(self, inc: dict, state: str, detail: str = "") -> dict:
        transition(inc, state, detail=detail)
        inc["updated_at"] = time.time()
        with self._lock:
            self._save()
        self._emit({"type": "repair_state", "incident_id": inc["id"],
                    "state": state})
        if state in {"resolved", "needs_human", "rolled_back",
                     "abandoned"}:
            self._record_eval(inc)
        return inc

    def _record_eval(self, inc: dict) -> None:
        """Once per incident: hand terminal verification evidence to the
        eval recorder. Never blocks a transition on a recorder error."""
        if self._eval_recorder is None or inc.get("_eval_recorded"):
            return
        inc["_eval_recorded"] = True
        try:
            self._eval_recorder(dict(inc))
        except Exception:
            pass

    def _fail_open(self, inc: dict, reason: str) -> None:
        """Not enough evidence/permission to continue autonomously."""
        inc["needs_human_reason"] = reason[:300]
        self._set(inc, "needs_human", reason)
        self._notify("important",
                     f"Repair needs review: {inc['error_class']}",
                     reason[:200])

    def _budget_check(self, inc: dict) -> bool:
        name = budget_exceeded(inc)
        if name:
            inc["needs_human_reason"] = f"budget exceeded: {name}"
            self._set(inc, "needs_human", f"budget exceeded: {name}")
            return True
        return False

    # ------------------------------------------------------------------
    # stage 1 — evidence

    def _stage_collect(self, inc: dict) -> str:
        self._set(inc, "collecting")
        snap = {}
        for name, fn in self.collectors.items():
            try:
                snap[name] = fn()
            except Exception as exc:
                snap[name] = {"error": str(exc)[:120]}
        for k in ("logs", "crash_history"):
            if k in snap:
                inc["logs"] = redact(str(snap[k]))[:4000]
        if "hardware" in snap:
            inc["hardware_snapshot"] = snap["hardware"]
        if "runtime" in snap:
            inc["runtime_snapshot"] = snap["runtime"]
        try:
            out = self.patcher._git("log", "-8", "--oneline",
                                    "--pretty=format:%h %s").stdout
            inc["recent_commits"] = [
                l.strip() for l in out.splitlines() if l.strip()][:8]
        except Exception:
            pass
        return "localizing"

    def _stage_localize(self, inc: dict) -> str:
        self._set(inc, "localizing")
        inc["suspects"] = self.localizer.localize(inc)
        inc["affected_files"] = [s["path"] for s in inc["suspects"]]
        return "diagnosing"

    def _stage_diagnose(self, inc: dict) -> str:
        self._set(inc, "diagnosing")
        inc["attempts"]["diagnosis"] = \
            int(inc["attempts"].get("diagnosis") or 0) + 1
        diag = self.diagnostician.diagnose(inc)
        inc["hypotheses"] = diag["hypotheses"]
        # Prior causal records seed ranked hypotheses — ranked
        # candidates to test, never assumed causes.
        if self.causal is not None:
            try:
                symptom = f"{inc.get('error_class','')} " \
                          f"{inc.get('error_message','')[:200]}"
                for p in self.causal.priors(
                        symptom, subsystem=str(inc.get("subsystem") or "")):
                    if all(h.get("causal_id") != p.get("causal_id")
                           for h in inc["hypotheses"]):
                        inc["hypotheses"].append(p)
                inc["hypotheses"].sort(
                    key=lambda h: -h.get("confidence", 0))
                inc["hypotheses"] = inc["hypotheses"][:6]
            except Exception:
                pass
        inc["repair_kind"] = diag["repair_kind"]
        inc["confidence"] = diag["confidence"]
        if inc["hypotheses"]:
            inc["confidence"] = max(inc["confidence"],
                                    inc["hypotheses"][0].get(
                                        "confidence", 0))
        # First-class hypothesis rows — persisted lifecycle + evidence,
        # not just incident payloads.
        if self.hypotheses is not None:
            try:
                self.hypotheses.upsert_for_incident(
                    inc["id"], inc["hypotheses"])
            except Exception:
                pass
        # Significant choice → journal it with the alternatives that
        # were on the table.
        if self.decisions is not None and inc["hypotheses"]:
            try:
                self.decisions.record(
                    f"{inc.get('subsystem','')}: "
                    f"{inc.get('error_class','')}",
                    alternatives=[h.get("kind", "?") for h in
                                  inc["hypotheses"]],
                    evidence=[e for h in inc["hypotheses"][:2]
                              for e in (h.get("evidence") or [])[:2]],
                    decision=f"repair_kind={inc['repair_kind']}; "
                             f"top={inc['hypotheses'][0].get('kind')}",
                    expected_outcome="repair resolves incident",
                    actor="self_repair",
                    context={"incident_id": inc["id"]})
            except Exception:
                pass
        # Prior procedures adjust confidence — a proven fix lifts it,
        # a known-bad history warns.
        prior = self.memory.best_fix(inc["signature"])
        if prior:
            inc["confidence"] = min(0.98, inc["confidence"] + 0.15)
            inc.setdefault("history", []).append(
                {"ts": time.time(), "event": "memory_recall",
                 "detail": f"known procedure ({prior['kind']}, "
                           f"confidence {prior['confidence']})"})
        # Weak diagnosis → gather external evidence for the patch mission
        # and human review. Research attaches; it does not decide.
        top_kind = str((inc["hypotheses"] or [{}])[0].get("kind") or "")
        if self._researcher and (top_kind == "unknown"
                                 or inc["confidence"] < 0.45) \
                and len(inc.setdefault("research", [])) < 3:
            try:
                res = self._researcher(dict(inc))
            except Exception:
                res = None
            if res:
                inc["research"].append(res)
                inc.setdefault("history", []).append(
                    {"ts": time.time(), "event": "research",
                     "detail": str(res.get("summary")
                                   or res.get("status") or "")[:300]})
        return "planning"

    def _stage_plan(self, inc: dict) -> str:
        self._set(inc, "planning")
        top = (inc["hypotheses"] or [{}])[0]
        steps = []
        if inc["repair_kind"] == "operational":
            steps = [f"verify hypothesis '{top.get('kind')}'",
                     f"apply operational fix for {top.get('kind')}",
                     "re-run health check",
                     "resume interrupted operation"]
        else:
            steps = ["reproduce failure in isolation where possible",
                     "open isolated repair worktree",
                     "write regression test that fails pre-patch",
                     "apply minimal patch",
                     "run targeted tests", "run regression suite",
                     "independent review", "canary check", "promote or reject"]
        inc["plan"] = steps[:12]
        return "patching" if inc["repair_kind"] == "code" else "testing"

    # ------------------------------------------------------------------
    # operational path

    def _apply_operational(self, inc: dict) -> str:
        top = (inc["hypotheses"] or [{}])[0]
        kind = str(top.get("kind") or "")
        fixer = self.fixers.get(kind)
        if fixer is None:
            # no deterministic fixer — fall back to the code pipeline if
            # there is something to patch, else escalate.
            if inc.get("suspects") and self.patch_generator:
                inc["repair_kind"] = "code"
                return "patching"
            self._fail_open(inc, f"no operational fixer for '{kind}' "
                                 "and no code suspects")
            return ""
        inc["repair_procedure"].append(f"operational:{kind}")
        try:
            result = fixer({"incident": dict(inc),
                            "repo_root": self.repo_root}) or {}
        except Exception as exc:
            result = {"ok": False, "detail": str(exc)[:300]}
        inc["verification"]["operational_fix"] = result
        if result.get("ok"):
            return "promoting"
        # deterministic fix failed — count it and reconsider
        inc["attempts"]["patch"] = int(
            inc["attempts"].get("patch") or 0) + 1
        self.memory.record(inc["signature"], kind=f"operational:{kind}",
                           steps=inc["repair_procedure"], success=False,
                           confidence=inc["confidence"],
                           detail=result.get("detail", ""))
        if self.memory.known_bad(inc["signature"], f"operational:{kind}"):
            self._fail_open(inc, f"operational fix '{kind}' repeatedly "
                                 "failed — refusing to loop")
            return ""
        return "planning"  # replan with the new evidence

    # ------------------------------------------------------------------
    # code path

    def _stage_patch(self, inc: dict) -> str:
        if self.patch_generator is None:
            self._fail_open(inc, "no code-repair generator wired — "
                                 "diagnosis and suspects attached")
            return ""
        if self._is_blocked():
            self._fail_open(inc, "autonomy is stopped/paused")
            return ""
        self._set(inc, "patching")
        try:
            wt = self.patcher.create(inc["id"])
        except Exception as exc:
            self._fail_open(inc, f"worktree failed: {exc}")
            return ""
        inc["worktree"] = str(wt)

        # Async path — a repair mission already spawned is generating the
        # patch inside the worktree; stay in `patching` until it settles.
        pending = inc.get("patch_mission")
        if pending and self._mission_status is not None:
            status = str(self._mission_status(pending) or "")
            if status in {"ready", "active", "planning", "executing",
                          "verifying", "evaluating", "replanning",
                          "waiting_dependency", "waiting_approval",
                          "paused"}:
                return ""           # still generating
            if status in {"completed", "completed_with_warnings"}:
                inc["patch_files"] = self.patcher.changed_files(wt) or \
                    inc.get("patch_files", [])
                if not inc["patch_files"]:
                    self._fail_open(
                        inc, "repair mission finished but the worktree "
                             "has no changes")
                    return ""
                inc["repair_procedure"].append(
                    f"patch:{','.join(inc['patch_files'])[:200]}")
                return "testing"
            # terminal failure → count attempt and replan/escalate
            inc["attempts"]["patch"] = int(
                inc["attempts"].get("patch") or 0) + 1
            inc["patch_mission"] = ""
            if self._budget_check(inc):
                return ""
            return "planning"

        inc["attempts"]["patch"] = int(
            inc["attempts"].get("patch") or 0) + 1
        try:
            patch = self.patch_generator(dict(inc), wt) or {}
        except Exception as exc:
            self._fail_open(inc, f"patch generation failed: {exc}")
            return ""
        # Generator may delegate to a mission — wait for it next tick.
        if patch.get("mission_id"):
            inc["patch_mission"] = str(patch["mission_id"])
            self._save()
            return ""
        inc["patch_files"] = list(patch.get("files") or [])
        inc["regression_test"] = str(patch.get("test") or "")
        if not inc["patch_files"]:
            self._fail_open(inc, "generator produced no changes")
            return ""
        inc["repair_procedure"].append(
            f"patch:{','.join(inc['patch_files'])[:200]}")
        return "testing"

    def _lost_worktree(self, inc: dict) -> bool:
        """A persisted incident can outlive its scratch worktree (restart,
        disk cleanup, manual prune). Rewind to `patching` so the candidate
        regenerates instead of stranding the incident at needs_human —
        bounded, so a repeatedly-vanishing worktree still escalates."""
        losses = int(inc["attempts"].get("worktree_loss") or 0) + 1
        inc["attempts"]["worktree_loss"] = losses
        # A lost worktree is infrastructure loss, not a failed patch —
        # refund the attempt the regeneration is about to consume so a
        # vanishing scratch dir can't burn the patch budget. The
        # worktree_loss counter itself is the bound on the rewind loop.
        inc["attempts"]["patch"] = max(
            0, int(inc["attempts"].get("patch") or 0) - 1)
        inc["worktree"] = ""
        inc["patch_mission"] = ""
        inc["patch_files"] = []
        if losses < 3:
            inc["repair_procedure"].append("worktree lost — regenerating")
            self._save()
            return True
        self._fail_open(inc, "worktree repeatedly missing — cannot verify")
        return False

    def _stage_test(self, inc: dict) -> str:
        self._set(inc, "testing")
        wt = Path(inc["worktree"]) if inc.get("worktree") else None
        if not wt or not wt.exists():
            return "patching" if self._lost_worktree(inc) else ""
        ver = inc.setdefault("verification", {})
        targets = targeted_tests_for(inc, wt)
        results = []
        for t in targets:
            results.append(run_unittest(wt, t,
                                        timeout_s=self.verify_timeout_s))
            if not results[-1]["ok"]:
                break
        ver["targeted"] = results
        if results and not all(r["ok"] for r in results):
            inc["attempts"]["same_patch_failures"] = int(
                inc["attempts"].get("same_patch_failures") or 0) + 1
            self.memory.record(inc["signature"], kind="code",
                               steps=inc["repair_procedure"], success=False,
                               confidence=inc["confidence"],
                               detail="targeted tests failed")
            return "planning" if not self._budget_check(inc) else ""
        # Regression gate — bounded, only when configured.
        if self.regression_suite:
            ver["regression"] = run_unittest(
                wt, self.regression_suite,
                timeout_s=max(self.verify_timeout_s, 600))
            if not ver["regression"].get("ok"):
                inc["attempts"]["same_patch_failures"] = int(
                    inc["attempts"].get("same_patch_failures") or 0) + 1
                self.memory.record(inc["signature"], kind="code",
                                   steps=inc["repair_procedure"],
                                   success=False,
                                   confidence=inc["confidence"],
                                   detail="regression suite failed")
                return "planning" if not self._budget_check(inc) else ""
        return "reviewing"

    def _default_review(self, inc: dict, wt: Path, diff: str) -> dict:
        """Deterministic baseline review — static checks that always run
        even without a separate reviewer model wired in."""
        notes = []
        ok = True
        joined = diff or ""
        for pat, why in ((r"except\s*:\s*pass",
                          "blanket except-pass hides the error"),
                         (r"except\s+Exception\s*:\s*pass",
                          "except-Exception-pass hides the error"),
                         (r"os\.system\(",
                          "raw os.system introduced")):
            if re.search(pat, joined):
                notes.append(f"suspicious: {why}")
                ok = False
        for f in inc.get("patch_files") or []:
            if f.startswith(("..", "/", "\\")) or ".." in Path(f).parts:
                notes.append(f"patch escapes repo: {f}")
                ok = False
        if not (inc.get("patch_files")):
            ok = False
            notes.append("empty patch")
        return {"ok": ok, "reviewer": "static",
                "confidence": 0.7 if ok else 0.3, "notes": notes}

    def _stage_review(self, inc: dict) -> str:
        self._set(inc, "reviewing")
        wt = Path(inc["worktree"]) if inc.get("worktree") else None
        if not wt or not wt.exists():
            return "patching" if self._lost_worktree(inc) else ""
        diff = ""
        try:
            diff = __import__("subprocess").run(
                ["git", "-C", str(wt), "diff", "HEAD"],
                capture_output=True, text=True, timeout=20, encoding="utf-8", errors="replace").stdout
        except Exception:
            pass
        if self.reviewer is not self._default_review or not diff:
            try:
                review = self.reviewer(dict(inc), wt, diff) or {}
            except Exception as exc:
                review = {"ok": False, "error": str(exc)[:200],
                          "reviewer": "external"}
        else:
            review = self.reviewer(dict(inc), wt, diff)
        inc["review"] = review
        if not review.get("ok"):
            self.memory.record(inc["signature"], kind="code",
                               steps=inc["repair_procedure"], success=False,
                               confidence=inc["confidence"],
                               detail="review rejected candidate")
            return "planning" if not self._budget_check(inc) else ""
        return "canary"

    def _stage_canary(self, inc: dict) -> str:
        self._set(inc, "canary")
        wt = Path(inc["worktree"]) if inc.get("worktree") else None
        if not wt or not wt.exists():
            return "patching" if self._lost_worktree(inc) else ""
        result = self.canary.check(dict(inc), wt)
        inc["verification"]["canary"] = result
        if result.get("ok") is False:
            inc["attempts"]["same_patch_failures"] = int(
                inc["attempts"].get("same_patch_failures") or 0) + 1
            return "planning" if not self._budget_check(inc) else ""
        if result.get("skipped") and \
                inc.get("subsystem") in self.canary_required_for:
            self._fail_open(
                inc, "canary required for this subsystem but no candidate "
                     "launcher is configured")
            return ""
        return "promoting"

    def _stage_promote(self, inc: dict) -> str:
        # Final gate: confidence, autonomy permission, review, tests.
        ver = inc.get("verification") or {}
        targeted_ok = all(r.get("ok") for r in ver.get("targeted") or [])
        if inc["repair_kind"] == "code":
            if inc["confidence"] < self.min_promote_confidence:
                self._fail_open(
                    inc, f"root-cause confidence {inc['confidence']:.2f} "
                         f"< {self.min_promote_confidence}")
                return ""
            if not targeted_ok and (ver.get("targeted")):
                self._fail_open(inc, "targeted verification did not pass")
                return ""
            if not self.auto_promote:
                self._fail_open(
                    inc, "candidate verified but auto-promote is disabled "
                         "— review and promote manually")
                return ""
        self._set(inc, "promoting")
        if inc["repair_kind"] == "code" and inc.get("worktree"):
            wt = Path(inc["worktree"])
            if not wt.exists():
                return "patching" if self._lost_worktree(inc) else ""
            files = self.patcher.changed_files(wt) or inc["patch_files"]
            if not files:
                self._fail_open(
                    inc, "rollback snapshot failed: no changed files recorded")
                return ""
            manifest = self.rollback.snapshot(inc["id"], files)
            if manifest.get("ok") is not True:
                self._fail_open(
                    inc, "rollback snapshot failed: "
                         f"{manifest.get('error', 'invalid snapshot')}")
                return ""
            inc["rollback"] = {"snapshot": str(self.rollback.lkg_root
                                                 / inc["id"]),
                               "files": [f["path"] for f in
                                         manifest["files"]]}
            try:
                how = self.patcher.promote(wt)
                inc["promotion"] = {"method": how, "ts": time.time(),
                                    "files": files}
            except Exception as exc:
                self._fail_open(inc, f"promotion failed: {exc}")
                return ""
            # Auditable commit — the promoted diff lands as a named
            # repair commit, not an anonymous dirty tree. Commit failure
            # never blocks a verified promotion.
            if self.commit_on_promote:
                top = (inc.get("hypotheses") or [{}])[0]
                sha = self.patcher.commit_promotion(
                    files, incident_id=inc["id"],
                    summary=f"{inc['error_class']} in {inc['subsystem']}",
                    evidence=f"Root cause: {top.get('kind', 'unknown')} — "
                             f"{top.get('detail', '')[:200]}"
                             f"\nVerification: {len(ver.get('targeted') or [])} "
                             "targeted check(s) passed")
                if sha:
                    inc["promotion"]["commit"] = sha
        self._set(inc, "resolved",
                  f"{inc['repair_kind']} repair applied")
        if self._on_resolved:
            try:
                self._on_resolved(dict(inc))
            except Exception:
                pass
        dur = time.time() - float(inc.get("created_at") or time.time())
        self.memory.record(inc["signature"], kind=inc["repair_kind"] or "code",
                           steps=inc["repair_procedure"], success=True,
                           confidence=inc["confidence"],
                           detail=inc["hypotheses"][0]["detail"]
                           if inc.get("hypotheses") else "",
                           duration_s=dur)
        # Resolution proves the top hypothesis — confirm it in the
        # lifecycle store and write the full causal chain to memory so
        # the next similar symptom starts with a ranked prior.
        top_h = (inc.get("hypotheses") or [{}])[0]
        if self.hypotheses is not None:
            try:
                for h in self.hypotheses.list(incident_id=inc["id"]):
                    if h.get("kind") == top_h.get("kind"):
                        self.hypotheses.confirm(
                            h["id"], evidence="incident resolved")
                    elif h.get("status") in {"proposed", "testing",
                                             "supported", "weakened"}:
                        self.hypotheses.reject(
                            h["id"], reason="not the resolved cause")
            except Exception:
                pass
        if self.causal is not None:
            try:
                ver = inc.get("verification") or {}
                ver_s = f"targeted={len(ver.get('targeted') or [])} " \
                        f"regression={len(ver.get('regression') or [])}"
                self.causal.record(
                    f"{inc.get('error_class','')} "
                    f"{inc.get('error_message','')[:200]}",
                    root_cause=str(top_h.get("kind") or "unknown"),
                    mechanism=str(top_h.get("detail") or ""),
                    fix=inc.get("repair_procedure") or
                    inc.get("repair_kind") or "",
                    verification=ver_s,
                    subsystem=str(inc.get("subsystem") or ""),
                    incident_id=inc["id"])
            except Exception:
                pass
        # Resume the operation the failure interrupted.
        if self._on_resumed and inc.get("interrupted_operation"):
            try:
                self._on_resumed(inc["interrupted_operation"])
            except Exception:
                pass
        sev = inc.get("severity")
        self._notify(
            "failure" if sev == "critical" else "info",
            f"Self-repair resolved: {inc['error_class']}",
            f"{inc['subsystem']} · {inc['repair_kind']} · "
            f"confidence {inc['confidence']:.0%}")
        if inc.get("worktree"):
            self.patcher.cleanup(Path(inc["worktree"]), inc["id"])
        self._save()
        return ""

    # ------------------------------------------------------------------
    # drivers

    _STAGE_FN = {}

    def _advance(self, inc: dict) -> None:
        """Advance one incident one stage. Bounded; safe to call per tick."""
        if self._budget_check(inc):
            return
        state = str(inc.get("state"))
        nxt: str | None
        if state == "detected":
            nxt = self._stage_collect(inc)
        elif state == "collecting":
            nxt = "localizing"
        elif state == "localizing":
            nxt = self._stage_localize(inc)
        elif state == "diagnosing":
            nxt = self._stage_diagnose(inc)
        elif state == "planning":
            nxt = self._stage_plan(inc)
        elif state == "patching":
            nxt = self._stage_patch(inc)
        elif state == "testing":
            nxt = (self._apply_operational(inc)
                   if inc.get("repair_kind") == "operational"
                   else self._stage_test(inc))
        elif state == "reviewing":
            nxt = self._stage_review(inc)
        elif state == "canary":
            nxt = self._stage_canary(inc)
        elif state == "promoting":
            nxt = self._stage_promote(inc)
        else:
            return
        if nxt:
            transition(inc, nxt)
            inc["updated_at"] = time.time()
            with self._lock:
                self._save()

    # An open incident untouched this long is definitionally stale —
    # every stage transition stamps updated_at, so a row this old was
    # parked mid-pipeline across restarts (its condition has usually
    # resolved itself; a disk-pressure incident from days ago must not
    # resume "fixing" a non-issue). Abandon it — terminal, reviewable,
    # and its repair mission retires via the supervisor check.
    STALE_INCIDENT_S = 3 * 86400.0

    def tick(self, now: float | None = None) -> None:
        """Bounded supervisor-tick step: each open incident advances at
        most one stage so a heavy repair cannot stall the supervisor."""
        now = time.time() if now is None else now
        with self._lock:
            due = [dict(r) for r in self._rows()
                   if r.get("state") in OPEN_REPAIR_STATES
                   and r.get("state") != "needs_human"]
        for snapshot in due:
            inc = self.get(snapshot["id"])  # live row, not the copy
            if inc is None:
                continue
            if (now - float(inc.get("updated_at") or 0.0)
                    > self.STALE_INCIDENT_S):
                self._set(inc, "abandoned",
                          "stale — no progress in "
                          f"{int(self.STALE_INCIDENT_S // 86400)}d")
                continue
            try:
                self._advance(inc)
            except Exception as exc:
                inc.setdefault("history", []).append(
                    {"ts": time.time(), "event": "stage_error",
                     "detail": str(exc)[:200]})
                inc["attempts"]["replans"] = int(
                    inc["attempts"].get("replans") or 0) + 1
                self._budget_check(inc)
                self._save()

    def process_incident(self, incident_id: str,
                         max_steps: int = 40) -> dict | None:
        """Synchronous end-to-end drive — used by tests and by the
        manual 'repair now' action."""
        for _ in range(max_steps):
            inc = self.get(incident_id)
            if inc is None or str(inc.get("state")) in \
                    TERMINAL_REPAIR_STATES:
                return inc
            self._advance(inc)
        return self.get(incident_id)

    def retry(self, incident_id: str) -> bool:
        inc = self.get(incident_id)
        if inc is None:
            return False
        if str(inc.get("state")) in TERMINAL_REPAIR_STATES:
            transition(inc, "detected", detail="manual retry")
            self._save()
            return True
        return False

    def rollback_incident(self, incident_id: str) -> dict:
        inc = self.get(incident_id)
        if inc is None:
            return {"ok": False, "error": "no such incident"}
        result = self.rollback.restore(incident_id)
        if result.get("ok"):
            # The candidate is dead — its worktree/branch must not linger.
            wt = inc.get("worktree")
            if wt:
                try:
                    self.patcher.cleanup(Path(wt), inc["id"])
                except Exception:
                    pass
                inc["worktree"] = ""
            self._set(inc, "rolled_back", "manual rollback")
        return result

    def abandon(self, incident_id: str) -> dict:
        """Human decision to not repair. Closes the incident, frees the
        candidate worktree, and stops the pipeline advancing it."""
        inc = self.get(incident_id)
        if inc is None:
            return {"ok": False, "error": "no such incident"}
        state = str(inc.get("state"))
        if state in {"resolved", "rolled_back", "abandoned"}:
            return {"ok": False, "error": f"incident already {state}"}
        wt = inc.get("worktree")
        if wt:
            try:
                self.patcher.cleanup(Path(wt), inc["id"])
            except Exception:
                pass
            inc["worktree"] = ""
        self._set(inc, "abandoned", "closed by user — no repair applied")
        return {"ok": True}
