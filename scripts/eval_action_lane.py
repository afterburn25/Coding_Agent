"""Action-lane evaluation — measures deterministic local-action execution.

Runs a bounded corpus of real user phrasings through the production lane
(action_ops → orchestrator._local_action_reply → execute_plan → verify →
ActionLedger) and reports per-case outcome classes:

    verified     — executed and confirmed on disk
    awaiting_approval — correctly parked at a permission gate
    clarify      — correctly asked instead of guessing
    fell_through — correctly handed to the model lane (out of grammar)
    failed       — ran and failed honestly
    MISCLASS     — outcome did not match the expected class (bug signal)

No models are required — the lane is deterministic. Usage:

    python scripts/eval_action_lane.py            # human report
    python scripts/eval_action_lane.py --json out.json

Exit code is non-zero when any case misclassifies, so the eval can gate
CI or regression runs the same way the unit suite does.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from localcodeagent.action_ledger import ActionLedger
from localcodeagent.agent.orchestrator import AgentOrchestrator
from localcodeagent.config import AgentConfig
from localcodeagent.context.intent import understand_turn
from localcodeagent.models.router import ModelRouter
from localcodeagent.tools.base import ToolRegistry
from localcodeagent.tools.filesystem import register_filesystem_tools
from localcodeagent.workflow.checkpoint import CheckpointManager
from localcodeagent.workflow.memory import ProjectMemory
from localcodeagent.workflow.repository import RepositoryIndex
from localcodeagent.workflow.tasks import TaskStore


class _FakeRuntime:
    def refresh_hardware(self):
        return None

    def fresh_hardware(self, max_age_s: float = 15.0):
        return None

    def ensure_ready(self, profile):
        return profile.endpoint

    def recover(self, profile):
        return profile.endpoint


def _orch(ws: Path, perms: dict, ledger: ActionLedger) -> AgentOrchestrator:
    reg = ToolRegistry(perms)
    register_filesystem_tools(reg, ws)
    return AgentOrchestrator(
        AgentConfig(), ModelRouter([]), reg, _FakeRuntime(),
        tasks=TaskStore(ws), checkpoints=CheckpointManager(ws),
        memory=ProjectMemory(ws), repository_index=RepositoryIndex(ws),
        action_ledger=ledger)


# (utterance, expected_class, setup_fn(ws) | None)
CASES: list[tuple[str, str, object]] = [
    # --- mkdir phrasing coverage -------------------------------------
    ("create a folder named alpha", "verified", None),
    ("create a folder alpha2", "verified", None),
    ("mkdir scratch", "verified", None),
    ("make a directory called builds", "verified", None),
    ("can you create a folder named polite", "verified", None),
    ("please create a folder named manners", "verified", None),
    # idempotent — already present
    ("create a folder named alpha", "verified",
     lambda ws: (ws / "alpha").mkdir(exist_ok=True)),
    # --- write --------------------------------------------------------
    ("write 'hello world' to note.txt", "verified", None),
    ("save 'config data' into settings.txt", "verified", None),
    ("create a file named readme.md containing 'docs'",
     "verified", None),
    # --- delete -------------------------------------------------------
    ("delete junk.txt", "verified",
     lambda ws: (ws / "junk.txt").write_text("x")),
    ("remove the file old.log", "verified",
     lambda ws: (ws / "old.log").write_text("x")),
    ("delete emptydir", "verified",
     lambda ws: (ws / "emptydir").mkdir(exist_ok=True)),
    # idempotent — already gone
    ("delete absent.txt", "verified", None),
    # --- move / rename / copy ----------------------------------------
    ("rename a.txt to b.txt", "verified",
     lambda ws: (ws / "a.txt").write_text("x")),
    ("move c.txt to d.txt", "verified",
     lambda ws: (ws / "c.txt").write_text("x")),
    ("copy e.txt to e2.txt", "verified",
     lambda ws: (ws / "e.txt").write_text("x")),
    # --- clarify, never guess ----------------------------------------
    ("delete the folder", "clarify", None),
    ("move report.pdf", "clarify",
     lambda ws: (ws / "report.pdf").write_text("x")),
    ("rename notes.md", "clarify",
     lambda ws: (ws / "notes.md").write_text("x")),
    ("copy data.csv", "clarify",
     lambda ws: (ws / "data.csv").write_text("x")),
    # --- outside-workspace target parks at the gate -------------------
    # ~/ expands outside the temp workspace on every platform.
    ("create a folder ~/NexusEvalGate", "awaiting_approval", None),
    # --- compound -----------------------------------------------------
    ("create a folder named comp1, and then create a folder named comp2",
     "verified", None),
    # --- out of grammar → model lane ---------------------------------
    ("tell me a joke", "fell_through", None),
    ("copy that", "fell_through", None),   # acknowledgment, not a file op
    ("move it", "fell_through", None),     # pronoun — no resolvable target
    ("write 'orphan'", "fell_through", None),  # ambiguous — write vs write-file
    ("refactor the parser to use iterparse", "fell_through", None),
    # classifier-missed compound — must not create a literal path
    # named 'alpha and make dir beta'; whole utterance goes to model.
    ("create folder alpha and make dir beta", "fell_through", None),
    ("can you generate images", "fell_through", None),
    ("what branches are in github for this project", "fell_through",
     None),
]

# The flagship outside-root case — an explicit absolute Windows path is
# only absolute on Windows; on POSIX it resolves workspace-relative.
if sys.platform.startswith("win"):
    CASES.append(
        ("create a folder D:\\NexusEvalGate", "awaiting_approval", None))

PERMS = {"filesystem.read": "allow", "filesystem.write": "allow",
         "filesystem.delete": "allow"}


def _classify(orch: AgentOrchestrator, text: str, task_id: str,
              ledger: ActionLedger) -> tuple[str, str]:
    env = understand_turn(text, active=None)
    result = orch._local_action_reply(text, task_id, env=env)
    if result is None:
        return "fell_through", ""
    content = result.content or ""
    pending = result.pending_approval or {}
    if pending.get("kind") == "local_action":
        return "awaiting_approval", content
    # Classify from evidence, not phrasing — the ledger status is the
    # authoritative record of what actually happened.
    rows = [r for r in ledger.recent(20) if r.get("task_id") == task_id]
    if rows:
        last = rows[-1]["status"]
        if last == "clarify":
            return "clarify", content
        if last in {"failed", "denied", "unverified", "unavailable"}:
            return "failed", content
        if last == "verified":
            return "verified", content
    low = content.lower()
    if "?" in content and ("what should" in low or "which" in low
                           or "what do you" in low):
        return "clarify", content
    if any(m in low for m in ("can't", "couldn't", "failed",
                              "did not", "didn't", "not approved",
                              "denied", "unavailable")):
        return "failed", content
    return "verified", content


def main() -> int:
    as_json = "--json" in sys.argv
    out_path = None
    if as_json:
        idx = sys.argv.index("--json")
        if idx + 1 < len(sys.argv) and not sys.argv[idx + 1].startswith("-"):
            out_path = Path(sys.argv[idx + 1])

    results: list[dict] = []
    misses = 0
    for i, (text, expected, setup) in enumerate(CASES):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = root / "ws"
            ws.mkdir()
            if callable(setup):
                setup(ws)
            ledger = ActionLedger(root / "ledger.json")
            orch = _orch(ws, PERMS, ledger)
            # The lane updates a real task row — create one like run() does.
            task_id = orch.tasks.create(text, "auto").id
            actual, detail = _classify(orch, text, task_id, ledger)
            ok = actual == expected
            if not ok:
                misses += 1
            results.append({
                "case": text, "expected": expected, "actual": actual,
                "ok": ok, "detail": detail[:200],
                "ledger": [r["status"] for r in ledger.recent(5)],
            })

    total = len(results)
    passed = total - misses
    by_class: dict[str, list[int]] = {}
    for r in results:
        by_class.setdefault(r["actual"], [0, 0])
        by_class[r["actual"]][1] += 1
        by_class[r["actual"]][0] += int(r["ok"])

    report = {
        "eval": "action_lane",
        "cases": total,
        "passed": passed,
        "accuracy": round(passed / total, 4) if total else 0.0,
        "by_actual_class": {
            k: {"ok": v[0], "n": v[1]} for k, v in sorted(by_class.items())},
        "misclassifications": [r for r in results if not r["ok"]],
        "results": results,
    }

    if out_path:
        out_path.write_text(json.dumps(report, indent=2))
    if as_json:
        print(json.dumps(report, indent=2))
    else:
        print(f"action-lane eval: {passed}/{total} "
              f"({report['accuracy'] * 100:.1f}%)")
        for cls, v in sorted(by_class.items()):
            print(f"  {cls:20s} {v[0]}/{v[1]}")
        for r in results:
            if not r["ok"]:
                print(f"  MISCLASS [{r['expected']}->{r['actual']}] "
                      f"{r['case']!r}: {r['detail'][:100]}")
    return 1 if misses else 0


if __name__ == "__main__":
    raise SystemExit(main())
