from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..config import AgentConfig, ModelProfile
from ..models.openai_compat import OpenAICompatibleProvider
from ..models.router import ModelRouter, RoutingDecision
from ..runtime.manager import RuntimeManager
from ..tools.base import ToolRegistry
from ..workflow.checkpoint import CheckpointManager
from ..workflow.memory import ProjectMemory
from ..workflow.repository import RepositoryIndex
from ..workflow.tasks import TaskStore
from ..workflow.verify import detect_verification_commands


SYSTEM_PROMPT = """You are Local Code Agent, a local-first software engineering agent.
Work carefully inside the selected workspace. Inspect before editing. Prefer small, verifiable changes.
Use tools when they are needed. Prefer apply_patch over whole-file replacement when editing existing files.
After code changes, run appropriate tests or builds when permissions allow.
Never claim a tool succeeded unless its result says it succeeded. If a permission requires approval, execution will pause for the user.
Use repository search/index tools to locate relevant code before guessing. Do not modify .agent metadata directly.
When the user asks to generate or edit an image, use the image tools automatically instead of merely describing a workflow.
Image tools have their own local model router, so the chat/coding model should not guess an image model unless the user explicitly overrides it.
Use web_search/fetch_url when current external information, documentation, releases, errors, or APIs materially affect the answer. Cite the source URLs you actually used. Use browser_run only when interaction or JavaScript rendering is needed.
"""

REVIEW_PROMPT = """You are the reviewer for a local coding agent. Review the supplied task and patch for correctness,
regressions, missed requirements, security problems, and test gaps. Be concise and concrete. If you find no material issue,
start the response with PASS. Otherwise start with FINDINGS and list the important issues. Do not invent files or behavior not
shown in the patch/context.
"""


@dataclass(slots=True)
class AgentResult:
    content: str
    routing: RoutingDecision
    tool_events: list[dict[str, Any]] = field(default_factory=list)
    model_events: list[dict[str, Any]] = field(default_factory=list)
    steps: int = 0
    task: dict[str, Any] = field(default_factory=dict)
    pending_approval: dict[str, Any] | None = None
    verification: list[dict[str, Any]] = field(default_factory=list)
    review: str = ""


@dataclass(slots=True)
class _AgentSession:
    task_id: str
    user_text: str
    mode: str
    messages: list[dict[str, Any]]
    decision: RoutingDecision
    profile: ModelProfile
    provider: OpenAICompatibleProvider
    tool_events: list[dict[str, Any]] = field(default_factory=list)
    model_events: list[dict[str, Any]] = field(default_factory=list)
    steps: int = 0
    failures: int = 0
    pending_calls: list[dict[str, Any]] = field(default_factory=list)
    pending_call_index: int = 0
    pending_approval: dict[str, Any] | None = None
    main_content: str = ""
    verification_commands: list[dict[str, str]] = field(default_factory=list)
    verification_index: int = 0
    verification_done: bool = False
    review_done: bool = False
    review_content: str = ""


