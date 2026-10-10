"""Live conversation dogfood — real chat stack, real model.

The scripted ``ConversationQaRunner`` proves routing/context/memory
decisions deterministically; it cannot prove what a live model actually
says. This module drives the deployed backend's ``/api/chat`` endpoint —
the same path the desktop UI uses — so defects surface exactly where the
user experiences them.

Per-turn ``expect`` keys (subset of the scripted runner, plus live-only):

    response_contains / response_not_contains   — lexical checks
    response_regex / response_not_regex         — pattern checks
    task_status                                  — task["status"]
    no_trailing_question / no_leading_filler /
    no_reasoning_narration / max_sentences / max_chars / scope
                                                 — scope.py metrics
    no_tools                                     — tool_events must be empty
    no_mutating_tools                            — only read-only permission
                                                   classes may run (a search or
                                                   page read to ground an answer
                                                   is fine; acting on the world
                                                   is not)
    tool_used                                    — tool_events non-empty and
                                                   containing this name
    model_called / no_model_call                 — model_events presence
    note                                         — free-text reviewer note
                                                  echoed into the report

Failures append to a FailureCorpus (the defect ledger); ``report`` renders
runs + aggregate metrics as markdown for the .dogfood record.
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Any

from .conversation import QaScenario, QaTurn, TurnResult, QaRunResult
from .corpus import CorpusEntry, FailureCorpus


@dataclass
class LiveReply:
    content: str = ""
    task_status: str = ""
    task_id: str = ""
    tool_events: list = field(default_factory=list)
    model_events: list = field(default_factory=list)
    elapsed_ms: float = 0.0
    http_status: int = 0
    error: str = ""
    queued: bool = False
    queue_id: str = ""


_TERMINAL = {"completed", "done", "failed", "error", "cancelled",
             "needs_review"}


class LiveSession:
    """One conversation against a running Nexus backend."""

    def __init__(self, base_url: str, *, timeout: float = 180.0,
                 queue_timeout: float = 240.0) -> None:
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.queue_timeout = queue_timeout

    def _post(self, path: str, body: dict) -> tuple[int, dict]:
        req = urllib.request.Request(
            self.base + path, data=json.dumps(body).encode("utf-8"),
            method="POST", headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read().decode("utf-8"))
            except Exception:
                return exc.code, {"error": str(exc)}
        except Exception as exc:
            return 0, {"error": str(exc)}

    def _get(self, path: str) -> dict:
        try:
            with urllib.request.urlopen(
                    self.base + path, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:
            return {}

    def say(self, text: str) -> LiveReply:
        started = time.perf_counter()
        status, data = self._post("/api/chat", {"message": text})
        elapsed = (time.perf_counter() - started) * 1000.0
        task = data.get("task") or {}
        queue_item = data.get("queue_item") or {}
        return LiveReply(
            content=str(data.get("content") or ""),
            task_status=str(task.get("status") or ""),
            task_id=str(task.get("id") or ""),
            tool_events=list(data.get("tool_events") or []),
            model_events=list(data.get("model_events") or []),
            elapsed_ms=elapsed, http_status=status,
            error=str(data.get("error") or ""),
            queued=bool(data.get("queued")),
            queue_id=str(queue_item.get("id") or ""),
        )

    def tasks(self) -> dict:
        return self._get("/api/tasks")

    def active_tasks(self) -> list[dict]:
        """Non-terminal tasks — the single-flight lane occupants."""
        payload = self.tasks()
        rows = []
        cur = payload.get("current")
        if isinstance(cur, dict):
            rows.append(cur)
        for row in payload.get("recent") or []:
            if str(row.get("status") or "") not in _TERMINAL:
                rows.append(row)
        return rows

    def cancel_active(self) -> int:
        """Cooperatively cancel every non-terminal task. Returns count."""
        cancelled = 0
        for row in self.active_tasks():
            tid = str(row.get("id") or "")
            if not tid:
                continue
            status, _ = self._post("/api/jobs/cancel",
                                   {"job_id": f"task-{tid}"})
            if status == 200:
                cancelled += 1
        return cancelled

    def drain_queue(self) -> int:
        """Remove every pending queue item. Returns count removed."""
        items = list(self.tasks().get("queue") or [])
        if not items:
            items = list(self._get("/api/queue").get("items") or [])
        removed = 0
        for item in items:
            iid = str(item.get("id") or "")
            if not iid:
                continue
            status, data = self._post("/api/queue/cancel", {"id": iid})
            if status == 200 and data.get("ok"):
                removed += 1
        return removed

    def isolate(self) -> None:
        """Reset to a clean lane: cancel active work, drain the queue,
        reset conversation context. A scenario must never inherit a
        phantom task from the previous scenario (or from autonomy)."""
        self.cancel_active()
        self.drain_queue()
        self.reset()

    def wait_for_prompt(self, prompt: str, *,
                        timeout: float | None = None) -> dict | None:
        """Poll the task ledger until the task spawned for ``prompt``
        reaches a terminal status. Queued chat replies execute
        asynchronously — the queued notice is not the answer."""
        deadline = time.perf_counter() + (timeout or self.queue_timeout)
        while time.perf_counter() < deadline:
            for row in self.tasks().get("recent") or []:
                if str(row.get("prompt") or "") != prompt:
                    continue
                if str(row.get("status") or "") in _TERMINAL:
                    return row
            time.sleep(2.0)
        return None

    def reset(self) -> None:
        self._post("/api/chat/reset", {})

    def approve(self, approval_id: str, decision: str = "approve") -> dict:
        _, data = self._post("/api/approval/respond",
                             {"id": approval_id, "decision": decision})
        return data


class LiveRunner:
    """Runs QaScenario turns through LiveSession and evaluates expects."""

    def __init__(self, session: LiveSession, *,
                 corpus: FailureCorpus | None = None) -> None:
        self.session = session
        self.corpus = corpus

    def run(self, scenario: QaScenario, *, reset: bool = True) -> QaRunResult:
        if reset:
            self.session.isolate()
        results: list[TurnResult] = []
        transcript: list[dict[str, str]] = []
        for index, turn in enumerate(scenario.turns):
            reply = self.session.say(turn.text)
            if reply.queued:
                # A queued notice is not the answer — the prompt executes
                # asynchronously once the lane frees. Wait for the spawned
                # task to terminate and use its real result.
                row = self.session.wait_for_prompt(turn.text)
                if row is None:
                    reply.error = (reply.error or
                                   "queued_item_unresolved")
                else:
                    reply.content = str(
                        row.get("final_content") or
                        row.get("summary") or reply.content or "")
                    reply.task_status = str(row.get("status") or "")
                    reply.task_id = str(row.get("id") or reply.task_id)
            tr = TurnResult(index=index, text=turn.text,
                            conversation_id=scenario.default_conversation_id,
                            failures=[], response=reply.content,
                            task_status=reply.task_status,
                            elapsed_ms=reply.elapsed_ms)
            tr.tool_calls = len(reply.tool_events)
            try:
                from ..context.scope import classify_scope, scope_metrics
                tr.metrics = scope_metrics(
                    reply.content, classify_scope(turn.text))
            except Exception:
                tr.metrics = {}
            tr.metrics["tool_events"] = [
                str(e.get("name") or e.get("tool") or e)[:60]
                for e in reply.tool_events[:8]]
            tr.metrics["model_events"] = len(reply.model_events)
            tr.metrics["http_status"] = reply.http_status
            failures = self._evaluate(turn, tr, reply)
            tr.failures = failures
            transcript.append({"role": "user", "content": turn.text})
            transcript.append({"role": "assistant", "content": reply.content})
            results.append(tr)
        run = QaRunResult(scenario.scenario_id, results)
        if self.corpus is not None and not run.ok:
            self._record(scenario, run, transcript)
        return run

    def _evaluate(self, turn: QaTurn, tr: TurnResult, reply: LiveReply) -> list[str]:
        expect = turn.expect or {}
        out: list[str] = []
        if reply.http_status and reply.http_status >= 400:
            out.append(f"http_status: {reply.http_status} {reply.error!r}")
        elif reply.error and reply.error != "None":
            out.append(f"error: {reply.error}")
        if not reply.content and not reply.error:
            out.append("empty_response")

        def _need(key, haystack, label, negate=False, regex=False):
            vals = expect.get(key)
            if not vals:
                return
            if isinstance(vals, str):
                vals = [vals]
            for v in vals:
                found = (re.search(str(v), haystack, re.I) is not None
                         if regex else str(v).lower() in haystack.lower())
                if negate and found:
                    out.append(f"{label}: unexpected {v!r} present")
                elif not negate and not found:
                    out.append(f"{label}: missing {v!r}")

        _need("response_contains", tr.response, "response")
        _need("response_not_contains", tr.response, "response", negate=True)
        _need("response_regex", tr.response, "response", regex=True)
        _need("response_not_regex", tr.response, "response", negate=True,
              regex=True)
        if "task_status" in expect and tr.task_status != expect["task_status"]:
            out.append(f"task_status: expected {expect['task_status']!r}, "
                       f"got {tr.task_status!r}")
        if expect.get("no_tools") and reply.tool_events:
            out.append(f"no_tools: ran {[tr.metrics['tool_events']]}")
        if expect.get("no_mutating_tools"):
            try:
                from ..agent.orchestrator import AgentOrchestrator
                read_only = AgentOrchestrator._READ_ONLY_TOOL_PERMS
            except Exception:
                read_only = frozenset({"filesystem.read"})
            mutating = [
                str(e.get("name") or e.get("tool") or e)[:60]
                for e in reply.tool_events
                if str(e.get("permission") or "") not in read_only
            ]
            if mutating:
                out.append(f"no_mutating_tools: ran {mutating}")
        if "tool_used" in expect:
            names = " ".join(tr.metrics["tool_events"]).lower()
            if str(expect["tool_used"]).lower() not in names:
                out.append(f"tool_used: expected {expect['tool_used']!r}, "
                           f"got {tr.metrics['tool_events']}")

        m = tr.metrics or {}
        if "max_sentences" in expect and int(m.get("sentences") or 0) > int(expect["max_sentences"]):
            out.append(f"max_sentences: >{expect['max_sentences']} (got {m.get('sentences')})")
        if "max_chars" in expect and int(m.get("chars") or 0) > int(expect["max_chars"]):
            out.append(f"max_chars: >{expect['max_chars']} (got {m.get('chars')})")
        if expect.get("no_leading_filler") and m.get("leading_filler"):
            out.append(f"no_leading_filler: {m['leading_filler']!r}")
        if expect.get("no_reasoning_narration") and m.get("reasoning_narration"):
            out.append(f"no_reasoning_narration: {m['reasoning_narration']!r}")
        if expect.get("no_trailing_question") and m.get("trailing_question"):
            out.append("no_trailing_question: ends with '?'")
        return out

    def _record(self, scenario: QaScenario, run: QaRunResult,
                transcript: list[dict[str, str]]) -> None:
        for tr in run.turns:
            for fail in tr.failures:
                category = fail.split(":", 1)[0].strip()
                self.corpus.record(CorpusEntry(
                    category={
                        "no_tools": "unexpected_tool_call",
                        "no_mutating_tools": "unexpected_tool_call",
                        "tool_used": "bad_tool_choice",
                        "task_status": "task_not_completed",
                        "response": "irrelevant_answer",
                    }.get(category, "unexpected_error"),
                    conversation=list(transcript),
                    failed_turn_index=tr.index,
                    user_text=tr.text,
                    expected=str((scenario.turns[tr.index].expect or {})),
                    actual=f"{fail} | reply: {tr.response[:300]}",
                    scenario_id=scenario.scenario_id,
                ))


def render_report(runs: list[QaRunResult]) -> str:
    """Markdown dogfood report — per-scenario failures + transcript tails."""
    from .conversation import aggregate_metrics
    lines = ["# Conversation Dogfood Report", ""]
    agg = aggregate_metrics(runs)
    lines.append(f"- turns: {agg.get('turns')}  answered: {agg.get('answered')}")
    lines.append(f"- model calls: {agg.get('model_calls')}  "
                 f"builtin: {agg.get('builtin_answers')}")
    lines.append(f"- latency median/p90 ms: {agg.get('median_latency_ms')}"
                 f"/{agg.get('p90_latency_ms')}")
    lines.append(f"- filler {agg.get('filler_rate')}%  "
                 f"trailing-q {agg.get('trailing_question_rate')}%  "
                 f"over-budget {agg.get('over_budget_rate')}%  "
                 f"narration {agg.get('reasoning_narration_rate')}%")
    lines.append(f"- depth: {agg.get('depth_histogram')}")
    lines.append("")
    for run in runs:
        status = "OK" if run.ok else f"{sum(len(t.failures) for t in run.turns)} failures"
        lines.append(f"## {run.scenario_id} — {status}")
        for t in run.turns:
            if not t.failures:
                continue
            lines.append(f"- turn {t.index} `{t.text[:80]}`")
            for f in t.failures:
                lines.append(f"    - {f}")
            lines.append(f"    - reply: {t.response[:240]!r}")
        lines.append("")
    return "\n".join(lines)
