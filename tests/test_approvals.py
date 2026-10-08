"""Interactive chat permission cards — decision gating, card payloads,
durable resolutions, exact-once semantics, and end-to-end resume.

One source of truth throughout: PermissionManager decides which options
are legal; the browser only ever renders what the backend authorizes.
"""
import tempfile
import unittest
from pathlib import Path

from localcodeagent import approvals
from localcodeagent.agent.orchestrator import AgentOrchestrator
from localcodeagent.config import AgentConfig, ModelProfile
from localcodeagent.models.router import ModelRouter
from localcodeagent.permissions import AUTONOMY_NEVER_AUTO, PermissionManager
from localcodeagent.tools.base import ToolRegistry, ToolSpec
from localcodeagent.workflow.checkpoint import CheckpointManager
from localcodeagent.workflow.memory import ProjectMemory
from localcodeagent.workflow.repository import RepositoryIndex
from localcodeagent.workflow.tasks import TaskStore


class _FakeRuntime:
    def refresh_hardware(self):
        return None

    def fresh_hardware(self, max_age_s: float = 15.0):
        return self.refresh_hardware()

    def ensure_ready(self, profile):
        return profile.endpoint

    def recover(self, profile):
        return profile.endpoint


def _pending(task_id: str, permission: str = "filesystem.write",
             **extra) -> dict:
    pending = {
        "kind": "tool",
        "name": "write_file",
        "arguments": {"path": "D:/Nexus/README.md", "content": "hi"},
        "permission": permission,
        "detail": "Create file D:/Nexus/README.md",
    }
    pending.update(extra)
    return approvals.stamp_pending(task_id, pending)


class DecisionOptionsTests(unittest.TestCase):
    """The backend alone decides which choices a card can offer."""

    def test_ask_offers_session_deny_always(self):
        self.assertEqual(
            approvals.decision_options("filesystem.write", "ask"),
            ["session", "deny", "always"])

    def test_deny_level_offers_nothing(self):
        self.assertEqual(
            approvals.decision_options("filesystem.write", "deny"), [])

    def test_creator_level_without_unlock_offers_nothing(self):
        self.assertEqual(
            approvals.decision_options(
                "system.control", "creator", creator_ok=False), [])

    def test_creator_level_unlocked_never_persists(self):
        self.assertEqual(
            approvals.decision_options(
                "system.control", "creator", creator_ok=True),
            ["session", "deny"])

    def test_never_auto_permissions_never_persist(self):
        for perm in AUTONOMY_NEVER_AUTO:
            opts = approvals.decision_options(perm, "ask")
            self.assertNotIn("always", opts, perm)
            self.assertIn("session", opts, perm)

    def test_unknown_decision_not_in_enum(self):
        self.assertNotIn("forever", approvals.DECISIONS)
        self.assertNotIn("sometimes", approvals.DECISIONS)


class ApprovalCardTests(unittest.TestCase):
    def test_card_is_backend_authoritative(self):
        mgr = PermissionManager({"filesystem.write": "ask"})
        pending = _pending("t1")
        card = approvals.approval_card(pending, "t1", mgr)
        self.assertEqual(card["id"], pending["id"])
        self.assertEqual(card["task_id"], "t1")
        self.assertEqual(card["permission"], "filesystem.write")
        # Friendly title — never the raw key as primary text.
        self.assertIn("·", card["title"])
        self.assertNotEqual(card["title"], "filesystem.write")
        self.assertEqual(card["decisions"], ["session", "deny", "always"])
        self.assertEqual(
            card["decision_labels"]["session"], "Allow this session")
        self.assertEqual(card["status"], "pending")
        self.assertTrue(card["action"])

    def test_card_disabled_when_policy_denies(self):
        mgr = PermissionManager({"filesystem.write": "deny"})
        card = approvals.approval_card(_pending("t1"), "t1", mgr)
        self.assertEqual(card["decisions"], [])
        self.assertEqual(
            card["disabled_reason"], approvals.DISABLED_MESSAGE)

    def test_card_marks_creator_requirement(self):
        mgr = PermissionManager({"system.control": "creator"})
        mgr.creator_verified = lambda: False
        card = approvals.approval_card(
            _pending("t1", permission="system.control"), "t1", mgr)
        self.assertTrue(card["creator_required"])
        self.assertEqual(card["decisions"], [])

    def test_arguments_sanitized_for_display(self):
        mgr = PermissionManager({"filesystem.write": "ask"})
        pending = _pending("t1")
        pending["arguments"]["content"] = "x" * 500
        card = approvals.approval_card(pending, "t1", mgr)
        self.assertLessEqual(len(card["arguments"]["content"]), 200)