class AgentOrchestrator:
    def __init__(
        self,
        config: AgentConfig,
        router: ModelRouter,
        tools: ToolRegistry,
        runtime: RuntimeManager,
        *,
        tasks: TaskStore,
        checkpoints: CheckpointManager,
        memory: ProjectMemory,
        repository_index: RepositoryIndex,
    ) -> None:
        self.config = config
        self.router = router
        self.tools = tools
        self.runtime = runtime
        self.tasks = tasks
        self.checkpoints = checkpoints
        self.memory = memory
        self.repository_index = repository_index
        self._sessions: dict[str, _AgentSession] = {}

    def _provider_for(self, profile: ModelProfile) -> OpenAICompatibleProvider:
        endpoint = self.runtime.ensure_ready(profile)
        return OpenAICompatibleProvider(profile, endpoint=endpoint)

    def _complete_with_recovery(
        self,
        provider: OpenAICompatibleProvider,
        profile: ModelProfile,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model_events: list[dict[str, Any]],
    ):
        attempts = 0
        while True:
            try:
                return provider.complete(messages=messages, tools=tools)
            except RuntimeError as exc:
                if profile.runtime != "llama_cpp" or attempts >= self.config.runtime_recovery_attempts:
                    raise
                attempts += 1
                endpoint = self.runtime.recover(profile)
                provider = OpenAICompatibleProvider(profile, endpoint=endpoint)
                model_events.append({
                    "type": "runtime_recovery",
                    "model_id": profile.id,
                    "attempt": attempts,
                    "reason": str(exc),
                })

    @staticmethod
    def _parse_call(call: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        fn = call.get("function", {})
        name = str(fn.get("name", ""))
        raw_args = fn.get("arguments") or "{}"
        try:
            args = raw_args if isinstance(raw_args, dict) else json.loads(raw_args)
            if not isinstance(args, dict):
                args = {}
        except (json.JSONDecodeError, TypeError):
            args = {}
        return name, args

    def _task_context(self, task_id: str) -> None:
        self.tools.context["task_id"] = task_id

    def _result(self, session: _AgentSession, content: str | None = None) -> AgentResult:
        task = self.tasks.get(session.task_id)
        return AgentResult(
            content=session.main_content if content is None else content,
            routing=session.decision,
            tool_events=list(session.tool_events),
            model_events=list(session.model_events),
            steps=session.steps,
            task=task.as_dict(),
            pending_approval=session.pending_approval,
            verification=list(task.verification),
            review=session.review_content or task.review,
        )

    def _pause_for_approval(
        self,
        session: _AgentSession,
        *,
        kind: str,
        name: str,
        arguments: dict[str, Any],
        permission: str,
        call_id: str = "",
        detail: str = "",
    ) -> AgentResult:
        pending = {
            "kind": kind,
            "name": name,
            "arguments": arguments,
            "permission": permission,
            "call_id": call_id,
            "detail": detail,
        }
        session.pending_approval = pending
        self.tasks.update(session.task_id, status="waiting_approval", phase="waiting_approval", pending_approval=pending)
        return self._result(
            session,
            f"Approval required to run {name} ({permission}). Approve or deny the pending action to continue this task.",
        )

    def _append_tool_result(self, session: _AgentSession, call: dict[str, Any], name: str, args: dict[str, Any], result: str) -> None:
        if result.startswith(("ERROR", "PERMISSION_DENIED")):
            session.failures += 1
        session.tool_events.append({"name": name, "arguments": args, "result": result})
        session.messages.append({
            "role": "tool",
            "tool_call_id": call.get("id", name),
            "name": name,
            "content": result,
        })
        task = self.tasks.get(session.task_id)
        self.tasks.update(session.task_id, steps=session.steps, files_changed=list(task.files_changed))

    def _maybe_escalate(self, session: _AgentSession) -> None:
        if session.mode != "auto" or session.failures < 2 or session.decision.role == "deep_reasoner":
            return
        task = self.tasks.get(session.task_id)
        escalated = self.router.choose(
            session.user_text,
            changed_files=len(task.files_changed),
            failures=session.failures,
        )
        if escalated.model_id == session.decision.model_id:
            return
        previous = session.decision.model_id
        session.decision = escalated
        session.profile = self.router.get_profile(escalated.model_id)
        session.provider = self._provider_for(session.profile)
        session.model_events.append({
            "type": "switch",
            "from": previous,
            "to": escalated.model_id,
            "role": escalated.role,
            "reason": "repeated tool failures",
        })
        session.messages.append({
            "role": "system",
            "content": "The previous model encountered repeated tool failures. Re-evaluate the problem carefully before continuing.",
        })
        self.tasks.update(session.task_id, model_id=escalated.model_id, model_role=escalated.role)

    def _process_pending_calls(self, session: _AgentSession) -> AgentResult | None:
        while session.pending_call_index < len(session.pending_calls):
            call = session.pending_calls[session.pending_call_index]
            name, args = self._parse_call(call)
            requires, permission = self.tools.requires_approval(name)
            if requires:
                return self._pause_for_approval(
                    session,
                    kind="tool",
                    name=name,
                    arguments=args,
                    permission=permission,
                    call_id=str(call.get("id", name)),
                )
            result = self.tools.execute(name, args)
            self._append_tool_result(session, call, name, args, result)
            session.pending_call_index += 1
            self._maybe_escalate(session)
        session.pending_calls = []
        session.pending_call_index = 0
        return None

    def _run_verification(self, session: _AgentSession) -> AgentResult | None:
        task = self.tasks.get(session.task_id)
        if not task.files_changed or not self.config.auto_verify_after_changes:
            session.verification_done = True
            return None
        if not session.verification_commands:
            session.verification_commands = detect_verification_commands(self.checkpoints.workspace)
        if not session.verification_commands:
            session.verification_done = True
            return None

        self.tasks.update(session.task_id, status="verifying", phase="verifying")
        while session.verification_index < len(session.verification_commands):
            item = session.verification_commands[session.verification_index]
            args = {"command": item["command"], "timeout": 300}
            requires, permission = self.tools.requires_approval("run_shell")
            if requires:
                return self._pause_for_approval(
                    session,
                    kind="verification",
                    name=item["name"],
                    arguments=args,
                    permission=permission,
                    detail=item["command"],
                )
            result = self.tools.execute("run_shell", args)
            entry = {"name": item["name"], "command": item["command"], "result": result}
            task = self.tasks.get(session.task_id)
            self.tasks.update(session.task_id, verification=[*task.verification, entry])
            session.tool_events.append({"name": "run_shell", "arguments": args, "result": result, "phase": "verification"})
            session.verification_index += 1
        session.verification_done = True
        return None

    def _run_review(self, session: _AgentSession) -> None:
        if session.review_done:
            return
        task = self.tasks.get(session.task_id)
        if not task.files_changed or not self.config.review_after_changes:
            session.review_done = True
            return
        diff = self.checkpoints.diff(session.task_id, max_chars=self.config.max_review_chars)
        if not diff.strip():
            session.review_done = True
            return

        self.tasks.update(session.task_id, status="reviewing", phase="reviewing")
        review_decision = self.router.choose(
            session.user_text,
            phase="review",
            changed_files=len(task.files_changed),
        )
        review_profile = self.router.get_profile(review_decision.model_id)
        review_provider = self._provider_for(review_profile)
        if review_decision.model_id != session.decision.model_id:
            session.model_events.append({
                "type": "switch",
                "from": session.decision.model_id,
                "to": review_decision.model_id,
                "role": "reviewer",
                "reason": "post-change review",
            })
        else:
            session.model_events.append({
                "type": "review",
                "model_id": review_decision.model_id,
                "role": "reviewer",
                "reason": "post-change review",
            })
        try:
            response = self._complete_with_recovery(
                review_provider,
                review_profile,
                messages=[
                    {"role": "system", "content": REVIEW_PROMPT},
                    {
                        "role": "user",
                        "content": f"Original task:\n{session.user_text}\n\nChanged files:\n{', '.join(task.files_changed)}\n\nPatch:\n{diff}",
                    },
                ],
                tools=None,
                model_events=session.model_events,
            )
            session.review_content = str(response.message.get("content") or "")
        except Exception as exc:
            session.review_content = f"Review unavailable: {type(exc).__name__}: {exc}"
        session.review_done = True
        self.tasks.update(session.task_id, review=session.review_content)

    def _finalize(self, session: _AgentSession) -> AgentResult:
        pending = self._run_verification(session)
        if pending:
            return pending
        self._run_review(session)

        task = self.tasks.get(session.task_id)
        verification_failed = any("EXIT_CODE=0" not in str(item.get("result", "")) for item in task.verification)
        status = "completed_with_warnings" if verification_failed else "completed"
        self.tasks.update(
            session.task_id,
            status=status,
            phase="done",
            pending_approval=None,
            summary=session.main_content[:4000],
            review=session.review_content,
            steps=session.steps,
        )
        task = self.tasks.get(session.task_id)
        self.memory.remember_task(
            task_id=task.id,
            prompt=task.prompt,
            summary=task.summary,
            files_changed=task.files_changed,
            review=task.review,
        )
        if task.files_changed:
            try:
                self.repository_index.build()
            except Exception:
                pass
        self._sessions.pop(session.task_id, None)
        return self._result(session)

    def _drive(self, session: _AgentSession) -> AgentResult:
        self._task_context(session.task_id)
        self.tasks.update(session.task_id, status="running", phase="working", pending_approval=None)
        session.pending_approval = None

        while session.steps < self.config.max_agent_steps:
            paused = self._process_pending_calls(session)
            if paused:
                return paused

            response = self._complete_with_recovery(
                session.provider,
                session.profile,
                messages=session.messages,
                tools=self.tools.schemas(),
                model_events=session.model_events,
            )
            session.steps += 1
            message = response.message
            session.messages.append(message)
            calls = message.get("tool_calls") or []
            self.tasks.update(session.task_id, steps=session.steps)
            if not calls:
                session.main_content = str(message.get("content") or "")
                return self._finalize(session)

            session.pending_calls = list(calls)
            session.pending_call_index = 0

        session.main_content = "Agent stopped after reaching the configured step limit. Review the tool log and continue if needed."
        self.tasks.update(
            session.task_id,
            status="step_limit",
            phase="done",
            summary=session.main_content,
            steps=session.steps,
        )
        self._sessions.pop(session.task_id, None)
        return self._result(session)

    def run(self, user_text: str, *, history: list[dict[str, Any]] | None = None, mode: str = "auto") -> AgentResult:
        self.runtime.refresh_hardware()
        task = self.tasks.create(user_text, mode)
        self._task_context(task.id)
        self.tasks.update(task.id, phase="planning")

        decision = self.router.choose(user_text, override=mode)
        profile = self.router.get_profile(decision.model_id)
        provider = self._provider_for(profile)
        project_memory = self.memory.context()
        index_summary = self.repository_index.ensure()
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "system",
                "content": f"Workspace memory:\n{project_memory}\n\nRepository index: {index_summary.get('file_count', 0)} indexed files.",
            },
        ]
        if history:
            messages.extend(history[-24:])
        messages.append({"role": "user", "content": user_text})
        session = _AgentSession(
            task_id=task.id,
            user_text=user_text,
            mode=mode,
            messages=messages,
            decision=decision,
            profile=profile,
            provider=provider,
            model_events=[{
                "type": "selected",
                "model_id": decision.model_id,
                "role": decision.role,
                "reasons": decision.reasons,
            }],
        )
        self._sessions[task.id] = session
        self.tasks.update(task.id, model_id=decision.model_id, model_role=decision.role)
        try:
            return self._drive(session)
        except Exception as exc:
            self.tasks.update(task.id, status="error", phase="done", error=f"{type(exc).__name__}: {exc}")
            self._sessions.pop(task.id, None)
            raise

    def resume(self, task_id: str, *, approved: bool) -> AgentResult:
        session = self._sessions.get(task_id)
        if session is None or not session.pending_approval:
            raise KeyError(f"No resumable approval is pending for task {task_id}")
        pending = session.pending_approval
        session.pending_approval = None
        self._task_context(task_id)

        if pending["kind"] == "tool":
            call = session.pending_calls[session.pending_call_index]
            name, args = self._parse_call(call)
            result = self.tools.execute(name, args, approved=True) if approved else f"PERMISSION_DENIED: user denied {pending['permission']} for {name}"
            self._append_tool_result(session, call, name, args, result)
            session.pending_call_index += 1
            self._maybe_escalate(session)
            return self._drive(session)

        if pending["kind"] == "verification":
            item = session.verification_commands[session.verification_index]
            args = pending["arguments"]
            result = self.tools.execute("run_shell", args, approved=True) if approved else f"PERMISSION_DENIED: user skipped verification command {item['name']}"
            entry = {"name": item["name"], "command": item["command"], "result": result}
            task = self.tasks.get(task_id)
            self.tasks.update(task_id, verification=[*task.verification, entry], pending_approval=None)
            session.tool_events.append({"name": "run_shell", "arguments": args, "result": result, "phase": "verification"})
            session.verification_index += 1
            return self._finalize(session)

        raise ValueError(f"Unknown approval kind {pending['kind']}")