class ResolutionRowTests(unittest.TestCase):
    def test_resolution_row_shape(self):
        mgr = PermissionManager({"filesystem.write": "ask"})
        card = approvals.approval_card(_pending("t1"), "t1", mgr)
        row = approvals.resolution_row(card, "session")
        self.assertEqual(row["id"], card["id"])
        self.assertEqual(row["decision"], "session")
        self.assertEqual(row["decision_label"], "Allow this session")
        self.assertEqual(row["status"], "resolved")
        self.assertGreater(row["resolved_at"], 0)


class _OrchestratorMixin:
    def _build(self, root: Path, permissions: dict):
        profile = ModelProfile(
            id="local", endpoint="http://unused/v1", model="x",
            roles=["primary_coder", "fast_coder", "deep_reasoner",
                   "reviewer"], runtime="external")
        config = AgentConfig(
            models=[profile], permissions=permissions,
            auto_verify_after_changes=False, review_after_changes=False)
        router = ModelRouter(config.models)
        tools = ToolRegistry(config.permissions)
        index = RepositoryIndex(root)
        index.build()
        agent = AgentOrchestrator(
            config, router, tools, _FakeRuntime(),
            tasks=TaskStore(root), checkpoints=CheckpointManager(root),
            memory=ProjectMemory(root), repository_index=index)
        return agent, tools


class OrchestratorApprovalEventTests(_OrchestratorMixin, unittest.TestCase):
    def test_parked_approval_event_carries_card(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            agent, tools = self._build(root, {"test.execute": "ask"})
            tools.register(ToolSpec(
                "dangerous_test_tool", "test",
                {"type": "object",
                 "properties": {"value": {"type": "string"}},
                 "required": ["value"]},
                "test.execute", lambda args: "OK"))
            events = []
            agent._provider_for = lambda _: _Scripted()
            result = agent.run(
                "do the thing",
                event_callback=events.append)
            self.assertEqual(result.task["status"], "waiting_approval")
            approval = next(
                e for e in events if e.get("type") == "approval")
            card = approval.get("card") or {}
            # The card is backend-authoritative: id, friendly title,
            # the legal decision set.
            self.assertTrue(card.get("id"))
            self.assertEqual(card.get("permission"), "test.execute")
            self.assertIn("session", card.get("decisions", []))
            self.assertIn("deny", card.get("decisions", []))
            # Pending record got durable identity + timestamps.
            pending = result.task["pending_approval"]
            self.assertEqual(pending["id"], card["id"])
            self.assertGreater(pending.get("created_at", 0), 0)

    def test_never_auto_card_omits_always(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            agent, tools = self._build(root, {"test.execute": "ask"})
            pending = _pending("t-x", permission="spend.money")
            card = agent._approval_card("t-x", pending)
            self.assertNotIn("always", card["decisions"])
            self.assertIn("session", card["decisions"])


class _Scripted:
    """Minimal provider whose first turn demands the gated tool and
    second turn finishes."""

    def __init__(self):
        self.calls = 0

    def complete(self, *, messages, tools=None):
        import json as _json
        from localcodeagent.models.provider import ProviderResponse
        self.calls += 1
        if self.calls == 1:
            return ProviderResponse(message={
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "call1",
                    "type": "function",
                    "function": {"name": "dangerous_test_tool",
                                 "arguments": _json.dumps(
                                     {"value": "ok"})},
                }],
            }, raw={})
        return ProviderResponse(
            message={"role": "assistant", "content": "finished"},
            raw={})


class AppStateDecisionTests(unittest.TestCase):
    """resolve_chat_approval — the single decision boundary."""

    def _state(self, td: str, permissions: dict):
        from localcodeagent.server import AppState
        cfg = AgentConfig(
            models=[ModelProfile(
                id="ext", endpoint="http://x/v1", model="m",
                roles=["primary_coder"], runtime="external")],
            permissions=permissions)
        return AppState(cfg, Path(td), Path(td) / ".runtime")

    def _park(self, state, permission="filesystem.write") -> tuple:
        task = state.tasks.create("make a readme", "auto")
        pending = _pending(task.id, permission=permission)
        state.tasks.update(task.id, status="waiting_approval",
                           phase="waiting_approval",
                           pending_approval=pending)
        return task.id, pending["id"]

    def test_session_decision_grants_and_resolves(self):
        from localcodeagent.server import stop_state
        with tempfile.TemporaryDirectory() as td:
            state = self._state(td, {"filesystem.write": "ask"})
            try:
                task_id, aid = self._park(state)
                card = state.resolve_chat_approval(task_id, aid, "session")
                self.assertEqual(card["id"], aid)
                # Session grant applied through PermissionManager —
                # the same object Settings reads.
                self.assertEqual(
                    state.permission_manager.effective("filesystem.write"),
                    "allow")
                task = state.tasks.get(task_id)
                rows = task.approval_resolutions
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["decision"], "session")
            finally:
                stop_state(state)

    def test_session_grant_does_not_persist_to_config(self):
        from localcodeagent.server import stop_state
        with tempfile.TemporaryDirectory() as td:
            state = self._state(td, {"filesystem.write": "ask"})
            try:
                task_id, aid = self._park(state)
                state.resolve_chat_approval(task_id, aid, "session")
                # Raw level unchanged — grants live only in the process.
                self.assertEqual(
                    state.permission_manager.level("filesystem.write"),
                    "ask")
            finally:
                stop_state(state)

    def test_always_decision_persists_level(self):
        from localcodeagent.server import stop_state
        with tempfile.TemporaryDirectory() as td:
            state = self._state(td, {"filesystem.write": "ask"})
            try:
                task_id, aid = self._park(state)
                state.resolve_chat_approval(task_id, aid, "always")
                self.assertEqual(
                    state.permission_manager.level("filesystem.write"),
                    "allow")
                # Persisted where Settings reads it — a fresh manager on
                # the same config file sees the grant after restart.
                import json
                raw = json.loads(state.config_path.read_text(
                    encoding="utf-8"))
                self.assertEqual(
                    raw["permissions"]["filesystem.write"], "allow")
            finally:
                stop_state(state)

    def test_deny_decision_records_resolution(self):
        from localcodeagent.server import stop_state
        with tempfile.TemporaryDirectory() as td:
            state = self._state(td, {"filesystem.write": "ask"})
            try:
                task_id, aid = self._park(state)
                state.resolve_chat_approval(task_id, aid, "deny")
                task = state.tasks.get(task_id)
                self.assertEqual(
                    task.approval_resolutions[0]["decision"], "deny")
                # Audit trail through PermissionManager's persisted log.
                import json as _json
                audit = (state.runtime_root / "data"
                         / "permission_audit.jsonl")
                kinds = [
                    _json.loads(line).get("event")
                    for line in audit.read_text(
                        encoding="utf-8").splitlines() if line.strip()]
                self.assertIn("approval_denied", kinds)
            finally:
                stop_state(state)

    def test_duplicate_decision_is_stale(self):
        from localcodeagent.approvals import ApprovalError
        from localcodeagent.server import stop_state
        with tempfile.TemporaryDirectory() as td:
            state = self._state(td, {"filesystem.write": "ask"})
            try:
                task_id, aid = self._park(state)
                state.resolve_chat_approval(task_id, aid, "session")
                # Simulate the parked state remaining (resume failed) —
                # the resolution id still rejects a replayed decision.
                with self.assertRaises(ApprovalError) as cm:
                    state.resolve_chat_approval(task_id, aid, "session")
                self.assertTrue(cm.exception.stale)
            finally:
                stop_state(state)

    def test_wrong_approval_id_is_stale(self):
        from localcodeagent.approvals import ApprovalError
        from localcodeagent.server import stop_state
        with tempfile.TemporaryDirectory() as td:
            state = self._state(td, {"filesystem.write": "ask"})
            try:
                task_id, _ = self._park(state)
                with self.assertRaises(ApprovalError) as cm:
                    state.resolve_chat_approval(
                        task_id, "ap-not-the-one", "session")
                self.assertTrue(cm.exception.stale)
            finally:
                stop_state(state)

    def test_missing_task_is_stale(self):
        from localcodeagent.approvals import ApprovalError
        from localcodeagent.server import stop_state
        with tempfile.TemporaryDirectory() as td:
            state = self._state(td, {"filesystem.write": "ask"})
            try:
                with self.assertRaises(ApprovalError) as cm:
                    state.resolve_chat_approval("no-such", "", "deny")
                self.assertTrue(cm.exception.stale)
            finally:
                stop_state(state)

    def test_denied_policy_offers_no_decision(self):
        from localcodeagent.approvals import ApprovalError
        from localcodeagent.server import stop_state
        with tempfile.TemporaryDirectory() as td:
            state = self._state(td, {"filesystem.write": "deny"})
            try:
                task_id, aid = self._park(state)
                with self.assertRaises(ApprovalError) as cm:
                    state.resolve_chat_approval(task_id, aid, "session")
                self.assertEqual(cm.exception.status, 403)
                self.assertIn("disabled", str(cm.exception))
            finally:
                stop_state(state)

    def test_illegal_decision_for_permission_rejected(self):
        from localcodeagent.approvals import ApprovalError
        from localcodeagent.server import stop_state
        with tempfile.TemporaryDirectory() as td:
            state = self._state(td, {"spend.money": "ask"})
            try:
                task_id, aid = self._park(state, permission="spend.money")
                with self.assertRaises(ApprovalError) as cm:
                    state.resolve_chat_approval(task_id, aid, "always")
                self.assertEqual(cm.exception.status, 403)
            finally:
                stop_state(state)

    def test_unknown_decision_rejected(self):
        from localcodeagent.approvals import ApprovalError
        from localcodeagent.server import stop_state
        with tempfile.TemporaryDirectory() as td:
            state = self._state(td, {"filesystem.write": "ask"})
            try:
                task_id, aid = self._park(state)
                with self.assertRaises(ApprovalError) as cm:
                    state.resolve_chat_approval(task_id, aid, "forever")
                self.assertEqual(cm.exception.status, 400)
            finally:
                stop_state(state)


class EndToEndDecisionTests(unittest.TestCase):
    """The full chat path: park → card → decision → exact-step resume."""

    def test_decide_then_resume_executes_once(self):
        from localcodeagent.approvals import ApprovalError
        from localcodeagent.server import AppState, stop_state
        with tempfile.TemporaryDirectory() as td:
            cfg = AgentConfig(
                models=[ModelProfile(
                    id="ext", endpoint="http://x/v1", model="m",
                    roles=["primary_coder", "fast_coder", "deep_reasoner",
                           "reviewer"], runtime="external")],
                permissions={"test.execute": "ask"},
                auto_verify_after_changes=False,
                review_after_changes=False)
            state = AppState(cfg, Path(td), Path(td) / ".runtime")
            try:
                seen = []
                state.tools.register(ToolSpec(
                    "dangerous_test_tool", "test",
                    {"type": "object",
                     "properties": {"value": {"type": "string"}},
                     "required": ["value"]},
                    "test.execute",
                    lambda args: seen.append(args["value"]) or "OK"))
                provider = _Scripted()
                state.agent._provider_for = lambda _: provider
                first = state.agent.run("do the thing")
                task_id = first.task["id"]
                pending = first.task["pending_approval"]
                self.assertEqual(first.task["status"], "waiting_approval")
                aid = pending["id"]

                # Chat card decision → grant applies → resume runs the
                # exact parked call, exactly once.
                state.resolve_chat_approval(task_id, aid, "session")
                second = state.agent.resume(task_id, approved=True)
                self.assertEqual(seen, ["ok"])
                self.assertEqual(second.content, "finished")
                self.assertEqual(second.task["status"], "completed")

                # A replayed decision against the consumed approval is
                # stale — the action must not execute a second time.
                with self.assertRaises(ApprovalError) as cm:
                    state.resolve_chat_approval(task_id, aid, "session")
                self.assertTrue(cm.exception.stale)
                self.assertEqual(seen, ["ok"])
            finally:
                stop_state(state)

    def test_deny_skips_execution_and_dependents(self):
        from localcodeagent.server import AppState, stop_state
        with tempfile.TemporaryDirectory() as td:
            cfg = AgentConfig(
                models=[ModelProfile(
                    id="ext", endpoint="http://x/v1", model="m",
                    roles=["primary_coder", "fast_coder", "deep_reasoner",
                           "reviewer"], runtime="external")],
                permissions={"test.execute": "ask"},
                auto_verify_after_changes=False,
                review_after_changes=False)
            state = AppState(cfg, Path(td), Path(td) / ".runtime")
            try:
                seen = []
                state.tools.register(ToolSpec(
                    "dangerous_test_tool", "test",
                    {"type": "object",
                     "properties": {"value": {"type": "string"}},
                     "required": ["value"]},
                    "test.execute",
                    lambda args: seen.append(args["value"]) or "OK"))
                provider = _Scripted()
                state.agent._provider_for = lambda _: provider
                first = state.agent.run("do the thing")
                task_id = first.task["id"]
                aid = first.task["pending_approval"]["id"]

                state.resolve_chat_approval(task_id, aid, "deny")
                # Denial resumes as refused — the tool body never runs.
                second = state.agent.resume(task_id, approved=False)
                self.assertEqual(seen, [])
                self.assertNotEqual(
                    second.task["status"], "waiting_approval")
            finally:
                stop_state(state)


class CancellationTests(unittest.TestCase):
    def test_orchestrator_stop_cancels_pending_approval(self):
        mixin = _OrchestratorMixin()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            agent, tools = mixin._build(root, {"test.execute": "ask"})
            tools.register(ToolSpec(
                "dangerous_test_tool", "test",
                {"type": "object",
                 "properties": {"value": {"type": "string"}},
                 "required": ["value"]},
                "test.execute", lambda args: "OK"))
            agent._provider_for = lambda _: _Scripted()
            result = agent.run("do the thing")
            task_id = result.task["id"]
            self.assertEqual(result.task["status"], "waiting_approval")
            agent._command_stop()
            task = agent.tasks.get(task_id)
            self.assertEqual(task.status, "cancelled")
            self.assertIsNone(task.pending_approval)
            # The card can render its cancelled state from the row.
            rows = task.approval_resolutions
            self.assertEqual(rows[-1]["decision"], "cancelled")


class SessionGrantLifecycleTests(unittest.TestCase):
    def test_session_grant_survives_within_process_only(self):
        mgr = PermissionManager({"filesystem.write": "session"})
        self.assertEqual(mgr.effective("filesystem.write"), "ask")
        mgr.grant_session("filesystem.write")
        self.assertEqual(mgr.effective("filesystem.write"), "allow")
        # A fresh manager (process restart) has no session grants —
        # nothing was written to durable config.
        mgr2 = PermissionManager({"filesystem.write": "session"})
        self.assertEqual(mgr2.effective("filesystem.write"), "ask")


if __name__ == "__main__":
    unittest.main()
