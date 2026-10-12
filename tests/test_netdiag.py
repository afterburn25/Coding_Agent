"""Backend transport-failure diagnostics and recovery.

Reproduces the WinError 10054 class of failures: a local backend
(llama.cpp/ComfyUI) resetting, aborting, or truncating a connection must
surface as a classified BackendConnectionError with subsystem/endpoint/
timing context — never a bare OSError — and must drive bounded recovery
without ever duplicating already-streamed output."""
from __future__ import annotations

import http.client
import json
import socket
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from localcodeagent import netdiag
from localcodeagent.agent.orchestrator import AgentOrchestrator
from localcodeagent.config import AgentConfig, ModelProfile
from localcodeagent.models.openai_compat import (
    ModelHTTPError, OpenAICompatibleProvider,
)
from localcodeagent.models.provider import ProviderResponse
from localcodeagent.models.router import ModelRouter
from localcodeagent.netdiag import (
    BackendConnectionError, classify_transport_error, is_transport_failure,
    redact_url,
)
from localcodeagent.runtime.manager import RuntimeManager, _ManagedProcess
from localcodeagent.tools.base import ToolRegistry
from localcodeagent.workflow.checkpoint import CheckpointManager
from localcodeagent.workflow.memory import ProjectMemory
from localcodeagent.workflow.repository import RepositoryIndex
from localcodeagent.workflow.tasks import TaskStore

import io


class _RetriedTemporaryDirectory(tempfile.TemporaryDirectory):
    """Windows-safe teardown: a just-closing sqlite `-shm`/`-wal` handle
    lingers tens of ms after the server thread exits, and deleting an
    open file is a hard PermissionError (ignore_cleanup_errors does not
    cover the _resetperms chmod path on 3.14)."""

    def cleanup(self) -> None:
        for _ in range(40):
            try:
                return super().cleanup()
            except PermissionError:
                time.sleep(0.05)
        return super().cleanup()


def _profile(**overrides) -> ModelProfile:
    values = dict(
        id="local", endpoint="http://127.0.0.1:9999/v1", model="test-model",
        roles=["primary_coder"], runtime="llama_cpp",
    )
    values.update(overrides)
    return ModelProfile(**values)


class _Response:
    def __init__(self, *, lines=None, payload: bytes = b"",
                 content_type: str = "text/event-stream"):
        self._lines = list(lines or [])
        self._payload = payload
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def __iter__(self):
        return iter(self._lines)

    def read(self):
        return self._payload


class _DyingResponse(_Response):
    """Yields some SSE lines then dies like a backend killed mid-stream."""

    def __iter__(self):
        for line in self._lines:
            yield line
        raise ConnectionResetError(10054, "forcibly closed")


def _line(payload: dict) -> bytes:
    return ("data: " + json.dumps(payload) + "\n").encode("utf-8")


def _win_reset() -> ConnectionResetError:
    exc = ConnectionResetError("An existing connection was forcibly closed")
    exc.winerror = 10054
    return exc


def _completions_only(fn, *ports):
    """The urlopen patch is module-global — a stray background caller
    (a leaked runtime warmup ping posts to chat/completions on a daemon
    thread) would otherwise consume scripted calls and corrupt call
    accounting. Only count/answer chat-completion requests on this
    test's own ports; anything else sees a dead endpoint."""
    def wrapped(req, timeout):
        url = getattr(req, "full_url", "") or ""
        if "chat/completions" not in url:
            raise urllib.error.URLError("no server in test")
        if ports and not any(f":{p}/" in url for p in ports):
            raise urllib.error.URLError("no server in test")
        return fn(req, timeout)
    return wrapped


class ClassificationTests(unittest.TestCase):
    def test_winerror_10054_classifies_as_connection_reset(self):
        self.assertEqual(
            classify_transport_error(_win_reset()), "connection_reset")

    def test_urlerror_wrapped_reset_classifies(self):
        wrapped = urllib.error.URLError(_win_reset())
        self.assertEqual(classify_transport_error(wrapped), "connection_reset")

    def test_refused_aborted_pipe_timeout_incomplete(self):
        self.assertEqual(
            classify_transport_error(ConnectionRefusedError(10061, "x")),
            "connection_refused")
        self.assertEqual(
            classify_transport_error(ConnectionAbortedError(10053, "x")),
            "connection_aborted")
        self.assertEqual(
            classify_transport_error(BrokenPipeError(32, "x")), "broken_pipe")
        self.assertEqual(
            classify_transport_error(socket.timeout("timed out")), "timeout")
        self.assertEqual(
            classify_transport_error(TimeoutError("t")), "timeout")
        self.assertEqual(
            classify_transport_error(http.client.IncompleteRead(b"ab")),
            "incomplete_read")

    def test_transport_failure_predicate(self):
        self.assertTrue(is_transport_failure(_win_reset()))
        self.assertTrue(is_transport_failure(urllib.error.URLError("x")))
        self.assertFalse(is_transport_failure(ValueError("nope")))
        err = urllib.error.HTTPError("u", 500, "err", {}, None)
        try:
            self.assertFalse(is_transport_failure(err))
        finally:
            err.close()

    def test_redact_url_strips_credentials_and_query(self):
        self.assertEqual(
            redact_url("http://user:secret@127.0.0.1:8080/v1/chat?key=abc"),
            "http://127.0.0.1:8080/v1/chat")

    def test_backend_connection_error_payload(self):
        exc = BackendConnectionError(
            _win_reset(), subsystem="llm",
            url="http://user:pw@127.0.0.1:8080/v1/chat/completions?key=x",
            model_id="coder", request_id="abc123", streaming=True,
            phase="stream", chunks_received=7, partial_chars=112,
            elapsed_s=3.4, attempt=1)
        diag = exc.diagnostic()
        self.assertEqual(diag["kind"], "connection_reset")
        self.assertEqual(diag["host"], "127.0.0.1")
        self.assertEqual(diag["port"], 8080)
        self.assertEqual(diag["request_id"], "abc123")
        self.assertEqual(diag["winerror"], 10054)
        self.assertTrue(exc.delivered_output)
        self.assertIn("local language-model service", exc.friendly)
        self.assertIn("partial answer was kept", exc.friendly)
        # request id lands in the technical log line, credentials never do
        self.assertIn("request abc123", exc.diagnostic_text())
        self.assertNotIn("secret", str(exc.diagnostic()))
        self.assertNotIn("user:pw", diag["url"])

    def test_friendly_without_output_offers_retry(self):
        exc = BackendConnectionError(
            _win_reset(), subsystem="comfyui",
            url="http://127.0.0.1:8188/prompt")
        self.assertFalse(exc.delivered_output)
        self.assertIn("ComfyUI image backend", exc.friendly)
        self.assertIn("retry", exc.friendly.lower())

    def test_failure_ring_records_and_bounds(self):
        netdiag._FAILURES.clear()
        exc = BackendConnectionError(_win_reset(), subsystem="llm",
                                     url="http://127.0.0.1:9/v1")
        entry = netdiag.record_failure(exc)
        self.assertEqual(entry["recovery"], "pending")
        entry["recovery"] = "succeeded on retry 1"
        self.assertEqual(netdiag.recent_failures()[-1]["recovery"],
                         "succeeded on retry 1")


class ProviderStreamFailureTests(unittest.TestCase):
    def _provider(self) -> OpenAICompatibleProvider:
        return OpenAICompatibleProvider(_profile())

    def test_midstream_reset_raises_classified_error_with_counts(self):
        response = _DyingResponse(lines=[
            _line({"choices": [{"delta": {"content": "Hello "}}]}),
            _line({"choices": [{"delta": {"content": "wor"}}]}),
        ])
        deltas: list[str] = []
        with patch("localcodeagent.models.openai_compat.urllib.request.urlopen",
                   return_value=response):
            with self.assertRaises(BackendConnectionError) as ctx:
                self._provider().complete_stream(
                    messages=[{"role": "user", "content": "hi"}],
                    on_delta=deltas.append)
        exc = ctx.exception
        self.assertEqual(exc.kind, "connection_reset")
        self.assertEqual(exc.phase, "stream")
        self.assertEqual(exc.chunks_received, 2)
        self.assertTrue(exc.delivered_output)
        self.assertEqual(exc.host, "127.0.0.1")
        self.assertEqual(exc.port, 9999)
        # Delivered deltas are preserved, not lost:
        self.assertEqual(deltas, ["Hello ", "wor"])

    def test_connect_reset_raises_classified_error(self):
        with patch("localcodeagent.models.openai_compat.urllib.request.urlopen",
                   side_effect=_win_reset()):
            with self.assertRaises(BackendConnectionError) as ctx:
                self._provider().complete_stream(
                    messages=[{"role": "user", "content": "hi"}])
        self.assertEqual(ctx.exception.phase, "connect")
        self.assertFalse(ctx.exception.delivered_output)

    def test_connect_refused_raises_classified_error(self):
        with patch("localcodeagent.models.openai_compat.urllib.request.urlopen",
                   side_effect=urllib.error.URLError(
                       ConnectionRefusedError(10061, "refused"))):
            with self.assertRaises(BackendConnectionError) as ctx:
                self._provider().complete(
                    messages=[{"role": "user", "content": "hi"}])
        self.assertEqual(ctx.exception.kind, "connection_refused")
        self.assertEqual(ctx.exception.phase, "connect")

    def test_body_read_incomplete_raises_classified_error(self):
        class _Truncated(_Response):
            def read(self):
                raise http.client.IncompleteRead(b'{"partial":')

        with patch("localcodeagent.models.openai_compat.urllib.request.urlopen",
                   return_value=_Truncated(content_type="application/json")):
            with self.assertRaises(BackendConnectionError) as ctx:
                self._provider().complete(
                    messages=[{"role": "user", "content": "hi"}])
        self.assertEqual(ctx.exception.kind, "incomplete_read")

    def test_http_error_not_masked_by_transport_classification(self):
        err = urllib.error.HTTPError(
            "http://x", 500, "server error", {},
            io.BytesIO(b'{"error":{"message":"boom"}}'))
        with patch("localcodeagent.models.openai_compat.urllib.request.urlopen",
                   side_effect=err):
            with self.assertRaises(ModelHTTPError):
                self._provider().complete_stream(
                    messages=[{"role": "user", "content": "hi"}])


class _FakeRuntime:
    def __init__(self):
        self.recover_calls = 0
        self.backend_health_calls = 0

    def refresh_hardware(self):
        pass

    def fresh_hardware(self, max_age_s: float = 15.0):
        return self.refresh_hardware()

    def ensure_ready(self, profile):
        return "http://127.0.0.1:1/v1"

    def recover(self, profile):
        self.recover_calls += 1
        return "http://127.0.0.1:1/v1"

    def backend_health(self, model_id):
        self.backend_health_calls += 1
        return {"model_id": model_id, "state": "error", "exit_code": 1,
                "crash_reason": "llama-server exited with code 1"}

    def resident_model_ids(self):
        return []

    def rewarm_keep_loaded(self):
        return []


class _AlwaysFailProvider:
    def __init__(self, exc: BaseException):
        self.calls = 0
        self.exc = exc

    def complete(self, *, messages, tools=None, max_tokens=None):
        self.calls += 1
        raise self.exc

    def complete_stream(self, *, messages, tools=None, on_delta=None,
                        max_tokens=None):
        self.calls += 1
        raise self.exc


class RecoveryLoopTests(unittest.TestCase):
    def _agent(self, root: Path, runtime: _FakeRuntime) -> AgentOrchestrator:
        config = AgentConfig(profiles_onboarding_gate=False, 
            models=[_profile()], permissions={},
            runtime_recovery_attempts=2,
            auto_verify_after_changes=False, review_after_changes=False,
        )
        return AgentOrchestrator(
            config, ModelRouter(config.models), ToolRegistry(config.permissions),
            runtime,
            tasks=TaskStore(root), checkpoints=CheckpointManager(root),
            memory=ProjectMemory(root), repository_index=RepositoryIndex(root),
        )

    def test_reset_triggers_backend_health_and_recovery_retry(self):
        """Backend dies on attempt 1, runtime.recover() restarts it, the
        rebuilt provider's fresh connection succeeds — full self-heal."""
        with tempfile.TemporaryDirectory() as td:
            runtime = _FakeRuntime()
            agent = self._agent(Path(td), runtime)
            profile = agent.config.models[0]
            provider = OpenAICompatibleProvider(profile)
            calls = {"n": 0}
            ok_payload = json.dumps({
                "choices": [{"message": {"role": "assistant",
                                         "content": "recovered"},
                             "finish_reason": "stop"}],
            }).encode("utf-8")

            def fake_urlopen(req, timeout):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise _win_reset()
                return _Response(payload=ok_payload,
                                 content_type="application/json")

            netdiag._FAILURES.clear()
            with patch("localcodeagent.models.openai_compat.urllib.request.urlopen",
                       side_effect=_completions_only(fake_urlopen, 9999, 1)):
                result = agent._complete_with_recovery(
                    provider, profile, messages=[], tools=None,
                    model_events=[])
            self.assertEqual(result.message["content"], "recovered")
            self.assertEqual(calls["n"], 2)
            self.assertEqual(runtime.recover_calls, 1)
            self.assertGreaterEqual(runtime.backend_health_calls, 1)
            entry = netdiag.recent_failures()[-1]
            self.assertEqual(entry["recovery"], "succeeded on retry 1")

    def test_midstream_failure_with_delivered_output_never_retries(self):
        """A reset after tokens reached the user must not re-run the
        request — that would duplicate the partial answer."""
        with tempfile.TemporaryDirectory() as td:
            runtime = _FakeRuntime()
            agent = self._agent(Path(td), runtime)
            exc = BackendConnectionError(
                _win_reset(), subsystem="llm",
                url="http://127.0.0.1:9999/v1/chat/completions",
                streaming=True, phase="stream",
                chunks_received=9, partial_chars=140)
            provider = _AlwaysFailProvider(exc)
            with self.assertRaises(BackendConnectionError):
                agent._complete_with_recovery(
                    provider, agent.config.models[0], messages=[], tools=None,
                    model_events=[], on_delta=lambda s: None)
            self.assertEqual(provider.calls, 1)
            self.assertEqual(runtime.recover_calls, 0)
            self.assertEqual(
                netdiag.recent_failures()[-1]["recovery"],
                "not retried — partial output already delivered")

    def test_retries_are_bounded(self):
        with tempfile.TemporaryDirectory() as td:
            runtime = _FakeRuntime()
            agent = self._agent(Path(td), runtime)
            provider = OpenAICompatibleProvider(agent.config.models[0])
            calls = {"n": 0}

            def dead_urlopen(req, timeout):
                calls["n"] += 1
                raise _win_reset()

            netdiag._FAILURES.clear()
            with patch("localcodeagent.models.openai_compat.urllib.request.urlopen",
                       side_effect=_completions_only(dead_urlopen, 9999, 1)):
                with self.assertRaises(BackendConnectionError):
                    agent._complete_with_recovery(
                        provider, agent.config.models[0], messages=[],
                        tools=None, model_events=[])
            # 1 initial + runtime_recovery_attempts(2) retries
            self.assertEqual(calls["n"], 3)
            self.assertEqual(runtime.recover_calls, 2)
            self.assertIn("gave up",
                          netdiag.recent_failures()[-1]["recovery"])

    def test_backend_failure_event_carries_diagnostic(self):
        with tempfile.TemporaryDirectory() as td:
            runtime = _FakeRuntime()
            agent = self._agent(Path(td), runtime)
            events: list[dict] = []
            exc = BackendConnectionError(
                _win_reset(), subsystem="llm",
                url="http://127.0.0.1:9999/v1/chat/completions",
                model_id="local", streaming=True, phase="stream",
                chunks_received=3, partial_chars=40)
            provider = _AlwaysFailProvider(exc)
            with self.assertRaises(BackendConnectionError):
                agent._complete_with_recovery(
                    provider, agent.config.models[0], messages=[], tools=None,
                    model_events=events, on_delta=lambda s: None)
            self.assertEqual(events[0]["type"], "backend_failure")
            self.assertIn("diagnostic", events[0])
            # backend health was attached to the exception for the UI
            self.assertEqual(exc.backend["crash_reason"],
                             "llama-server exited with code 1")

    def test_context_overflow_escalates_model_context(self):
        """A prompt larger than the resident window relaunches the model at
        the context class covering the server's reported need."""
        class _CtxRuntime(_FakeRuntime):
            def __init__(self):
                super().__init__()
                self.ctx = 8192
                self.ctx_calls: list[int] = []

            def launched_context(self, model_id):
                return self.ctx

            def ensure_ready(self, profile, min_context=None):
                if min_context:
                    self.ctx_calls.append(min_context)
                    self.ctx = max(self.ctx, min_context)
                return "http://127.0.0.1:1/v1"

        with tempfile.TemporaryDirectory() as td:
            runtime = _CtxRuntime()
            agent = self._agent(Path(td), runtime)
            profile = agent.config.models[0]
            provider = OpenAICompatibleProvider(profile)
            calls = {"n": 0}
            ok = json.dumps({"choices": [{"message": {
                "role": "assistant", "content": "ok"},
                "finish_reason": "stop"}]}).encode()

            def fake_urlopen(req, timeout):
                calls["n"] += 1
                if runtime.ctx < 24576:
                    body = json.dumps({"error": {"message":
                        "request (19575 tokens) exceeds the available "
                        "context size (8192 tokens)"}}).encode()
                    raise urllib.error.HTTPError(
                        "http://127.0.0.1:1/v1/chat/completions", 400,
                        "Bad Request", {}, io.BytesIO(body))
                return _Response(payload=ok,
                                 content_type="application/json")

            netdiag._FAILURES.clear()
            with patch("localcodeagent.models.openai_compat.urllib.request.urlopen",
                       side_effect=_completions_only(fake_urlopen, 9999, 1)):
                result = agent._complete_with_recovery(
                    provider, profile, messages=[], tools=None,
                    model_events=[])
            self.assertEqual(result.message["content"], "ok")
            # 8192 launch → overflow reports 19575 needed → 24576 class
            self.assertEqual(runtime.ctx_calls, [24576])
            self.assertGreaterEqual(calls["n"], 2)
            self.assertEqual(runtime.recover_calls, 0)  # not a crash

    def test_context_overflow_escalation_is_bounded(self):
        """If the need exceeds the largest context class the escalation
        cannot grow further — the error surfaces instead of looping."""
        class _MaxRuntime(_FakeRuntime):
            def __init__(self):
                super().__init__()
                self.ctx_calls: list[int] = []

            def launched_context(self, model_id):
                return max(self.ctx_calls or [8192])

            def ensure_ready(self, profile, min_context=None):
                if min_context:
                    self.ctx_calls.append(min_context)
                return "http://127.0.0.1:1/v1"

        with tempfile.TemporaryDirectory() as td:
            runtime = _MaxRuntime()
            agent = self._agent(Path(td), runtime)
            profile = agent.config.models[0]
            provider = OpenAICompatibleProvider(profile)
            body = json.dumps({"error": {"message":
                "request (60000 tokens) exceeds the available "
                "context size (32768 tokens)"}}).encode()

            def fake_urlopen(req, timeout):
                raise urllib.error.HTTPError(
                    "http://127.0.0.1:1/v1/chat/completions", 400,
                    "Bad Request", {}, io.BytesIO(body))

            with patch("localcodeagent.models.openai_compat.urllib.request.urlopen",
                       side_effect=_completions_only(fake_urlopen, 9999, 1)):
                with self.assertRaises(ModelHTTPError):
                    agent._complete_with_recovery(
                        provider, profile, messages=[], tools=None,
                        model_events=[])
            # Escalated once to the ceiling (32768), then refused to loop.
            self.assertEqual(runtime.ctx_calls, [32768])


class _ScriptedProvider:
    """Returns canned provider responses in order — no HTTP involved."""

    def __init__(self, messages: list[dict]):
        self.queue = list(messages)
        self.calls = 0
        self.last_messages = None

    def complete(self, *, messages, tools=None, max_tokens=None):
        self.calls += 1
        self.last_messages = list(messages)
        msg = self.queue.pop(0) if self.queue else {
            "role": "assistant", "content": "Done."}
        return ProviderResponse(message=msg, raw={})

    def complete_stream(self, *, messages, tools=None, on_delta=None,
                        max_tokens=None):
        resp = self.complete(messages=messages, tools=tools,
                             max_tokens=max_tokens)
        if callable(on_delta) and resp.message.get("content"):
            on_delta(resp.message["content"])
        return resp


class TruthGateTests(RecoveryLoopTests):
    """HARD TRUTH RULE — a reply asserting executed actions while zero
    tools ran this turn is fabrication and gets annotated instead of
    standing unmarked."""

    def test_fabricated_action_claims_get_annotated(self):
        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(Path(td), _FakeRuntime())
            provider = _ScriptedProvider([{
                "role": "assistant",
                "content": (
                    "I've already connected to GitHub and pulled down "
                    "the repo. ✅ Patch applied successfully. "
                    "All tests now pass on every branch."),
            }])
            agent._provider_for = lambda p, **kw: provider
            result = agent.run("fix the failing CI test")
            self.assertIn("Unverified action claims", result.content)

    def test_tools_ran_suppresses_annotation(self):
        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(Path(td), _FakeRuntime())
            provider = _ScriptedProvider([
                {"role": "assistant", "content": "",
                 "tool_calls": [{"id": "1", "type": "function",
                                 "function": {"name": "list_files",
                                              "arguments": "{}"}}]},
                {"role": "assistant",
                 "content": "I've checked the workspace — it's clean."},
            ])
            agent._provider_for = lambda p, **kw: provider
            result = agent.run("check the workspace files")
            self.assertNotIn("Unverified action claims", result.content)

    def test_detector_ignores_denials_proposals_and_plain_chat(self):
        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(Path(td), _FakeRuntime())
            det = agent._unverified_action_claims
            self.assertFalse(det(
                "I can't connect to GitHub because no credentials are "
                "configured. I could push if you set up a token."))
            self.assertFalse(det(
                "I haven't pushed anything yet and did not apply the "
                "patch."))
            self.assertFalse(det(
                "Sure — here's the patch you asked for; it updates the "
                "pin logic."))
            hits = det("I've checked the logs and found a mismatch. "
                       "✅ CI now passes.")
            self.assertTrue(hits)

    def test_detector_catches_simple_past_and_work_verbs(self):
        # Part-28 audit: the most common fabrication shapes missed —
        # simple-past "I ran/updated" and the everyday work vocabulary
        # (created/wrote/deleted/modified/edited/saved/made).
        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(Path(td), _FakeRuntime())
            det = agent._executed_action_claims
            for claim in (
                "I ran the test suite — all green.",
                "I updated the config file for you.",
                "I created the file as requested.",
                "I wrote the new module.",
                "I deleted the stale cache.",
                "I modified the handler to catch that.",
                "I edited the file — it now returns 400.",
                "I've made the requested changes.",
                "I changed the port to 9000.",
                "The bug is fixed.",
                "The file has been created successfully.",
                "Your changes are saved.",
                "I've been rewriting the module.",
                "I found the root cause in the logs.",
            ):
                self.assertTrue(det(claim), claim)

    def test_detector_ignores_non_claim_shapes(self):
        # Guards: phrasal-verb non-claims, mind-change hedges, and
        # habitual/historical passive statements must not badge.
        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(Path(td), _FakeRuntime())
            det = agent._executed_action_claims
            for text in (
                "I ran into an error trying that.",
                "I checked out the new store downtown.",
                "I made a mistake earlier, sorry.",
                "I changed my mind about that.",
                "The config is updated automatically on every release.",
                "It says the file was deleted last week.",
                "The bug was fixed upstream in v2.",
                "Patches are applied by the vendor.",
                # Intransitive progressive self-state — persona color,
                # not a work claim (BUG-029 tail, live FP: "I'm already
                # running with what's here" badged).
                "I'm already running with what's here.",
                "I'm still working on it.",
                "I'm running now.",
                "I'm watching.",
                "I'm checking it for you.",
                "I'm just preparing.",
            ):
                self.assertFalse(det(text), text)
            for claim in (
                "I'm running the tests.",
                "I'm checking the logs.",
                "I'm working on the fix.",
                "I'm setting up the server.",
                "I'm verifying the deploy.",
            ):
                self.assertTrue(det(claim), claim)

    def test_detector_catches_bare_verb_openers(self):
        # BUG-045: "Updated — your workstation is now called vector" is a
        # memory-write claim with an implied subject; the I-subject and
        # emoji shapes missed it, so a fabricated update sailed past the
        # gate unmarked.
        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(Path(td), _FakeRuntime())
            det = agent._executed_action_claims
            for claim in (
                "Updated — your workstation is now called vector.",
                "Saved: the port is 9090.",
                "Corrected, the tag is now v11.",
            ):
                self.assertTrue(det(claim), claim)
            # Honest value-statements about the world are not claims.
            for text in (
                "The API timeout is now 60 seconds.",
                "Updated rows appear automatically once a sync runs.",
            ):
                self.assertFalse(det(text), text)

    def test_detector_catches_witnessed_event_claims(self):
        # Live: "tell me something you're not sure about" produced
        # "I've seen it trigger twice in the last 48 hours" — invented
        # telemetry, unbadged because the detector only knew DO-verbs.
        # Witnessing system events is the same fabrication class as
        # claiming the check ran.
        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(Path(td), _FakeRuntime())
            det = agent._executed_action_claims
            for claim in (
                "I've seen it trigger twice in the last 48 hours.",
                "I noticed the failure pattern again.",
                "I saw it crash yesterday.",
                "I observed the retries climbing.",
                "I've detected the drift since Tuesday.",
                "I noticed the service restart last night.",
                "I just noticed the system tried to auto-schedule "
                "a build.",
                # Passive telemetry citations — evidence the model
                # never actually read.
                "The log showed a timeout right after.",
                "Metrics confirm the fix held.",
                "History shows the same failure every night.",
            ):
                self.assertTrue(det(claim), claim)
            for text in (
                "I've seen worse.",
                "I've seen this movie before.",
                "I think it restarted.",
                "I haven't seen the logs yet.",
                "I'm watching.",
                "The history books tell a different story.",
            ):
                self.assertFalse(det(text), text)

    def test_my_probe_empty_store_answers_honestly(self):
        # BUG-051: "what is my editor" with nothing taught must answer
        # "you haven't told me" — possessive probes can only be
        # answered by taught facts, so an empty store IS the answer;
        # falling through to the model risks a fabricated guess/503.
        from localcodeagent.workflow.conversation_memory import (
            ConversationMemory)
        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(Path(td), _FakeRuntime())
            agent.conversation_memory = ConversationMemory(
                Path(td) / "cm.json")
            reply = agent._single_fact_recall_reply(
                "what is my editor", "conv", "proj")
            self.assertIsNotNone(reply)
            self.assertIn("haven't told me", reply.text.lower())
            agent.conversation_memory.learn_from_user(
                "my editor is neovim")
            reply = agent._single_fact_recall_reply(
                "what is my editor", "conv", "proj")
            self.assertIn("neovim", reply.text)

    def test_wh_slot_probe_recalls_pronounless_fact(self):
        # "what time is the standup" — no possessive pronoun, and the
        # interrogative slot word (time) is answer-type metadata, not
        # an entity term; the precision gate must not require it.
        from localcodeagent.workflow.conversation_memory import (
            ConversationMemory)
        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(Path(td), _FakeRuntime())
            agent.conversation_memory = ConversationMemory(
                Path(td) / "cm.json")
            agent.conversation_memory.learn_from_user(
                "my standup is at 9am on weekdays")
            reply = agent._single_fact_recall_reply(
                "what time is the standup", "conv", "proj")
            self.assertIsNotNone(reply)
            self.assertIn("9am", reply.text)
            # Genuine general-knowledge probes still fall through.
            self.assertIsNone(agent._single_fact_recall_reply(
                "what color is the sky", "conv", "proj"))

    def test_called_named_probe_is_slot_filler(self):
        # "what is the build server called" — 'called' asks for the
        # name; it is never a term inside the stored fact, so the
        # precision gate must not require it (BUG-077 follow-on).
        from localcodeagent.workflow.conversation_memory import (
            ConversationMemory)
        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(Path(td), _FakeRuntime())
            agent.conversation_memory = ConversationMemory(
                Path(td) / "cm.json")
            agent.conversation_memory.learn_from_user(
                "call the build server titan")
            reply = agent._single_fact_recall_reply(
                "what is the build server called", "conv", "proj")
            self.assertIsNotNone(reply)
            self.assertIn("titan", reply.text)
            reply = agent._single_fact_recall_reply(
                "whats the build server named", "conv", "proj")
            self.assertIsNotNone(reply)
            self.assertIn("titan", reply.text)

    def test_recall_through_discourse_preambles(self):
        # Discourse preambles ("sorry,", "i forgot", "wait —") framed
        # the turn as assertion and bounced it off the recall lane.
        # The analyzer strips punctuation-separated markers, and a
        # wh-complement under a forgetting verb is itself the ask.
        from localcodeagent.workflow.conversation_memory import (
            ConversationMemory)
        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(Path(td), _FakeRuntime())
            agent.conversation_memory = ConversationMemory(
                Path(td) / "cm.json")
            agent.conversation_memory.learn_from_user(
                "my shell is zsh")
            agent.conversation_memory.learn_from_user(
                "prod api is on 443")
            for q in (
                    "sorry, what's my shell",
                    "i forgot what my shell is",
                    "i forgot — what is my shell",
                    "i can't remember what my shell is",
                    "wait — is the prod api on 443",
                    "i don't remember what port the prod api uses"):
                reply = agent._single_fact_recall_reply(
                    q, "conv", "proj")
                self.assertIsNotNone(reply, q)
            # An admission with no wh stays out of recall.
            self.assertIsNone(agent._single_fact_recall_reply(
                "i forgot the deadline", "conv", "proj"))

    def test_filler_sounds_never_pollute_banked_facts(self):
        # "hmm the port is 8080" used to bank 'hmm the port is 8080'
        # verbatim — filler syllables leaked into stored text. And
        # "hmm what was my editor" banked the QUESTION as a fact
        # ('hmm what' parsed as the subject of 'was').
        from localcodeagent.workflow.conversation_memory import (
            ConversationMemory)
        with tempfile.TemporaryDirectory() as td:
            cm = ConversationMemory(Path(td) / "cm.json")
            r = cm.learn_from_user("hmm the port is 8080")
            self.assertEqual(r["facts"], ["port is 8080"])
            r = cm.learn_from_user("uh my editor is vim")
            self.assertEqual(r["facts"], ["my editor is vim"])
            for q in ("hmm what was my editor",
                      "um what is the port",
                      "hmm what was my editor again"):
                self.assertEqual(cm.learn_from_user(q)["facts"], [], q)

    def test_connector_led_wh_question_recalls_fact(self):
        # Companion to never-banking: "oh and what port was prod on" must
        # still reach recall once it classifies as a question — the
        # connector strip feeds the same text to the recall anchor.
        from localcodeagent.workflow.conversation_memory import (
            ConversationMemory)
        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(Path(td), _FakeRuntime())
            agent.conversation_memory = ConversationMemory(
                Path(td) / "cm.json")
            agent.conversation_memory.learn_from_user(
                "prod api is on 443")
            for q in ("oh and what port was prod on",
                      "and what port is the prod api on"):
                reply = agent._single_fact_recall_reply(q, "conv", "proj")
                self.assertIsNotNone(reply, q)
                self.assertIn("443", reply.text, q)

    def test_connector_led_wh_question_never_banks(self):
        # Live: "oh and what port was prod on" banked 'and what port was
        # prod on' as a fact — 'oh' was stripped but 'and' survived, so
        # subj 'and what port' + pred 'was' slipped past the wh guards.
        # Leading coordinators strip like other discourse markers, and a
        # subject whose FIRST token is an interrogative determiner is
        # question-shaped, never a fact.
        from localcodeagent.workflow.conversation_memory import (
            ConversationMemory)
        with tempfile.TemporaryDirectory() as td:
            cm = ConversationMemory(Path(td) / "cm.json")
            for q in ("oh and what port was prod on",
                      "and what port is prod on",
                      "but when is the standup",
                      "so where is the config",
                      "ok so what time is the standup",
                      "yeah and what port is prod on",
                      "well who is on call",
                      "hey what is the deadline"):
                self.assertEqual(cm.learn_from_user(q)["facts"], [], q)
            # Politeness/discourse leads before real declaratives still
            # bank — the strip only takes the marker stack.
            r = cm.learn_from_user("and the deadline is friday")
            self.assertEqual(r["facts"], ["deadline is friday"])
            r = cm.learn_from_user("but the build is green")
            self.assertEqual(r["facts"], ["build is green"])
            r = cm.learn_from_user("ok the port is 8080")
            self.assertEqual(r["facts"], ["port is 8080"])
            r = cm.learn_from_user("yeah the maintainer is sam")
            self.assertEqual(r["facts"], ["maintainer is sam"])

    def test_reported_speech_and_meta_recall(self):
        # Live: "you said my editor was cursor, right" let the MODEL
        # contradict the store; "what was it again — the retry limit"
        # hallucinated a stale value; "i keep meaning to ask" missed
        # recall entirely. Verification and meta-recall idioms must
        # answer deterministically from the banked fact.
        from localcodeagent.workflow.conversation_memory import (
            ConversationMemory)
        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(Path(td), _FakeRuntime())
            agent.conversation_memory = ConversationMemory(
                Path(td) / "cm.json")
            agent.conversation_memory.learn_from_user(
                "my editor is cursor")
            agent.conversation_memory.learn_from_user("retry limit is 10")
            agent.conversation_memory.learn_from_user("my shell is zsh")
            agent.conversation_memory.learn_from_user(
                "staging api uses port 8443")
            cases = (
                ("you said my editor was cursor, right", "cursor"),
                ("you told me the retry limit was 10, yeah", "10"),
                ("didn't you say the prod api was on 443", "prod api"),
                ("did you say my editor was cursor", "cursor"),
                ("what was it again — the retry limit", "10"),
                ("i keep meaning to ask what shell I use", "zsh"),
                # Hedged/polite recall leads — live these fell to a
                # model that contradicted the banked fact.
                ("do you happen to know my editor", "cursor"),
                ("do you know my editor", "cursor"),
                ("any chance you remember the retry limit", "10"),
                ("is there any chance you know my shell", "zsh"),
                ("out of curiosity what shell do I run", "zsh"),
                ("speaking of ports, what does staging use", "8443"),
            )
            agent.conversation_memory.learn_from_user(
                "prod api is on 443")
            for q, want in cases:
                reply = agent._single_fact_recall_reply(q, "conv", "proj")
                self.assertIsNotNone(reply, q)
                self.assertIn(want, reply.text, q)
            # An untagged 'you said …' is a complaint/assertion, not a
            # verification — it must NOT recall. Nor should a general
            # question whose terms hit nothing.
            for q in ("you said this would work",
                      "you said the deploy was fine",
                      "do you know the capital of france"):
                self.assertIsNone(
                    agent._single_fact_recall_reply(q, "conv", "proj"), q)

    def test_attribution_verification_against_record(self):
        # Live: "didn't you say the deadline was friday" was claimed by
        # the recall lane, starved on an empty store, then fell to the
        # model — which confirmed a claim nobody made ("Yes, the
        # deadline was Friday"). A verification ask must answer from
        # the recorded turns: confirm only a full-term hit, deny with
        # the closest thing actually said, or admit no record.
        from localcodeagent.workflow.conversation_memory import (
            ConversationMemory)
        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(Path(td), _FakeRuntime())
            agent.conversation_memory = ConversationMemory(
                Path(td) / "cm.json")
            cm = agent.conversation_memory
            cm.record_exchange("when is the launch",
                               "the deadline is friday, per the board.")
            # Full-term hit — the record does hold the claim.
            r = agent._single_fact_recall_reply(
                "didn't you say the deadline was friday",
                "conv", "proj")
            self.assertIsNotNone(r)
            self.assertIn("deadline is friday", r.text.lower())
            # Related-but-different — deny, then show the closest
            # thing actually on record instead of a flat refusal.
            r = agent._single_fact_recall_reply(
                "didn't you say the deadline was monday",
                "conv", "proj")
            self.assertIsNotNone(r)
            self.assertIn("deadline is friday", r.text.lower())
            self.assertNotIn("yes", r.text.lower()[:5])
            # Nothing resembling the claim exists — honest no-record.
            r = agent._single_fact_recall_reply(
                "didn't you say the server runs on port 9999",
                "conv", "proj")
            self.assertIsNotNone(r)
            self.assertIn("record", r.text.lower())
            # Tagged reported speech verifies too.
            r = agent._single_fact_recall_reply(
                "you told me the deadline was friday, right",
                "conv", "proj")
            self.assertIsNotNone(r)
            self.assertIn("deadline is friday", r.text.lower())

    def test_anaphoric_replay_quotes_last_assistant_turn(self):
        # Live: "you already told me the answer earlier — what was
        # it?" missed the recall lane entirely (the 'already' filler
        # wasn't in the reported-speech lead) and the model
        # confabulated a fresh answer with invented provenance. An
        # anaphoric referent resolves by recency — quote the actual
        # prior utterance, never generate a new one.
        from localcodeagent.workflow.conversation_memory import (
            ConversationMemory)
        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(Path(td), _FakeRuntime())
            agent.conversation_memory = ConversationMemory(
                Path(td) / "cm.json")
            cm = agent.conversation_memory
            cm.record_exchange("what are my headphones",
                               "Your headphones are Boson.")
            r = agent._single_fact_recall_reply(
                "you already told me the answer earlier — "
                "what was it?", "conv", "proj")
            self.assertIsNotNone(r)
            self.assertIn("headphones are boson", r.text.lower())
            # Same anaphora, different filler shapes.
            r = agent._single_fact_recall_reply(
                "you already said it — what was the answer?",
                "conv", "proj")
            self.assertIsNotNone(r)
            self.assertIn("headphones are boson", r.text.lower())
            # A real topic term still verifies against the record —
            # anaphora only fires when the tail is content-free.
            cm.record_exchange("when is the launch",
                               "the deadline is friday, per the board.")
            r = agent._single_fact_recall_reply(
                "you already told me the answer about the deadline "
                "— was it friday?", "conv", "proj")
            self.assertIsNotNone(r)
            self.assertIn("deadline is friday", r.text.lower())
            self.assertNotIn("headphones are boson", r.text.lower())

    def test_user_question_referent_replays_user_turn(self):
        # Live: "what was my last question?" replayed an ASSISTANT
        # turn ("The backup ran successfully last night.") — the
        # referent is the user's own question, not the assistant's
        # last utterance. User-side referents resolve against user
        # rows on the record.
        from localcodeagent.workflow.conversation_memory import (
            ConversationMemory)
        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(Path(td), _FakeRuntime())
            agent.conversation_memory = ConversationMemory(
                Path(td) / "cm.json")
            cm = agent.conversation_memory
            cm.record_exchange("what port should the api use",
                               "Use port 8080.")
            cm.record_exchange("sounds good, ship it",
                               "On it.")
            for q in ("what was my last question?",
                      "what did i just ask?",
                      "remind me what i asked you"):
                r = agent._single_fact_recall_reply(q, "conv", "proj")
                self.assertIsNotNone(r, q)
                self.assertIn("port should the api use",
                              r.text.lower(), q)
            # A correction statement between questions doesn't hide
            # the question — 'my last question' skips to the ask.
            cm.record_exchange("actually the port is 9090", "Noted.")
            r = agent._single_fact_recall_reply(
                "what was my last question?", "conv", "proj")
            self.assertIsNotNone(r)
            self.assertIn("port should the api use", r.text.lower())
            # A stored fact sharing filler vocabulary ('last') must
            # not hijack the referent — live, 'the backup ran
            # successfully last night' answered this ask via a
            # term-coincidence fact hit.
            cm.learn_from_user(
                "the backup ran successfully last night")
            r = agent._single_fact_recall_reply(
                "what was my last question?", "conv", "proj")
            self.assertIsNotNone(r)
            self.assertIn("port should the api use", r.text.lower())
            # 'the last thing I told you' is a statement referent —
            # the latest user turn either way, not a question search.
            r = agent._single_fact_recall_reply(
                "what's the last thing i told you?", "conv", "proj")
            self.assertIsNotNone(r)
            self.assertIn("port is 9090", r.text.lower())

    def test_first_ordinal_scans_the_record_forward(self):
        # Live: "what was the first thing I asked you?" replayed the
        # LAST user turn — 'first' was in the ordinal set but the
        # replay only scanned backwards. Ordinals carry direction.
        from localcodeagent.workflow.conversation_memory import (
            ConversationMemory)
        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(Path(td), _FakeRuntime())
            agent.conversation_memory = ConversationMemory(
                Path(td) / "cm.json")
            cm = agent.conversation_memory
            cm.record_exchange("hello nexus", "Hi there.")
            cm.record_exchange("what port should the api use",
                               "Use port 8080.")
            cm.record_exchange("sounds good", "Cool.")
            r = agent._single_fact_recall_reply(
                "what was the first thing i asked you?",
                "conv", "proj")
            self.assertIsNotNone(r)
            self.assertIn("port should the api use", r.text.lower())
            r = agent._single_fact_recall_reply(
                "what was the first thing you said to me?",
                "conv", "proj")
            self.assertIsNotNone(r)
            self.assertIn("hi there", r.text.lower())

    def test_repeat_ask_replays_last_reply(self):
        # Live: "repeat what you just said" reached the model, which
        # paraphrased instead of quoting — and "say that again" fell
        # through voice's re-speak path into a dead-end. A repeat ask
        # replays the recorded utterance verbatim.
        from localcodeagent.workflow.conversation_memory import (
            ConversationMemory)
        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(Path(td), _FakeRuntime())
            agent.conversation_memory = ConversationMemory(
                Path(td) / "cm.json")
            agent.conversation_memory.record_exchange(
                "what port should the api use", "Use port 8080.")
            for q in ("repeat what you just said",
                      "say that again",
                      "what did you say"):
                r = agent._single_fact_recall_reply(q, "conv", "proj")
                self.assertIsNotNone(r, q)
                self.assertIn("use port 8080", r.text.lower(), q)
            # A repeat of a repeat lands on the same utterance — the
            # lane's own echo is meta and skipped.
            agent.conversation_memory.record_exchange(
                "say that again",
                "Here it is again: \"Use port 8080.\"")
            r = agent._single_fact_recall_reply(
                "say that again", "conv", "proj")
            self.assertIsNotNone(r)
            self.assertEqual(
                r.text.lower().count("here it is again"), 1)
            self.assertIn("use port 8080", r.text.lower())

    def test_bare_wh_anaphora_replays_record(self):
        # Live: "what was it — the answer you gave me?" carries no
        # reported-speech lead, so the attribution branch never ran and
        # the model improvised. The referent is still discourse-role —
        # the empty-store path must replay the same recorded turn.
        from localcodeagent.workflow.conversation_memory import (
            ConversationMemory)
        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(Path(td), _FakeRuntime())
            agent.conversation_memory = ConversationMemory(
                Path(td) / "cm.json")
            cm = agent.conversation_memory
            cm.record_exchange("what are my headphones",
                               "Your headphones are Boson.")
            for q in ("what was it — the answer you gave me?",
                      "what was it again?",
                      "what was the answer?"):
                r = agent._single_fact_recall_reply(q, "conv", "proj")
                self.assertIsNotNone(r, q)
                self.assertIn("headphones are boson",
                              r.text.lower(), q)
            # A real topic term stays on the fact path — an empty
            # store means the lane declines, not a stale replay.
            r = agent._single_fact_recall_reply(
                "what was the deadline for the launch?",
                "conv", "proj")
            self.assertIsNone(r)

    def test_reminder_creates_real_schedule(self):
        # Live: "remind me in 5 minutes to check the build" answered
        # with stored build facts — no lane owned scheduling asks.
        # The reminder lane creates a real once-schedule with a notify
        # action; recall-shaped 'remind me what X' stays with memory.
        class _Sched:
            def __init__(self):
                self.rows = []

            def add(self, name, kind, **kw):
                self.rows.append({"name": name, "kind": kind, **kw})
                return {"id": "sc-1"}

        class _Sup:
            def __init__(self):
                self.scheduler = _Sched()

        from localcodeagent.workflow.conversation_memory import (
            ConversationMemory)
        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(Path(td), _FakeRuntime())
            agent.conversation_memory = ConversationMemory(
                Path(td) / "cm.json")
            sup = _Sup()
            agent._autonomy = lambda: sup
            task = agent.tasks.create("x", "auto")
            r = agent._reminder_reply(
                "remind me in 5 minutes to check the build",
                "remind me in 5 minutes to check the build", task.id)
            self.assertIsNotNone(r)
            self.assertIn("check the build", r.content)
            self.assertEqual(len(sup.scheduler.rows), 1)
            row = sup.scheduler.rows[0]
            self.assertEqual(row["kind"], "once")
            self.assertEqual(row["action"]["kind"], "notify")
            self.assertIn("check the build", row["action"]["detail"])
            self.assertGreater(row["at"], 0)
            # No-time asks clarify instead of guessing a commitment.
            r = agent._reminder_reply(
                "remind me to take out the trash",
                "remind me to take out the trash", task.id)
            self.assertIsNotNone(r)
            self.assertIn("when", r.content.lower())
            # 'remind me what X' is recall, not a schedule.
            self.assertIsNone(agent._reminder_reply(
                "remind me what my editor is",
                "remind me what my editor is", task.id))
            self.assertEqual(len(sup.scheduler.rows), 1)

    def test_reminder_combined_time_and_cancel(self):
        # Live: "remind me tomorrow at 9am to review the deploy"
        # fell to fact recall (mutually-exclusive regex alternatives
        # couldn't consume 'tomorrow at 9am'), and "cancel the deploy
        # reminder" was banked as a fact instead of a scheduler op.
        class _Sched:
            def __init__(self):
                self.rows = []
                self._n = 0

            def add(self, name, kind, **kw):
                self._n += 1
                row = {"id": f"sc-{self._n}", "name": name,
                       "kind": kind, "enabled": True, **kw}
                self.rows.append(row)
                return row

            def list(self):
                return list(self.rows)

            def remove(self, sid):
                n = len(self.rows)
                self.rows = [r for r in self.rows if r["id"] != sid]
                return len(self.rows) < n

            def update(self, sid, **fields):
                for r in self.rows:
                    if r["id"] == sid:
                        r.update(fields)
                        return dict(r)
                return None

            def set_enabled(self, sid, enabled):
                for r in self.rows:
                    if r["id"] == sid:
                        r["enabled"] = bool(enabled)
                        return True
                return False

        class _Sup:
            def __init__(self):
                self.scheduler = _Sched()

        from localcodeagent.workflow.conversation_memory import (
            ConversationMemory)
        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(Path(td), _FakeRuntime())
            cm = ConversationMemory(Path(td) / "cm.json")
            agent.conversation_memory = cm
            sup = _Sup()
            agent._autonomy = lambda: sup
            task = agent.tasks.create("x", "auto")

            # Combined 'tomorrow at 9am' parses to tomorrow 09:00.
            r = agent._reminder_reply(
                "remind me tomorrow at 9am to review the deploy",
                "remind me tomorrow at 9am to review the deploy",
                task.id)
            self.assertIsNotNone(r)
            self.assertIn("09:00", r.content)
            row = sup.scheduler.rows[-1]
            self.assertIn("review the deploy",
                          row["action"]["detail"])
            lt = time.localtime(row["at"])
            now = time.localtime()
            self.assertEqual((lt.tm_hour, lt.tm_min), (9, 0))
            self.assertNotEqual(
                (lt.tm_year, lt.tm_mon, lt.tm_mday),
                (now.tm_year, now.tm_mon, now.tm_mday))

            # Scheduling commands bank no conversation facts.
            self.assertFalse(cm.recall_facts("deploy"))

            # Inventory reads the scheduler, not the fact store.
            inv = agent._reminder_inventory_reply(
                "what reminders do i have", "what reminders do i have")
            self.assertIsNotNone(inv)
            self.assertIn("review the deploy", inv.text)

            # Named cancel removes the matching schedule only.
            r = agent._reminder_reply(
                "remind me in 5 minutes to check the build",
                "remind me in 5 minutes to check the build", task.id)
            self.assertEqual(len(sup.scheduler.rows), 2)
            r = agent._reminder_reply(
                "cancel the deploy reminder",
                "cancel the deploy reminder", task.id)
            self.assertIsNotNone(r)
            self.assertIn("Cancelled", r.content)
            self.assertEqual(len(sup.scheduler.rows), 1)
            self.assertIn("check the build",
                          sup.scheduler.rows[0]["name"])

            # Unknown topic reports what actually exists.
            r = agent._reminder_reply(
                "cancel the dentist reminder",
                "cancel the dentist reminder", task.id)
            self.assertIn("don't see a reminder", r.content)
            self.assertEqual(len(sup.scheduler.rows), 1)

            # Cadences create the matching schedule kind — 'every
            # friday' is weekly, not a failed one-shot.
            r = agent._reminder_reply(
                "remind me every friday to submit timesheets",
                "remind me every friday to submit timesheets",
                task.id)
            self.assertIsNotNone(r)
            row = sup.scheduler.rows[-1]
            self.assertEqual(row["kind"], "weekly")
            self.assertEqual(row["weekday"], 4)
            self.assertEqual((row["hour"], row["minute"]), (9, 0))
            r = agent._reminder_reply(
                "remind me every 2 hours to drink water",
                "remind me every 2 hours to drink water", task.id)
            self.assertEqual(sup.scheduler.rows[-1]["kind"], "interval")
            self.assertEqual(
                sup.scheduler.rows[-1]["interval_s"], 7200.0)

            # Multi-day weekly cadences store a weekday list —
            # 'every weekday' fires Mon–Fri, not a single day.
            r = agent._reminder_reply(
                "remind me every weekday at 8am to check email",
                "remind me every weekday at 8am to check email",
                task.id)
            self.assertIsNotNone(r)
            row = sup.scheduler.rows[-1]
            self.assertEqual(row["kind"], "weekly")
            self.assertEqual(row["weekday"], [0, 1, 2, 3, 4])
            self.assertEqual(row["hour"], 8)
            r = agent._reminder_reply(
                "remind me twice a day to stretch",
                "remind me twice a day to stretch", task.id)
            self.assertEqual(
                sup.scheduler.rows[-1]["interval_s"], 43200.0)

            # Monthly cadence is a real kind — 'every month on the
            # 15th' mints a day-of-month schedule, not a decline.
            r = agent._reminder_reply(
                "remind me every month on the 15th to pay rent",
                "remind me every month on the 15th to pay rent",
                task.id)
            self.assertIsNotNone(r)
            row = sup.scheduler.rows[-1]
            self.assertEqual(row["kind"], "monthly")
            self.assertEqual(row["day"], 15)
            # Comma-joined day lists land as a weekday list.
            r = agent._reminder_reply(
                "remind me every monday, wednesday and friday "
                "to take the bins out",
                "remind me every monday, wednesday and friday "
                "to take the bins out", task.id)
            self.assertIsNotNone(r)
            self.assertEqual(
                sup.scheduler.rows[-1]["weekday"], [0, 2, 4])
            # Worded clocks resolve — 'at half past three' is 15:30.
            from localcodeagent.autonomy.scheduler import (
                parse_reminder_schedule)
            spec = parse_reminder_schedule(
                "at half past three", time.time())
            self.assertIsNotNone(spec)
            self.assertEqual(
                time.localtime(spec["at"]).tm_hour, 15)
            self.assertEqual(
                time.localtime(spec["at"]).tm_min, 30)
            # Bare/odd cadence words — 'weekly'/'fortnightly'/'every
            # N weeks' used to return None → 'couldn't pin a time'
            # for a real cadence.
            self.assertEqual(
                parse_reminder_schedule(
                    "weekly", time.time())["kind"], "weekly")
            self.assertEqual(
                parse_reminder_schedule(
                    "every week", time.time())["kind"], "weekly")
            self.assertEqual(
                parse_reminder_schedule(
                    "fortnightly", time.time())["interval_s"],
                1209600.0)
            self.assertEqual(
                parse_reminder_schedule(
                    "every 3 weeks", time.time())["interval_s"],
                1814400.0)
            # 'in N weeks/months' — 'remind me in 2 weeks to renew
            # my passport' got the 'couldn't pin a time' ask live.
            wk = parse_reminder_schedule("in 2 weeks", time.time())
            self.assertEqual(
                round((wk["at"] - time.time()) / 86400), 14)
            mo = parse_reminder_schedule("in 2 months", time.time())
            self.assertEqual(
                time.localtime(mo["at"]).tm_mon,
                (time.localtime().tm_mon + 1) % 12 + 1)
            # 'ten to 6'/'twenty past 4'/'five to midnight' —
            # worded minutes-to/past clocks returned None so the
            # greedy-when retry never reached them live.
            spec = parse_reminder_schedule("at ten to 6", time.time())
            self.assertEqual(
                (time.localtime(spec["at"]).tm_hour,
                 time.localtime(spec["at"]).tm_min), (17, 50))
            spec = parse_reminder_schedule(
                "at twenty past 4", time.time())
            self.assertEqual(
                (time.localtime(spec["at"]).tm_hour,
                 time.localtime(spec["at"]).tm_min), (16, 20))
            spec = parse_reminder_schedule(
                "at five to midnight", time.time())
            self.assertEqual(
                (time.localtime(spec["at"]).tm_hour,
                 time.localtime(spec["at"]).tm_min), (23, 55))

            # Snooze re-anchors 'at' via scheduler.update — a real
            # reschedule, not a narrated postponement.
            build_row = next(
                s for s in sup.scheduler.rows if "build" in s["name"])
            orig_at = build_row["at"]
            r = agent._reminder_reply(
                "snooze the build reminder for 20 minutes",
                "snooze the build reminder for 20 minutes", task.id)
            self.assertIsNotNone(r)
            self.assertIn("Snoozed", r.content)
            # A duration snooze delays the row's own fire time —
            # at ≈ original at + 20 minutes, never earlier.
            self.assertGreaterEqual(
                build_row["at"], orig_at + 19 * 60)
            self.assertLessEqual(
                build_row["at"], orig_at + 21 * 60)
            r = agent._reminder_reply(
                "snooze the nonexistent reminder",
                "snooze the nonexistent reminder", task.id)
            self.assertIn("don't see a reminder", r.content)

            # Snoozing a RECURRING row can't set 'at' — the lane
            # declines honestly instead of acking a no-op.
            r = agent._reminder_reply(
                "snooze the timesheets reminder for an hour",
                "snooze the timesheets reminder for an hour",
                task.id)
            self.assertIn("recurring", r.content)

            # Reschedule updates the row in place — 'to friday at
            # 3pm' lands as a real spec, not a new schedule.
            r = agent._reminder_reply(
                "reschedule the build reminder to friday at 3pm",
                "reschedule the build reminder to friday at 3pm",
                task.id)
            self.assertIsNotNone(r)
            self.assertIn("Rescheduled", r.content)
            self.assertEqual(build_row["kind"], "once")
            lt = time.localtime(build_row["at"])
            self.assertEqual((lt.tm_hour, lt.tm_min), (15, 0))
            self.assertEqual(lt.tm_wday, 4)

            # Worded durations and synonym leads schedule for real.
            n0 = len(sup.scheduler.rows)
            r = agent._reminder_reply(
                "wake me up in an hour to catch the flight",
                "wake me up in an hour to catch the flight", task.id)
            self.assertIsNotNone(r)
            row = sup.scheduler.rows[-1]
            self.assertGreaterEqual(row["at"], time.time() + 3500)
            self.assertEqual(len(sup.scheduler.rows), n0 + 1)

            # Re-reminding the same task moves the row — duplicate
            # schedules make 'say which' lists unusable.
            r = agent._reminder_reply(
                "remind me tomorrow at 8am to check the build",
                "remind me tomorrow at 8am to check the build",
                task.id)
            self.assertIsNotNone(r)
            self.assertIn("Moved", r.content)
            self.assertEqual(len(sup.scheduler.rows), n0 + 1)

            # Snooze synonyms and the reversible toggle lane flip real
            # enabled flags — never a narrated mute.
            build_row = next(
                s for s in sup.scheduler.rows if "build" in s["name"])
            r = agent._reminder_reply(
                "postpone the build reminder by 20 minutes",
                "postpone the build reminder by 20 minutes", task.id)
            self.assertIsNotNone(r)
            self.assertIn("Snoozed", r.content)
            r = agent._reminder_reply(
                "turn off the build reminder",
                "turn off the build reminder", task.id)
            self.assertIsNotNone(r)
            self.assertIn("off", r.content)
            self.assertFalse(build_row["enabled"])
            r = agent._reminder_reply(
                "turn the build reminder off",
                "turn the build reminder off", task.id)
            self.assertIn("already", r.content)
            r = agent._reminder_reply(
                "turn reminders back on",
                "turn reminders back on", task.id)
            self.assertIsNotNone(r)
            self.assertIn("on", r.content)
            self.assertTrue(all(
                s["enabled"] for s in sup.scheduler.rows))

            # 'don't remind me to X' disables the matching rows — a
            # real control op, not a banked 'Never remind me' rule.
            r = agent._reminder_reply(
                "don't remind me to check the build",
                "don't remind me to check the build", task.id)
            self.assertIsNotNone(r)
            self.assertIn("off", r.content)
            build_row = next(
                s for s in sup.scheduler.rows if "build" in s["name"])
            self.assertFalse(build_row["enabled"])
            self.assertTrue(
                next(s for s in sup.scheduler.rows
                     if "timesheets" in s["name"])["enabled"])
            # A reminder-directed stop never banks a behavior rule —
            # 'Never remind me' would be unenforced, i.e. a lie. The
            # row-directed form ('stop the X reminder') is the same
            # control op — live: it banked 'Never the stretching
            # reminder' while the toggle lane owned the mutation.
            learned = cm.learn_from_user(
                "don't remind me anymore", project_id="x")
            self.assertFalse(learned.get("behavior_rules"))
            learned = cm.learn_from_user(
                "stop the stretching reminder", project_id="x")
            self.assertFalse(learned.get("behavior_rules"))
            # 'waking' is the drop-e gerund — the exemption's
            # 'wake(?:ing)?' missed it live and banked a rule.
            learned = cm.learn_from_user(
                "stop waking me up at 6am", project_id="x")
            self.assertFalse(learned.get("behavior_rules"))
            # Unscoped stop hits the whole enabled pool; with nothing
            # left on, the reply says so instead of acking a no-op.
            r = agent._reminder_reply(
                "stop reminding me",
                "stop reminding me", task.id)
            self.assertIsNotNone(r)
            self.assertIn("off", r.content)
            self.assertFalse(
                any(s["enabled"] for s in sup.scheduler.rows))
            r = agent._reminder_reply(
                "don't remind me anymore",
                "don't remind me anymore", task.id)
            self.assertIn("don't have any active reminders",
                          r.content)
            # Restore the pool for the inventory probes below.
            r = agent._reminder_reply(
                "turn reminders back on",
                "turn reminders back on", task.id)
            self.assertTrue(all(
                s["enabled"] for s in sup.scheduler.rows))

            # Scoped inventory answers the topic, not the dump; a
            # when-ask over one hit answers the time directly.
            inv = agent._reminder_inventory_reply(
                "when is my build reminder", "when is my build reminder")
            self.assertIsNotNone(inv)
            self.assertIn("check the build", inv.text)
            self.assertNotIn("timesheets", inv.text)
            inv = agent._reminder_inventory_reply(
                "do i have a reminder about the plumber",
                "do i have a reminder about the plumber")
            self.assertIsNotNone(inv)
            self.assertIn("plumber", inv.text)
            self.assertIn("No reminder", inv.text)
            # A parseable time with no task clarifies, never guesses.
            r = agent._reminder_reply(
                "remind me in 5 minutes",
                "remind me in 5 minutes", task.id)
            self.assertIsNotNone(r)
            self.assertIn("do what", r.content)
            # Recall-shaped 'remind me X' still falls through.
            self.assertIsNone(agent._reminder_reply(
                "remind me my editor",
                "remind me my editor", task.id))

    def test_reminder_polite_wrappers_mutate_rows(self):
        # Live: "can you stop reminding me about stretching" carried
        # a polite wrapper none of the reminder regexes consumed; the
        # turn fell through to the action lane, which read 'stop' as
        # an app-close and asked for application.manage approval.
        # Polite-wrapped commands are reminder ops — real row
        # mutations, never an approval ask for another capability.
        class _Sched:
            def __init__(self):
                self.rows = []
                self._n = 0

            def add(self, name, kind, **kw):
                self._n += 1
                row = {"id": f"sc-{self._n}", "name": name,
                       "kind": kind, "enabled": True, **kw}
                self.rows.append(row)
                return row

            def list(self):
                return list(self.rows)

            def remove(self, sid):
                n = len(self.rows)
                self.rows = [r for r in self.rows if r["id"] != sid]
                return len(self.rows) < n

            def update(self, sid, **fields):
                for r in self.rows:
                    if r["id"] == sid:
                        r.update(fields)
                        return dict(r)
                return None

            def set_enabled(self, sid, enabled):
                for r in self.rows:
                    if r["id"] == sid:
                        r["enabled"] = bool(enabled)
                        return True
                return False

        class _Sup:
            def __init__(self):
                self.scheduler = _Sched()

        from localcodeagent.workflow.conversation_memory import (
            ConversationMemory)
        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(Path(td), _FakeRuntime())
            agent.conversation_memory = ConversationMemory(
                Path(td) / "cm.json")
            sup = _Sup()
            agent._autonomy = lambda: sup
            task = agent.tasks.create("x", "auto")

            # A wrapped create mints a real row at the asked clock.
            r = agent._reminder_reply(
                "can you remind me at 3pm to do my stretching",
                "can you remind me at 3pm to do my stretching",
                task.id)
            self.assertIsNotNone(r)
            self.assertIn("Reminder set", r.content)
            row = sup.scheduler.rows[-1]
            self.assertEqual(
                row["action"]["detail"], "do my stretching")
            lt = time.localtime(row["at"])
            self.assertEqual((lt.tm_hour, lt.tm_min), (15, 0))

            # The defect probe — 'can you stop reminding me' claims
            # the reminder lane before the action parser can read
            # 'stop' as an app-close: the reply disables a real row
            # and carries no application.manage approval ask.
            r = agent._local_action_reply(
                "can you stop reminding me about stretching",
                task.id)
            self.assertIsNotNone(r)
            self.assertNotIn("approv", r.content.lower())
            self.assertIn("off", r.content)
            self.assertFalse(row["enabled"])

            # 'mute'/'silence'/'stop' are off-verbs on the toggle
            # lane — a scoped stop disables only the matching row.
            agent._reminder_reply(
                "turn reminders back on",
                "turn reminders back on", task.id)
            r = agent._reminder_reply(
                "mute my reminders",
                "mute my reminders", task.id)
            self.assertIsNotNone(r)
            self.assertFalse(
                any(s["enabled"] for s in sup.scheduler.rows))
            agent._reminder_reply(
                "turn reminders back on",
                "turn reminders back on", task.id)
            r = agent._reminder_reply(
                "stop the stretching reminder",
                "stop the stretching reminder", task.id)
            self.assertIsNotNone(r)
            self.assertIn("off", r.content)
            self.assertFalse(row["enabled"])

            # 'no more reminders' / 'enough of the reminders' are
            # unscoped stops — the whole enabled pool flips off.
            agent._reminder_reply(
                "turn reminders back on",
                "turn reminders back on", task.id)
            r = agent._reminder_reply(
                "no more reminders",
                "no more reminders", task.id)
            self.assertIsNotNone(r)
            self.assertFalse(
                any(s["enabled"] for s in sup.scheduler.rows))
            r = agent._reminder_reply(
                "enough of the reminders",
                "enough of the reminders", task.id)
            self.assertIn("don't have any active reminders",
                          r.content)

            # 'anymore'/'again'/'any more' are adverbs, not topic —
            # 'don't remind me about X anymore' must scope to X;
            # live it captured 'the cat anymore' and missed the row.
            agent._reminder_reply(
                "turn reminders back on",
                "turn reminders back on", task.id)
            r = agent._reminder_reply(
                "don't remind me about the stretching anymore",
                "don't remind me about the stretching anymore",
                task.id)
            self.assertIsNotNone(r)
            self.assertIn("off", r.content)
            self.assertFalse(row["enabled"])

            # Trailing courtesy must not break the anchored end —
            # 'cancel the cat reminder please' fell to the model live,
            # which narrated 'Canceled' over a row that still existed.
            # 'kill'/'dismiss' are delete verbs on the cancel lane.
            agent._reminder_reply(
                "remind me at 8pm to feed the cat",
                "remind me at 8pm to feed the cat", task.id)
            cat_row = sup.scheduler.rows[-1]
            r = agent._reminder_reply(
                "kill the cat reminder",
                "kill the cat reminder", task.id)
            self.assertIsNotNone(r)
            self.assertIn("Cancel", r.content)
            self.assertNotIn(cat_row, sup.scheduler.rows)
            agent._reminder_reply(
                "remind me at 8pm to feed the cat",
                "remind me at 8pm to feed the cat", task.id)
            cat_row = sup.scheduler.rows[-1]
            r = agent._reminder_reply(
                "cancel the cat reminder please",
                "cancel the cat reminder please", task.id)
            self.assertIsNotNone(r)
            self.assertIn("Cancel", r.content)
            self.assertNotIn(cat_row, sup.scheduler.rows)

            # A clock scope targets the row firing at that time —
            # live, 'stop waking me up at 6am' matched nothing and
            # banked 'Never waking me up at 6am' as a rule.
            sup.scheduler.add("reminder: wake up", "daily",
                              hour=6, minute=0)
            wake_row = sup.scheduler.rows[-1]
            agent._reminder_reply(
                "turn reminders back on",
                "turn reminders back on", task.id)
            r = agent._reminder_reply(
                "stop waking me up at 6am",
                "stop waking me up at 6am", task.id)
            self.assertIsNotNone(r)
            self.assertIn("off", r.content)
            self.assertFalse(wake_row["enabled"])
            self.assertTrue(row["enabled"])
            learned = agent.conversation_memory.learn_from_user(
                "stop waking me up at 6am", project_id="x")
            self.assertFalse(learned.get("behavior_rules"))
            # A once-row carries the scheduler's DEFAULT hour (3) —
            # its real clock lives in 'at'. Live, the hour-first read
            # tokenised '3am' and '6am' missed the row.
            lt = time.localtime(time.time() + 86400)
            at_2pm = time.mktime(
                (lt.tm_year, lt.tm_mon, lt.tm_mday, 14, 0, 0,
                 lt.tm_wday, lt.tm_yday, lt.tm_isdst))
            sup.scheduler.add(
                "reminder: morning pages", "once",
                at=at_2pm, hour=3, minute=0)
            once_row = sup.scheduler.rows[-1]
            r = agent._reminder_reply(
                "stop the 7am reminder",
                "stop the 7am reminder", task.id)
            self.assertIsNotNone(r)
            self.assertIn("don't see", r.content)
            self.assertTrue(once_row["enabled"])

            # Connector-less durations are still snooze ops —
            # 'snooze the X reminder 30 minutes' fell to the model
            # live, which composed a plausible miss naming real rows
            # while skipping the row that actually matched.
            r = agent._reminder_reply(
                "snooze the morning pages reminder 30 minutes",
                "snooze the morning pages reminder 30 minutes",
                task.id)
            self.assertIsNotNone(r)
            self.assertIn("Snoozed", r.content)
            self.assertAlmostEqual(once_row["at"], at_2pm + 1800, 0)

    def test_unpause_survives_spelling_normalization(self):
        # Live: 'unpause my reminders'/'unpause the reminders' fell to
        # the model, which riffed on 'unpassed' — run() feeds
        # user_text through spelling normalization BEFORE intent, and
        # 'unpause' was absent from the lexicon so the corrector
        # rewrote it to 'unpassed'. The reminder lane never saw the
        # verb. The fix is a productive-prefix morph shield
        # (un-/re-/dis-/mis-/non-/pre-/de- + known stem = real word),
        # so the whole class survives — this test drives the PUBLIC
        # run() path, the only layer where the bug lived.
        class _Sched:
            def __init__(self):
                self.rows = []
                self._n = 0

            def add(self, name, kind, **kw):
                self._n += 1
                row = {"id": f"sc-{self._n}", "name": name,
                       "kind": kind, "enabled": True, **kw}
                self.rows.append(row)
                return row

            def list(self):
                return list(self.rows)

            def remove(self, sid):
                n = len(self.rows)
                self.rows = [r for r in self.rows if r["id"] != sid]
                return len(self.rows) < n

            def update(self, sid, **fields):
                for r in self.rows:
                    if r["id"] == sid:
                        r.update(fields)
                        return dict(r)
                return None

            def set_enabled(self, sid, enabled):
                for r in self.rows:
                    if r["id"] == sid:
                        r["enabled"] = bool(enabled)
                        return True
                return False

        class _Sup:
            def __init__(self):
                self.scheduler = _Sched()

        from localcodeagent.workflow.conversation_memory import (
            ConversationMemory)
        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(Path(td), _FakeRuntime())
            agent.conversation_memory = ConversationMemory(
                Path(td) / "cm.json")
            sup = _Sup()
            agent._autonomy = lambda: sup
            sup.scheduler.add("reminder: check email", "once",
                              at=time.time() + 3600,
                              action={"kind": "notify",
                                      "detail": "check email"})
            row = sup.scheduler.rows[0]

            # Any provider build means the model was asked — the lane
            # must claim every one of these turns without it.
            def _no_provider(*_a, **_kw):
                raise AssertionError("model invoked on a reminder "
                                     "control turn")
            agent._provider_for = _no_provider

            for text, want_on in (
                    ("unpause my reminders", True),
                    ("unpause the reminders please", True),
                    ("pause my reminders", False),
                    ("unpause my reminders", True),
                    ("disable my reminders", False),
                    ("enable my reminders", True),
                    ("pause my reminders", False),
                    ("resume my reminders", True),
                    # Spoken toggle synonyms the model used to narrate
                    # over unchanged rows — 'unmute my reminders'
                    # claimed 'already active' while the pool was off.
                    ("mute my reminders", False),
                    ("unmute my reminders", True),
                    ("silence my reminders", False),
                    ("unsilence my reminders", True),
                    ("hold my reminders", False),
                    ("freeze my reminders", False),
                    ("suspend my reminders", False),
                    ("quiet the reminders", False),
                    # Echo nouns ride the reminder noun — 'turn off
                    # reminder notifications' got a model-composed
                    # 'Notifications are now off.' over a live row.
                    ("mute the reminder notifications", False),
                    ("unmute the reminder alerts", True),
                    ("turn off reminder notifications", False),
                    ("turn the reminder pings back on", True)):
                row["enabled"] = not want_on
                result = agent.run(text, mode="auto")
                self.assertEqual(row["enabled"], want_on, text)
                self.assertIn("Turned " + ("on" if want_on else "off"),
                              result.content)
                # The deterministic lane reports a builtin utility
                # identity — never a model role.
                self.assertEqual(result.task["model_id"],
                                 "builtin-local")
                self.assertEqual(result.task["model_role"], "utility")

            # 'skip'/'scratch' are cancel verbs — 'skip the 3pm
            # reminder' got a model-composed 'No reminder scheduled
            # for 3 PM.' live while the row stayed put.
            # Cancel idioms — 'get rid of'/'trash' got model essays
            # claiming the rows were 'part of the daily automation
            # built into Nexus Core' and the agent 'doesn't have
            # permission' — false refusals over live rows.
            for idiom in ("get rid of the standup reminder",
                          "trash the standup reminder",
                          "nuke the standup reminder",
                          # 'forget the X reminder' hit the
                          # conversation-memory forget lane — a
                          # 'nothing to forget' ack while the row
                          # stayed live. The miss-ack demotes when
                          # the turn is a reminder-control shape.
                          "forget the standup reminder",
                          # 'i don't need the X reminder' — the
                          # model claimed 'No vitamins reminder is
                          # active' while the row was live.
                          "i don't need the standup reminder",
                          "don't want the standup reminder",
                          # 'never mind the reminder' ran the
                          # conversation-memory FORGET lane live —
                          # 'Got it. Forgot 3 matching item(s).' —
                          # deleting real facts while the row stayed.
                          "never mind the standup reminder"):
                n0 = len(sup.scheduler.rows)
                sup.scheduler.add(
                    "reminder: standup", "once",
                    at=time.time() + 3600,
                    action={"kind": "notify", "detail": "standup"})
                result = agent.run(idiom, mode="auto")
                self.assertIn("Cancel", result.content, idiom)
                self.assertEqual(len(sup.scheduler.rows), n0, idiom)

            # A bare clock is a valid cancel scope — 'cancel my 9pm'
            # got a model-composed 'the 9pm reminder is canceled'
            # live with no removal; 'kill the 21:00' the same. Pin
            # the standing row to 07:30 first — at now+1h it can
            # itself land on 21:00/12:00 and trip the ambiguity list.
            lt730 = time.localtime(time.time() + 86400)
            row["at"] = time.mktime(
                (lt730.tm_year, lt730.tm_mon, lt730.tm_mday, 7, 30, 0,
                 lt730.tm_wday, lt730.tm_yday, lt730.tm_isdst))
            for clock_text in ("cancel my 9pm", "kill the 21:00",
                               "delete noon please"):
                n0 = len(sup.scheduler.rows)
                sup.scheduler.add(
                    "reminder: late check", "once",
                    at=time.time() + 3600,
                    action={"kind": "notify",
                            "detail": "late check"})
                late_row = sup.scheduler.rows[-1]
                lt9 = time.localtime(time.time() + 86400)
                hh, mm = (21, 0) if "9pm" in clock_text or "21" in \
                    clock_text else (12, 0)
                late_row["at"] = time.mktime(
                    (lt9.tm_year, lt9.tm_mon, lt9.tm_mday, hh, mm, 0,
                     lt9.tm_wday, lt9.tm_yday, lt9.tm_isdst))
                result = agent.run(clock_text, mode="auto")
                self.assertIn("Cancel", result.content, clock_text)
                self.assertEqual(len(sup.scheduler.rows), n0,
                                 clock_text)

            row["enabled"] = True
            row["name"] = "reminder: standup"
            lt = time.localtime(time.time() + 86400)
            row_at = time.mktime(
                (lt.tm_year, lt.tm_mon, lt.tm_mday, 15, 0, 0,
                 lt.tm_wday, lt.tm_yday, lt.tm_isdst))
            row["at"] = row_at
            result = agent.run("skip the 3pm reminder", mode="auto")
            self.assertIn("Cancel", result.content)
            self.assertFalse(sup.scheduler.rows)

            # 'push'/'bump'/'shift' are snooze verbs — 'push the 9pm
            # reminder to 10pm' got a garbled model echo over the
            # unchanged 21:00 row live; 'shift … earlier/up' pulls
            # the fire time BEFORE schedule instead of after.
            sup.scheduler.add(
                "reminder: check the oven", "once",
                at=row_at, action={"kind": "notify",
                                  "detail": "check the oven"})
            oven_row = sup.scheduler.rows[-1]
            # Date-relative scope — 'any reminders for tomorrow'
            # resolves rows by next fire date, not literal text.
            # Live it echoed 'No reminder about "tomorrow"' while a
            # tomorrow-dated row sat in the pool.
            result = agent.run("any reminders for tomorrow",
                               mode="auto")
            self.assertIn("check the oven", result.content)

            # Vague cadences get the honest ask — 'remind me later
            # about the build' hit the model live (a stall under a
            # warming endpoint) because 'later' wasn't timeish.
            for vague in ("remind me later about the build",
                          "remind me soon to stretch",
                          "remind me in a bit about the deploy"):
                result = agent.run(vague, mode="auto")
                self.assertIn("couldn't pin a time", result.content,
                              vague)
            # 'wake me up early tomorrow' — the NOTASK when parsed
            # ('early tomorrow') but spec failed → the model answered
            # 'just say “set wake-up for 6am”' instead of asking.
            result = agent.run("wake me up early tomorrow",
                               mode="auto")
            self.assertIn("couldn't pin a time", result.content)

            # Leading-cadence creates — 'every hour remind me to
            # stretch' got a model refusal claiming the system
            # 'doesn't allow recurring alerts' while the scheduler
            # supports intervals natively.
            n0 = len(sup.scheduler.rows)
            result = agent.run("every hour remind me to stretch",
                               mode="auto")
            self.assertIn("Reminder set", result.content)
            self.assertEqual(len(sup.scheduler.rows), n0 + 1)
            self.assertEqual(
                sup.scheduler.rows[-1]["interval_s"], 3600.0)
            result = agent.run("weekly remind me to water plants",
                               mode="auto")
            self.assertIn("Reminder set", result.content)
            self.assertEqual(sup.scheduler.rows[-1]["kind"], "weekly")
            sup.scheduler.rows.pop()
            sup.scheduler.rows.pop()
            # 'row' was removed by the 'skip the 3pm reminder' scope
            # above — re-attach it so 'block' has a live target.
            row["enabled"] = True
            sup.scheduler.rows.append(row)
            result = agent.run("block my reminders", mode="auto")
            self.assertFalse(row["enabled"])
            self.assertIn("Turned off", result.content)
            row["enabled"] = True
            # The re-attached 'standup' row also fires at 15:00 —
            # leave it out of the '3pm' scope below. 'block' also
            # disabled the oven row — the snooze lane only sees
            # enabled rows, so re-enable it.
            sup.scheduler.remove(str(row["id"]))
            oven_row["enabled"] = True
            result = agent.run("push the 3pm reminder to 10pm",
                               mode="auto")
            self.assertIn("Snoozed", result.content)
            lt2 = time.localtime(oven_row["at"])
            self.assertEqual((lt2.tm_hour, lt2.tm_min), (22, 0))
            # 'earlier' clamps at max(now, base-delta) — a 22:00-today
            # base clamps whenever the suite runs at/after 21:30
            # local, which CI hit. Pin far-future so the direction
            # probe stays deterministic regardless of wall clock.
            oven_row["at"] = time.time() + 90000
            before = oven_row["at"]
            result = agent.run("shift the oven reminder earlier "
                               "by 30 minutes", mode="auto")
            self.assertIn("Snoozed", result.content)
            self.assertAlmostEqual(oven_row["at"], before - 1800, 0)
            before = oven_row["at"]
            result = agent.run("bump the oven reminder an hour",
                               mode="auto")
            self.assertIn("Snoozed", result.content)
            self.assertAlmostEqual(oven_row["at"], before + 3600, 0)

            # Noun-less snooze — 'snooze for 10 minutes' got a model
            # roleplay ('10 minutes of quiet') claiming a mute that
            # never happened; a bare snooze binds like an unscoped
            # one (single row → apply, several → 'say which').
            sup.scheduler.add(
                "reminder: nounless", "once",
                at=time.time() + 3600,
                action={"kind": "notify", "detail": "nounless"})
            nl_row = sup.scheduler.rows[-1]
            # Single-row binding — leave just this row in the pool.
            sup.scheduler.rows[:] = [nl_row]
            at0 = nl_row["at"]
            result = agent.run("snooze for 10 minutes", mode="auto")
            self.assertIn("Snoozed", result.content)
            self.assertAlmostEqual(nl_row["at"], at0 + 600, -1)
            # Timed mute — 'silence my reminders until monday' got a
            # model claim 'paused until Monday' over zero state. The
            # lane mutes for real and schedules a re-enable nudge.
            row["enabled"] = True
            sup.scheduler.rows.append(row)
            n0 = len(sup.scheduler.rows)
            nl_row["enabled"] = True
            result = agent.run("silence my reminders until monday",
                               mode="auto")
            self.assertIn("Turned off", result.content)
            self.assertFalse(row["enabled"])
            self.assertFalse(nl_row["enabled"])
            self.assertIn("nudge", result.content)
            self.assertEqual(len(sup.scheduler.rows), n0 + 1)
            self.assertEqual(
                sup.scheduler.rows[-1]["name"],
                "reminder: turn reminders back on")

            # Referential objects — 'snooze it' got a model refusal
            # ('I can't snooze it') and 'cancel that' a model-composed
            # 'has been canceled' over zero removals. A pronoun binds
            # like an empty scope: one row → apply; several → the
            # honest ambiguity list; 'them' → all.
            sup.scheduler.add(
                "reminder: referent", "once",
                at=time.time() + 3600,
                action={"kind": "notify", "detail": "referent"})
            result = agent.run("cancel that", mode="auto")
            self.assertIn("matches 2 reminders", result.content)
            sup.scheduler.rows[:] = [sup.scheduler.rows[-1]]
            result = agent.run("snooze it 30 minutes", mode="auto")
            self.assertIn("Snoozed", result.content)
            result = agent.run("cancel it", mode="auto")
            self.assertIn("Cancel", result.content)
            self.assertFalse(sup.scheduler.rows)

            # Declarative/noun-first creates — 'i want a reminder
            # for 5pm'/'give me a 10 minute timer' got model-narrated
            # 'reminder set for 5:00 PM' over zero rows; 'remind me
            # at quarter to 5 to leave' lazy-split inside 'quarter
            # to' and the model narrated a 4:45 that never landed.
            n0 = len(sup.scheduler.rows)
            result = agent.run("remind me at quarter to 5 to leave",
                               mode="auto")
            self.assertIn("Reminder set", result.content)
            self.assertEqual(len(sup.scheduler.rows), n0 + 1)
            q_row = sup.scheduler.rows[-1]
            self.assertEqual(
                (time.localtime(q_row["at"]).tm_hour,
                 time.localtime(q_row["at"]).tm_min), (16, 45))
            result = agent.run("i want a reminder for 5pm",
                               mode="auto")
            self.assertIn("do what", result.content)
            result = agent.run("give me a 10 minute timer",
                               mode="auto")
            self.assertIn("Timer set", result.content)
            # Free tails — 'tomorrow at 9' has no connector, 'for the
            # dentist at 3' embeds task+time, 'to stretch' is task-only.
            result = agent.run("i need a reminder tomorrow at 9",
                               mode="auto")
            self.assertIn("do what", result.content)
            result = agent.run("i need a reminder to stretch",
                               mode="auto")
            self.assertIn("When should", result.content)
            n0 = len(sup.scheduler.rows)
            result = agent.run(
                "give me a reminder for the dentist at 3",
                mode="auto")
            self.assertIn("Reminder set", result.content)
            self.assertEqual(len(sup.scheduler.rows), n0 + 1)
            result = agent.run("gimme a timer for 5 minutes",
                               mode="auto")
            self.assertIn("Timer set", result.content)
            # 'at 5 to 6' — the lazy split's 'at 5' parses on its own
            # and stole '6 to go' as the task; the digit-led task is
            # the tell that the when must bind greedily.
            n0 = len(sup.scheduler.rows)
            result = agent.run("remind me at 5 to 6 to go",
                               mode="auto")
            self.assertIn("Reminder set", result.content)
            self.assertEqual(len(sup.scheduler.rows), n0 + 1)
            g_row = sup.scheduler.rows[-1]
            self.assertEqual(g_row["action"]["detail"], "go")
            self.assertEqual(
                (time.localtime(g_row["at"]).tm_hour,
                 time.localtime(g_row["at"]).tm_min), (17, 55))
            # Non-clock digits in the task stay tasks.
            result = agent.run("remind me at 5 to take 6 pills",
                               mode="auto")
            self.assertIn("take 6 pills", result.content)
            # Noun-first fragments — 'reminder for my dentist
            # appointment tomorrow' got a model 'reminder set for
            # 15:00' over zero rows; 'reminders for me' is a scoped
            # inventory ask, not a create with a 'me' task.
            n0 = len(sup.scheduler.rows)
            result = agent.run(
                "reminder for my dentist appointment tomorrow",
                mode="auto")
            self.assertIn("Reminder set", result.content)
            self.assertEqual(len(sup.scheduler.rows), n0 + 1)
            result = agent.run("reminders for me", mode="auto")
            self.assertIn("reminder", result.content.lower())
            self.assertNotIn("do what", result.content)
            # Gerund/keep creates, referential scope, verb-typos —
            # 'keep reminding me every friday' got a model 'already
            # set' over zero rows; 'that reminder at 3' and 'reminder
            # me at 3' got fake 'I've set the reminder for Friday at
            # 3 PM' narrations over zero rows.
            result = agent.run("keep reminding me every friday",
                               mode="auto")
            self.assertIn("do what", result.content)
            result = agent.run("keep reminding me to stretch",
                               mode="auto")
            self.assertIn("When should", result.content)
            n0 = len(sup.scheduler.rows)
            result = agent.run(
                "keep reminding me every friday to stretch",
                mode="auto")
            self.assertIn("Reminder set", result.content)
            self.assertEqual(len(sup.scheduler.rows), n0 + 1)
            n0 = len(sup.scheduler.rows)
            result = agent.run("that reminder at 3", mode="auto")
            self.assertIn("reminder", result.content.lower())
            self.assertEqual(len(sup.scheduler.rows), n0)
            result = agent.run("reminder me at 3", mode="auto")
            self.assertIn("do what", result.content)
            n0 = len(sup.scheduler.rows)
            result = agent.run("reminder me at 3 to leave",
                               mode="auto")
            self.assertIn("Reminder set", result.content)
            self.assertEqual(len(sup.scheduler.rows), n0 + 1)
            # Article/adjective leads — 'a reminder for 3pm' and
            # 'new reminder at 4' got model fake-sets over zero
            # rows; 'the reminder at 3' stays referential.
            result = agent.run("a reminder for 3pm", mode="auto")
            self.assertIn("do what", result.content)
            result = agent.run("new reminder at 4", mode="auto")
            self.assertIn("do what", result.content)
            n0 = len(sup.scheduler.rows)
            result = agent.run("an alarm at 7", mode="auto")
            self.assertIn("Alarm set", result.content)
            self.assertEqual(len(sup.scheduler.rows), n0 + 1)
            # Leading-time creates — 'in 20 minutes remind me to
            # stretch' and 'at 6am wake me up' got model fake-sets
            # over zero rows; 'every 30 minutes ping me' got a fake
            # "That's set" with no task at all.
            sup.scheduler.rows.clear()
            n0 = len(sup.scheduler.rows)
            result = agent.run(
                "in 20 minutes remind me to stretch", mode="auto")
            self.assertIn("Reminder set", result.content)
            self.assertEqual(len(sup.scheduler.rows), n0 + 1)
            self.assertEqual(sup.scheduler.rows[-1]
                             ["action"]["detail"], "stretch")
            n0 = len(sup.scheduler.rows)
            result = agent.run("at 6am wake me up", mode="auto")
            self.assertIn("Reminder set", result.content)
            self.assertEqual(len(sup.scheduler.rows), n0 + 1)
            self.assertEqual(sup.scheduler.rows[-1]
                             ["action"]["detail"], "wake you")
            n0 = len(sup.scheduler.rows)
            result = agent.run("every 30 minutes ping me", mode="auto")
            self.assertIn("do what", result.content)
            self.assertEqual(len(sup.scheduler.rows), n0)
            # Verb-led declaratives — 'set a reminder to call mom'
            # and 'can i get a reminder' got model riffs.
            result = agent.run("set a reminder to call mom",
                               mode="auto")
            self.assertIn("call mom", result.content)
            result = agent.run("can i get a reminder", mode="auto")
            self.assertIn("fire", result.content)
            result = agent.run("i'll need a reminder for tomorrow",
                               mode="auto")
            self.assertIn("do what", result.content)
            # Bare verb+me — 'wake me'/'ping me' got model fake-sets
            # ('I'll set a reminder to wake you at 3 AM') over zero
            # rows; waking is its own task so the ask is the time.
            result = agent.run("wake me", mode="auto")
            self.assertIn("when", result.content.lower())
            result = agent.run("ping me", mode="auto")
            self.assertIn("do what", result.content)
            # Help-idiom creates — 'don't let me forget to call mom'
            # banked 'Never let me forget to call mom' as a rule;
            # 'make sure i remember the dentist at 3' banked garbled
            # text. Both are creates; reminder-shaped turns must
            # never bank.
            result = agent.run("don't let me forget to call mom",
                               mode="auto")
            self.assertIn("call mom", result.content)
            n0 = len(sup.scheduler.rows)
            result = agent.run("make sure i remember the dentist "
                               "at 3", mode="auto")
            self.assertIn("Reminder set", result.content)
            self.assertEqual(len(sup.scheduler.rows), n0 + 1)
            self.assertFalse(
                agent.conversation_memory.recall_facts("dentist"))
            # Terse forms — 'reminders off' got a model 'all inactive'
            # over untouched rows; 'snooze everything' got a false
            # capability refusal. Both are real pool mutations now.
            sup.scheduler.add(
                "reminder: terse", "once",
                at=time.time() + 3600,
                action={"kind": "notify", "detail": "terse"})
            result = agent.run("reminders off", mode="auto")
            self.assertIn("off", result.content.lower())
            self.assertFalse(sup.scheduler.rows[-1]["enabled"])
            result = agent.run("reminders on", mode="auto")
            self.assertIn("on", result.content.lower())
            self.assertTrue(sup.scheduler.rows[-1]["enabled"])
            t0 = sup.scheduler.rows[-1]["at"]
            result = agent.run("snooze everything", mode="auto")
            self.assertIn("Snoozed", result.content)
            self.assertAlmostEqual(
                sup.scheduler.rows[-1]["at"], t0 + 600, 0)
            # 'change the timer to 30 minutes' — a duration target
            # got 'Reschedule to when?'; the in-prefix reads it.
            sup.scheduler.rows.clear()
            sup.scheduler.add(
                "timer: terse", "once", at=time.time() + 600,
                action={"kind": "notify", "detail": "terse"})
            result = agent.run("change the timer to 30 minutes",
                               mode="auto")
            self.assertIn("Rescheduled", result.content)
            # Negation tasks, wake-creates, ordinal scope — 'remind
            # me not to forget' got a model 'scheduled for now';
            # 'wake me up in an hour' is self-tasking like an alarm;
            # 'cancel the next reminder' picks the soonest fire.
            result = agent.run("remind me not to forget",
                               mode="auto")
            self.assertIn("not to forget", result.content)
            n0 = len(sup.scheduler.rows)
            result = agent.run("wake me up in an hour", mode="auto")
            self.assertIn("Reminder set", result.content)
            self.assertEqual(len(sup.scheduler.rows), n0 + 1)
            sup.scheduler.rows.clear()
            sup.scheduler.add(
                "reminder: far", "once", at=time.time() + 86400,
                action={"kind": "notify", "detail": "far"})
            sup.scheduler.add(
                "reminder: soon", "once", at=time.time() + 600,
                action={"kind": "notify", "detail": "soon"})
            result = agent.run("cancel the next reminder",
                               mode="auto")
            self.assertIn("soon", result.content)
            self.assertEqual(len(sup.scheduler.rows), 1)
            sup.scheduler.rows.clear()

            # Possessive scopes — 'cancel tomorrow's reminder' left
            # 'tomorrow's' as one token that never matched hay
            # 'tomorrow'; "what's my next reminder" echoed scope 's.
            sup.scheduler.add(
                "reminder: poss", "once",
                at=time.time() + 86400,
                action={"kind": "notify", "detail": "poss"})
            result = agent.run("cancel tomorrow's reminder",
                               mode="auto")
            self.assertIn("Cancel", result.content)
            self.assertFalse(sup.scheduler.rows)
            sup.scheduler.add(
                "reminder: poss2", "once",
                at=time.time() + 3600,
                action={"kind": "notify", "detail": "poss2"})
            result = agent.run("what's my next reminder", mode="auto")
            self.assertIn("poss2", result.content)
            sup.scheduler.remove(str(sup.scheduler.rows[-1]["id"]))

            # 'next' is ordinal in inventory too — 'when does my
            # next reminder fire' listed every row; and a noun-scoped
            # miss must name the noun — 'any alarms' answered 'No
            # reminders set' while reminder rows existed.
            sup.scheduler.add(
                "reminder: soonest", "once",
                at=time.time() + 600,
                action={"kind": "notify", "detail": "soonest"})
            sup.scheduler.add(
                "reminder: later", "once",
                at=time.time() + 86400,
                action={"kind": "notify", "detail": "later"})
            result = agent.run("when does my next reminder fire",
                               mode="auto")
            self.assertIn("soonest", result.content)
            self.assertNotIn("later", result.content)
            result = agent.run("any alarms", mode="auto")
            self.assertIn("No alarms", result.content)
            self.assertIn("soonest", result.content)
            # Imperative/fragment inventory leads — 'tell me about
            # my reminders' and 'anything scheduled' got model
            # essays claiming 'nothing scheduled' over live rows;
            # 'what comes up next' answers the soonest row.
            for lead in ("tell me about my reminders",
                         "run through my reminders",
                         "list my reminders",
                         "anything scheduled"):
                result = agent.run(lead, mode="auto")
                self.assertIn("reminder", result.content.lower(),
                              lead)
                self.assertIn("soonest", result.content, lead)
                self.assertNotIn("about “about”", result.content,
                                 lead)
            result = agent.run("what comes up next", mode="auto")
            self.assertIn("soonest", result.content)
            self.assertNotIn("later", result.content)
            # 'anything scheduled for tonight' is a question about the
            # scheduler, not a fact — the learn lane banked it live and
            # the restate-ack then outranked the real schedule answer.
            result = agent.run("anything scheduled for tonight",
                               mode="auto")
            self.assertIn("soonest", result.content)
            self.assertFalse(
                agent.conversation_memory.recall_facts("scheduled"))
            sup.scheduler.rows.clear()

            # When-only utterances — 'in two hours' and 'at half 6'
            # got model 'Alarm set for in two hours. Got it.' acks
            # over zero rows; a parseable when asks for the task, an
            # unparseable one asks for a clock.
            for bare_when, needle in (
                    ("in two hours", "do what in two hours"),
                    ("at half 6", "do what at half 6"),
                    ("tomorrow", "do what tomorrow"),
                    ("5 minutes", "do what in 5 minutes"),
                    ("later", "couldn't pin a time")):
                n0 = len(sup.scheduler.rows)
                result = agent.run(bare_when, mode="auto")
                self.assertIn(needle, result.content, bare_when)
                self.assertEqual(len(sup.scheduler.rows), n0,
                                 bare_when)
            # Copula-guard — 'tomorrow is friday' and 'this is my
            # favorite mug' are facts, not reminder asks; the
            # when-only regex must not suppress banking them.
            learned = agent.conversation_memory.learn_from_user(
                "this is my favorite mug", project_id="x")
            self.assertTrue(learned)
            # Adjective-lead noun fragments — 'weekday reminders'
            # got a model 'set for 4' claim over zero rows.
            n0 = len(sup.scheduler.rows)
            result = agent.run("weekday reminders", mode="auto")
            self.assertIn("do what", result.content)
            self.assertIn("weekday", result.content)
            self.assertEqual(len(sup.scheduler.rows), n0)
            n0 = len(sup.scheduler.rows)
            result = agent.run("morning alarm at 7", mode="auto")
            self.assertIn("Alarm set", result.content)
            self.assertEqual(len(sup.scheduler.rows), n0 + 1)

            # Bare possessive inventory — 'my reminders' got a
            # model 'I'll set a reminder for you in 5 minutes'
            # fake-set; it's an inventory ask.
            sup.scheduler.rows.clear()
            sup.scheduler.add(
                "reminder: gym", "once",
                at=time.time() + 86400,
                action={"kind": "notify", "detail": "gym"})
            result = agent.run("my reminders", mode="auto")
            self.assertIn("gym", result.content)
            result = agent.run("my schedule", mode="auto")
            self.assertIn("gym", result.content)
            # 'another reminder for 4' — 'for' leads a bare clock,
            # not a task named '4'.
            n0 = len(sup.scheduler.rows)
            result = agent.run("another reminder for 4", mode="auto")
            self.assertIn("do what at 4", result.content)
            self.assertEqual(len(sup.scheduler.rows), n0)
            # 'clear my schedule' — 'schedule' is a pool-wide noun;
            # the model refused with 'no access to your calendar'
            # while real rows sat in the scheduler.
            result = agent.run("clear my schedule", mode="auto")
            self.assertIn("Cancelled", result.content)
            self.assertFalse(sup.scheduler.rows)
            sup.scheduler.rows.clear()

            # Pool-scoped reschedule — 'reschedule everything to
            # tomorrow' got a model 'no access to your calendar'
            # refusal; single-row binds, multi-row asks which.
            sup.scheduler.add(
                "reminder: pool1", "once",
                at=time.time() + 3600,
                action={"kind": "notify", "detail": "pool1"})
            result = agent.run("reschedule everything to tomorrow",
                               mode="auto")
            self.assertIn("Rescheduled", result.content)
            sup.scheduler.add(
                "reminder: pool2", "once",
                at=time.time() + 7200,
                action={"kind": "notify", "detail": "pool2"})
            result = agent.run("reschedule everything to 9am",
                               mode="auto")
            self.assertIn("matches 2", result.content)
            sup.scheduler.rows.clear()

            # Snooze idioms — 'give me 5 more minutes' got a model
            # 'That's now set for 20:26' over an unchanged row; the
            # imminent one-shot is the intent. 'make it earlier'
            # went to the editor lane. Bare directions on the pool
            # ('snooze everything earlier') must move rows back,
            # not push them forward.
            near = sup.scheduler.add(
                "reminder: near", "once",
                at=time.time() + 600,
                action={"kind": "notify", "detail": "near"})
            far = sup.scheduler.add(
                "reminder: far", "once",
                at=time.time() + 86400,
                action={"kind": "notify", "detail": "far"})
            result = agent.run("give me 5 more minutes", mode="auto")
            self.assertIn("Snoozed", result.content)
            self.assertAlmostEqual(
                near["at"], time.time() + 900, delta=30)
            self.assertAlmostEqual(
                far["at"], time.time() + 86400, delta=30)
            result = agent.run("make them earlier", mode="auto")
            self.assertIn("Snoozed 2", result.content)
            self.assertAlmostEqual(
                near["at"], time.time() + 300, delta=30)
            self.assertAlmostEqual(
                far["at"], time.time() + 85800, delta=30)
            sup.scheduler.rows.clear()

            # Adjective cadences and event triggers — 'biweekly
            # reminder to call mom' got a model 'set for 14 days'
            # over zero rows; the adjective IS the cadence.
            # 'remind me before the meeting' is a trigger ask, not
            # a recall topic. 'remind me in advance' is timeish.
            n0 = len(sup.scheduler.rows)
            result = agent.run("biweekly reminder to call mom",
                               mode="auto")
            self.assertIn("Reminder set", result.content)
            self.assertEqual(len(sup.scheduler.rows), n0 + 1)
            self.assertEqual(sup.scheduler.rows[-1]["kind"],
                             "interval")
            n0 = len(sup.scheduler.rows)
            result = agent.run("weekday reminders to stretch",
                               mode="auto")
            self.assertIn("Reminder set", result.content)
            self.assertEqual(sup.scheduler.rows[-1]["weekday"],
                             [0, 1, 2, 3, 4])
            n0 = len(sup.scheduler.rows)
            result = agent.run("remind me before the meeting",
                               mode="auto")
            self.assertIn("trigger", result.content)
            self.assertEqual(len(sup.scheduler.rows), n0)
            result = agent.run("remind me in advance", mode="auto")
            self.assertIn("couldn't pin a time", result.content)
            sup.scheduler.rows.clear()

            # Bare nouns and dangling connectors — 'timer' got a
            # model 'Timer set for 30 minutes. It'll expire at
            # 6:51 PM.' over zero rows; 'reminders?' got 'No active
            # reminders' over a live row; 'wake me at' got a false
            # 'no alarm setup exists' refusal.
            sup.scheduler.add(
                "alarm: wake", "once",
                at=time.time() + 43200,
                action={"kind": "notify", "detail": "wake"})
            n0 = len(sup.scheduler.rows)
            result = agent.run("timer", mode="auto")
            self.assertIn("When should", result.content)
            self.assertEqual(len(sup.scheduler.rows), n0)
            result = agent.run("reminders?", mode="auto")
            self.assertIn("wake", result.content)
            result = agent.run("remind me to", mode="auto")
            self.assertIn("do what", result.content)
            result = agent.run("remind me tomorrow to", mode="auto")
            self.assertIn("do what tomorrow", result.content)
            result = agent.run("wake me at", mode="auto")
            self.assertIn("when", result.content.lower())
            # 'change my alarm to 7' — a bare clock reschedule
            # target got 'Reschedule to when?'.
            result = agent.run("change my alarm to 7", mode="auto")
            self.assertIn("Rescheduled", result.content)
            # 'no reminders' — a pool stop; the model corrected the
            # spelling instead of disabling anything.
            result = agent.run("no reminders", mode="auto")
            self.assertIn("off", result.content.lower())
            self.assertFalse(sup.scheduler.rows[-1]["enabled"])
            sup.scheduler.rows.clear()

            # 'every couple hours' parses; 'urgent/important' are
            # adjective leads, not fake-set material; 'anything at
            # 6' is a scoped inventory ask.
            n0 = len(sup.scheduler.rows)
            result = agent.run("urgent reminder for tomorrow",
                               mode="auto")
            self.assertIn("do what tomorrow", result.content)
            self.assertEqual(len(sup.scheduler.rows), n0)
            result = agent.run("important reminder to call mom",
                               mode="auto")
            self.assertIn("call mom", result.content)
            result = agent.run("anything at 6", mode="auto")
            self.assertIn("No reminder", result.content)
            result = agent.run("what's at 3", mode="auto")
            self.assertIn("No reminder", result.content)
            sup.scheduler.rows.clear()

            # 'at 6 in the morning' — the daypart phrase is the
            # meridiem. The server splitter used to pick 'in the
            # morning' as the when and bank 'at 6' as the task.
            result = agent.run(
                "remind me at 6 in the morning to stretch",
                mode="auto")
            self.assertIn("stretch", result.content)
            self.assertTrue(sup.scheduler.rows)
            row = sup.scheduler.rows[-1]
            self.assertIn("stretch", row["name"])
            self.assertNotEqual(row["name"], "reminder: at 6")
            self.assertEqual(
                time.localtime(row["at"]).tm_hour, 6)
            result = agent.run(
                "remind me at 6 in the morning", mode="auto")
            self.assertIn("do what", result.content)
            # 'saturday mornings' / 'on the weekend' — day+daypart
            # and article-led pool cadences are real schedules.
            sup.scheduler.rows.clear()
            result = agent.run(
                "saturday mornings remind me to mow", mode="auto")
            row = sup.scheduler.rows[-1]
            self.assertEqual(row["kind"], "weekly")
            self.assertEqual(row["weekday"], 5)
            self.assertEqual(row["hour"], 9)
            result = agent.run(
                "on the weekend remind me to relax", mode="auto")
            row = sup.scheduler.rows[-1]
            self.assertEqual(row["kind"], "weekly")
            self.assertEqual(row["weekday"], [5, 6])
            result = agent.run("saturday mornings remind me",
                               mode="auto")
            self.assertIn("do what", result.content)
            sup.scheduler.rows.clear()

            # 'cancel the reminder pay' — the topic trails the noun
            # bare. The model used to narrate 'Cancelled the
            # reminder "pay"' and 'No active reminder named X'
            # while the row sat untouched in the store.
            sup.scheduler.add(
                "Reminder: pay rent", "once",
                at=time.time() + 86400,
                action={"kind": "notify", "detail": "pay rent"})
            sup.scheduler.add(
                "Reminder: stretch", "once",
                at=time.time() + 90000,
                action={"kind": "notify", "detail": "stretch"})
            sup.scheduler.add(
                "Reminder: pay", "once",
                at=time.time() + 90000,
                action={"kind": "notify", "detail": "pay"})
            n0 = len(sup.scheduler.rows)
            result = agent.run("cancel the reminder stretch",
                               mode="auto")
            self.assertIn("Cancelled", result.content)
            self.assertEqual(len(sup.scheduler.rows), n0 - 1)
            # 'pay' matches both 'pay' and 'pay rent' — ambiguity
            # must ask, never pick.
            result = agent.run("cancel the reminder pay",
                               mode="auto")
            self.assertIn("matches", result.content)
            result = agent.run("cancel the reminder nonexistent",
                               mode="auto")
            self.assertIn("don't see", result.content)
            sup.scheduler.rows.clear()

            # 'day after tomorrow' is one +2d anchor — the split
            # used to bank 'day after' as the task at 'tomorrow'.
            result = agent.run(
                "remind me day after tomorrow to call bank",
                mode="auto")
            self.assertIn("call bank", result.content)
            self.assertTrue(sup.scheduler.rows)
            row = sup.scheduler.rows[-1]
            self.assertIn("call bank", row["name"])
            self.assertNotIn("day after", row["name"])
            lt = time.localtime(row["at"])
            self.assertEqual(lt.tm_mday,
                             time.localtime(
                                 time.time() + 2 * 86400).tm_mday)
            # 'in an hour and a half' — the trailing fraction is
            # real duration, not a model essay.
            result = agent.run(
                "remind me in an hour and a half to stretch",
                mode="auto")
            row = sup.scheduler.rows[-1]
            self.assertIn("stretch", row["name"])
            delta = row["at"] - time.time()
            self.assertGreater(delta, 80 * 60)
            self.assertLess(delta, 100 * 60)
            # 'how long until my next reminder' — a countdown ask;
            # the model guessed 'in 4 hours' over a 4-day row.
            result = agent.run("how long until my next reminder",
                               mode="auto")
            self.assertIn("in ", result.content)
            self.assertIn("stretch", result.content)
            result = agent.run("time until my next reminder",
                               mode="auto")
            self.assertIn("in ", result.content)
            sup.scheduler.rows.clear()

            # 'at 6:30 pm friday' — clock-first weekday. The
            # splitter banked 'at 6:30 pm' as the task at Fri 9am.
            result = agent.run(
                "remind me at 6:30 pm friday to call", mode="auto")
            self.assertIn("call", result.content)
            row = sup.scheduler.rows[-1]
            self.assertIn("call", row["name"])
            self.assertNotIn("6:30", row["name"])
            lt = time.localtime(row["at"])
            self.assertEqual(lt.tm_hour, 18)
            self.assertEqual(lt.tm_min, 30)
            self.assertEqual(lt.tm_wday, 4)
            # 'in a few minutes' — vague; honest pin-fail, never
            # the model's 'The timer is 30 minutes' invention.
            n0 = len(sup.scheduler.rows)
            result = agent.run("remind me in a few minutes",
                               mode="auto")
            self.assertIn("couldn't pin", result.content)
            self.assertEqual(len(sup.scheduler.rows), n0)
            sup.scheduler.rows.clear()

            # A banked answer must never outrank the reminder
            # lane — live, 'remind me in a few minutes' was
            # answered 'The timer is 30 minutes' from answer
            # memory via has_memory_answer, before the lane ran.
            class _AMHit:
                hit = True
            class _AM:
                available = True
                def lookup(self, *a, **kw):
                    return _AMHit()
            agent.answer_memory = _AM()
            self.assertFalse(
                agent.has_memory_answer(
                    "remind me in a few minutes"))
            agent.answer_memory = None

            # And inside run(): single-fact recall must not answer
            # a time-shaped reminder ask with banked trivia —
            # live it replied 'The timer is 30 minutes' from a
            # narrated fake-set fact. 'remind me about X' keeps
            # its deliberate recall behavior.
            agent.conversation_memory.learn_from_user(
                "the deploy tag is v42", project_id="x")
            agent.conversation_memory.learn_from_user(
                "the timer is 30 minutes", project_id="x")
            self.assertIsNone(agent._single_fact_recall_reply(
                "remind me in a few minutes", "conv", "x"))
            self.assertIsNone(agent._single_fact_recall_reply(
                "remind me tomorrow at 9", "conv", "x"))
            reply = agent._single_fact_recall_reply(
                "remind me about the deploy tag", "conv", "x")
            self.assertIsNotNone(reply)
            self.assertIn("v42", reply.text)

            # A FACT can literally name a timer — 'stop
            # remembering the timer is 30 minutes' must un-bank
            # the narrated fact, not bounce to the cancel lane.
            learned = agent.conversation_memory.learn_from_user(
                "stop remembering the timer is 30 minutes",
                project_id="x")
            self.assertTrue(
                any("30 minutes" in f.get("text", "")
                    for f in learned.get("forgotten", [])))
            learned = agent.conversation_memory.learn_from_user(
                "stop remembering the 9pm alarm",
                project_id="x")
            self.assertTrue(learned.get("forgotten_miss"))

            # 'at 5 tomorrow evening' — the trailing daypart is
            # the meridiem; the model pin-failed 'at 5 tomorrow
            # evening' over a real grammar gap.
            sup.scheduler.rows.clear()
            result = agent.run(
                "ping me at 5 tomorrow evening to stretch",
                mode="auto")
            self.assertIn("stretch", result.content)
            row = sup.scheduler.rows[-1]
            lt = time.localtime(row["at"])
            self.assertEqual(lt.tm_hour, 17)
            sup.scheduler.rows.clear()

            # Noun-scoped miss — 'move my alarm to 8' over a pool
            # holding only 'pay rent' must name the real row, not
            # claim nothing is set.
            sup.scheduler.add(
                "Reminder: pay rent", "once", at=time.time() + 86400)
            result = agent.run("move my alarm to 8", mode="auto")
            self.assertIn("No alarm set", result.content)
            self.assertIn("pay rent", result.content)
            result = agent.run("turn off the 7am alarm",
                               mode="auto")
            self.assertIn("No alarm set", result.content)
            self.assertIn("pay rent", result.content)

            # 'cancel everything' — pool-wide cancel through the
            # deterministic lane; the model fake-refused 'no
            # active reminders' over a live pool.
            sup.scheduler.add(
                "Reminder: dentist", "once",
                at=time.time() + 90000)
            result = agent.run("cancel everything", mode="auto")
            self.assertIn("Cancelled 2", result.content)
            self.assertFalse(sup.scheduler.rows)

            # The mangled-text probe the model actually saw live —
            # it must now stay 'unpause' end to end.
            from localcodeagent.context.spelling import (
                normalize_user_text)
            normalized, fixes = normalize_user_text(
                "unpause my reminders")
            self.assertEqual(normalized, "unpause my reminders")
            self.assertFalse(fixes)

            # No ephemeral control turn banks a durable rule.
            self.assertFalse(
                agent.conversation_memory.recall_facts("unpause"))
            learned = agent.conversation_memory.learn_from_user(
                "unpause my reminders", project_id="x")
            self.assertFalse(learned.get("behavior_rules"))
            # 'never <notify/remind/ping/wake> me…' is the same
            # reminder-scope prohibition wearing a 'never' lead — it
            # bypassed the revoke-path exemption and banked
            # 'Never notify me about the build' live. Genuine 'never'
            # prohibitions still bank.
            for ephemeral_never in (
                    "never notify me about the build again",
                    "never remind me about the deploy",
                    "never ping me on sundays",
                    "never wake me up at 6am",
                    "i don't want you to remind me about the deploy"):
                learned = agent.conversation_memory.learn_from_user(
                    ephemeral_never, project_id="x")
                self.assertFalse(learned.get("behavior_rules"),
                                 ephemeral_never)
            learned = agent.conversation_memory.learn_from_user(
                "never use my real name in commit messages",
                project_id="x")
            self.assertTrue(learned.get("behavior_rules"))

    def test_rules_inventory_reports_true_count(self):
        # Live: "what rules are you following right now" listed 8 of
        # 10 active rules with no indication the set was truncated —
        # a self-inspection lane misreporting its own rule book.
        from localcodeagent.workflow.conversation_memory import (
            ConversationMemory)
        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(Path(td), _FakeRuntime())
            agent.conversation_memory = ConversationMemory(
                Path(td) / "cm.json")
            cm = agent.conversation_memory
            r = agent._facts_recall_reply(
                "what rules are you following right now",
                "conv", "proj")
            self.assertIsNotNone(r)
            self.assertIn("no standing rules", r.text.lower())
            for i in range(3):
                cm.learn_from_user(f"never run job {i} on friday")
            r = agent._facts_recall_reply(
                "what rules are you following right now",
                "conv", "proj")
            self.assertIsNotNone(r)
            self.assertIn("operating rules", r.text.lower())
            self.assertNotIn("more.", r.text.lower())
            for i in range(3, 10):
                cm.learn_from_user(f"never run job {i} on friday")
            r = agent._facts_recall_reply(
                "what rules are you following right now",
                "conv", "proj")
            self.assertIsNotNone(r)
            self.assertIn("10 operating rules", r.text.lower())
            self.assertIn("2 more", r.text.lower())
            # "how many rules" counts the STORE, not the last list —
            # the model lane once answered "eight" off the truncated
            # rendering while ten were active.
            r = agent._facts_recall_reply(
                "how many rules are active", "conv", "proj")
            self.assertIsNotNone(r)
            self.assertIn("10 rules are active", r.text.lower())

    def test_scoped_recall_and_inventory_asks(self):
        # Live: "do you have anything on the backup schedule" dumped
        # the AUTONOMY inventory (standing goals/missions) instead of
        # the banked fact, and "what do you remember about my shell"
        # dumped every fact in the store. Scoped asks must resolve the
        # named subject only; "what do you" must not blanket-skip
        # memory-shaped verbs (have/know/remember).
        from localcodeagent.workflow.conversation_memory import (
            ConversationMemory)
        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(Path(td), _FakeRuntime())
            agent.conversation_memory = ConversationMemory(
                Path(td) / "cm.json")
            agent.conversation_memory.learn_from_user(
                "the backup schedule is every day at 3am")
            agent.conversation_memory.learn_from_user(
                "retry limit is 10")
            agent.conversation_memory.learn_from_user(
                "my shell is zsh")
            for q, want in (
                    ("do you have anything on the backup schedule",
                     "3am"),
                    ("what do you have on the retry limit", "10"),
                    ("got anything about the backup schedule", "3am"),
                    ("you got any info on the retry limit", "10"),
                    ("what do you know about the retry limit", "10"),
                    ("what do you remember about my shell", "zsh")):
                reply = (agent._single_fact_recall_reply(q, "conv",
                                                       "proj")
                         or agent._facts_recall_reply(q, "conv", "proj"))
                self.assertIsNotNone(reply, q)
                self.assertIn(want, reply.text, q)
            # Scoped dump returns ONLY the matching fact — the whole
            # store must not ride along.
            reply = agent._facts_recall_reply(
                "what do you remember about my shell", "conv", "proj")
            self.assertIsNotNone(reply)
            self.assertIn("zsh", reply.text)
            self.assertNotIn("retry", reply.text)
            # Unscoped/about-me still dumps; non-memory "what do you"
            # stays skipped; unknown scopes fall through.
            for q in ("what do you do on weekends",
                      "what do you think about it",
                      "do you have something on the deploy pipeline"):
                self.assertIsNone(
                    agent._single_fact_recall_reply(q, "conv", "proj"),
                    q)

    def test_ever_tell_and_fact_count(self):
        # Live: "did I ever tell you my editor" got a self-contradictory
        # model reply ("No — ... You told me that") because 'ever'
        # sat between the aux and the verb the recall lead expected;
        # "how many facts do you have" got a persona-drifted evasion
        # instead of the actual count.
        from localcodeagent.workflow.conversation_memory import (
            ConversationMemory)
        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(Path(td), _FakeRuntime())
            agent.conversation_memory = ConversationMemory(
                Path(td) / "cm.json")
            agent.conversation_memory.learn_from_user(
                "my editor is cursor")
            agent.conversation_memory.learn_from_user(
                "retry limit is 10")
            r = agent._single_fact_recall_reply(
                "did I ever tell you my editor", "conv", "proj")
            self.assertIsNotNone(r)
            self.assertIn("cursor", r.text)
            # Negative case — the honest "you haven't told me" answer.
            r = agent._single_fact_recall_reply(
                "did I ever tell you my birthday", "conv", "proj")
            self.assertIsNotNone(r)
            self.assertIn("haven't told me", r.text)
            r = agent._facts_recall_reply(
                "how many facts do you have", "conv", "proj")
            self.assertIsNotNone(r)
            self.assertIn("2", r.text)
            # "how many things are running" is an autonomy ask — never
            # a fact count.
            self.assertIsNone(
                agent._facts_recall_reply(
                    "how many things are running", "conv", "proj"))

    def test_topic_pivot_preamble_strips_on_learn(self):
        # "speaking of ports, prod uses 8443" — the bankable clause
        # follows the pivot's comma. The pivot itself must not bank
        # (it did not, but only because the comma routed the turn
        # away — the strip makes the tail reachable deliberately).
        from localcodeagent.workflow.conversation_memory import (
            ConversationMemory)
        with tempfile.TemporaryDirectory() as td:
            cm = ConversationMemory(Path(td) / "cm.json")
            for q, want in (
                    ("speaking of ports, prod uses 8443",
                     ["prod uses 8443"]),
                    ("as for the deadline, it is friday", []),  # 'it' anaphora
                    ("regarding the cache, it expires hourly",
                     [])):  # ditto
                self.assertEqual(cm.learn_from_user(q)["facts"], want, q)

    def test_port_probe_is_answer_type_slot(self):
        # "which port is prod on" — 'port' names the answer's type, not
        # the entity; the stored fact is 'prod api is on 443' and never
        # carries the word 'port' (live-observed 503).
        from localcodeagent.workflow.conversation_memory import (
            ConversationMemory)
        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(Path(td), _FakeRuntime())
            agent.conversation_memory = ConversationMemory(
                Path(td) / "cm.json")
            agent.conversation_memory.learn_from_user(
                "prod api is on 443")
            agent.conversation_memory.learn_from_user(
                "staging api uses port 8443")
            reply = agent._single_fact_recall_reply(
                "which port is prod on", "conv", "proj")
            self.assertIsNotNone(reply)
            self.assertIn("443", reply.text)
            self.assertNotIn("8443", reply.text)
            # A fact that merely *mentions* 'prod' in its value loses
            # to the subject-anchored one — the question asks about the
            # entity, not an incidental tail term.
            agent.conversation_memory.learn_from_user(
                "staging db is postgres and prod is mysql")
            reply = agent._single_fact_recall_reply(
                "which port is prod on then", "conv", "proj")
            self.assertIsNotNone(reply)
            self.assertEqual("The prod api is on 443.", reply.text)

    def test_compound_spelling_and_verification_recall(self):
        # 'on call' queries a banked 'oncall' — English writes compounds
        # both ways, and a strict token gate starves the split spelling.
        # 'is it still X' is a verification probe — recall restates the
        # fact instead of falling to the model.
        from localcodeagent.workflow.conversation_memory import (
            ConversationMemory)
        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(Path(td), _FakeRuntime())
            agent.conversation_memory = ConversationMemory(
                Path(td) / "cm.json")
            agent.conversation_memory.learn_from_user(
                "the oncall used to be priya, now it's sam")
            agent.conversation_memory.learn_from_user(
                "prod api is on 443")
            for probe in ("who is on call", "who's on call",
                          "is it still sam"):
                reply = agent._single_fact_recall_reply(
                    probe, "conv", "proj")
                self.assertIsNotNone(reply, probe)
                self.assertIn("sam", reply.text)
            reply = agent._single_fact_recall_reply(
                "is the prod api still on 443", "conv", "proj")
            self.assertIsNotNone(reply)
            self.assertIn("443", reply.text)

    def test_does_the_x_verification_recalls_fact(self):
        # "does the metrics api run on 9100" — a yes/no verification
        # with an explicit subject. The interrogative verb ('run') is
        # filler the fact never carries; recall restates the fact
        # rather than stalling the model lane (live-observed 180s
        # stall on the shared GPU).
        from localcodeagent.workflow.conversation_memory import (
            ConversationMemory)
        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(Path(td), _FakeRuntime())
            agent.conversation_memory = ConversationMemory(
                Path(td) / "cm.json")
            agent.conversation_memory.learn_from_user(
                "the metrics api is on 9100")
            reply = agent._single_fact_recall_reply(
                "does the metrics api run on 9100", "conv", "proj")
            self.assertIsNotNone(reply)
            self.assertIn("9100", reply.text)
            # A wrong value in the question doesn't confirm — recall
            # still surfaces the stored fact verbatim.
            reply = agent._single_fact_recall_reply(
                "does the metrics api use port 8080", "conv", "proj")
            self.assertIsNotNone(reply)
            self.assertIn("9100", reply.text)

    def test_when_does_probe_drops_trailing_verb(self):
        # "when does the freeze lift" — the probe's own verb isn't a
        # fact term; after the strict subset fails, retry on the noun
        # phrase alone so 'code freeze lifts on monday' answers.
        from localcodeagent.workflow.conversation_memory import (
            ConversationMemory)
        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(Path(td), _FakeRuntime())
            agent.conversation_memory = ConversationMemory(
                Path(td) / "cm.json")
            agent.conversation_memory.learn_from_user(
                "the code freeze lifts on monday")
            reply = agent._single_fact_recall_reply(
                "when does the freeze lift", "conv", "proj")
            self.assertIsNotNone(reply)
            self.assertIn("monday", reply.text)
            self.assertIsNone(agent._single_fact_recall_reply(
                "when does the comet arrive", "conv", "proj"))

    def test_when_do_plural_probe_matches_singular_fact(self):
        # "when do the backups run" — the plural probe binds the
        # singular 'backup schedule is …' fact (fold-tolerant terms)
        # and the trailing 'run' verb retries dropped.
        from localcodeagent.workflow.conversation_memory import (
            ConversationMemory)
        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(Path(td), _FakeRuntime())
            agent.conversation_memory = ConversationMemory(
                Path(td) / "cm.json")
            agent.conversation_memory.learn_from_user(
                "backup schedule is every day at 3am")
            reply = agent._single_fact_recall_reply(
                "when do the backups run", "conv", "proj")
            self.assertIsNotNone(reply)
            self.assertIn("3am", reply.text)

    def test_remind_me_probe_uses_recall_lane(self):
        # "remind me about my editor" is a memory recall request — the
        # frame must not classify 'remind' as an assertion and veto
        # the lane into a model riff.
        from localcodeagent.workflow.conversation_memory import (
            ConversationMemory)
        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(Path(td), _FakeRuntime())
            agent.conversation_memory = ConversationMemory(
                Path(td) / "cm.json")
            agent.conversation_memory.learn_from_user(
                "my editor is vim")
            reply = agent._single_fact_recall_reply(
                "remind me about my editor", "conv", "proj")
            self.assertIsNotNone(reply)
            self.assertIn("vim", reply.text)

    def test_echoed_notice_stripped_from_stored_reply(self):
        # BUG-029: the badge lives verbatim in stored history, so small
        # models learn to parrot it as their own sign-off. An echoed
        # notice is furniture — strip it before storage so it can't be
        # banked into Answer Memory as a trusted answer.
        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(Path(td), _FakeRuntime())
            provider = _ScriptedProvider([{
                "role": "assistant",
                "content": (
                    "Got it — keeping at it.\n\n"
                    "Everything's humming. ✅  \n\n"
                    "⚠ **Unverified action claims** — no tools or "
                    "commands ran in this reply. Statements asserting "
                    "completed actions above are narrative, not "
                    "confirmed execution."),
            }])
            agent._provider_for = lambda p, **kw: provider
            result = agent.run("keep working on this")
            self.assertNotIn("Unverified action claims", result.content)
            self.assertIn("humming", result.content)

    def test_badged_history_turns_are_stripped_for_model(self):
        # BUG-029: a genuinely badged reply stays badged in the visible
        # transcript, but annotation furniture must never re-enter the
        # model's context — feeding it back teaches the model to append
        # fake badges (and to imitate fabrication).
        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(Path(td), _FakeRuntime())
            provider = _ScriptedProvider([{
                "role": "assistant", "content": "Still here."}])
            agent._provider_for = lambda p, **kw: provider
            agent.run("how is it going", history=[
                {"role": "user", "content": "did you run the tests?"},
                {"role": "assistant",
                 "content": (
                     "I ran the tests — all green. ✅\n\n"
                     "⚠ **Unverified action claims** — no tools or "
                     "commands ran in this reply. Statements asserting "
                     "completed actions above are narrative, not "
                     "confirmed execution.")},
            ])
            sent = json.dumps(provider.last_messages or [],
                              ensure_ascii=False)
            self.assertNotIn("Unverified action claims", sent)
            # The flag's furniture AND the flagged claim text stay out —
            # the claims themselves are the parrot seed (each repeat
            # fabricates fresh telemetry); the verbatim record still
            # serves the recall lanes and UI transcript.
            self.assertNotIn("all green", sent)
            self.assertIn("withheld", sent)


class _DeadProcess:
    def poll(self):
        return 1

    @property
    def returncode(self):
        return 1


class BackendHealthTests(unittest.TestCase):
    def test_dead_backend_reports_exit_code_crash_and_log_tail(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            profile = _profile()
            manager = RuntimeManager(AgentConfig(profiles_onboarding_gate=False, models=[profile]),
                                     base_dir=root)
            log = root / "llama.log"
            log.write_text(
                "loading model\nggml_cuda error: out of memory\n"
                "llama server shutting down\n", encoding="utf-8")
            status = manager._status[profile.id]
            status.managed = True
            status.state = "error"
            status.log_path = str(log)
            status.error = "runtime exited with code 1"
            manager._managed[profile.id] = _ManagedProcess(
                profile, _DeadProcess(), "http://127.0.0.1:9999/v1",
                io.StringIO(), status)
            health = manager.backend_health(profile.id)
            self.assertEqual(health["exit_code"], 1)
            self.assertIsNone(health["pid"])
            self.assertIn("out of memory", health["log_tail"])
            # crash signature recognised — not just a bare socket reset
            self.assertIn("exhausted", health["crash_reason"].lower())

    def test_live_backend_reports_pid_and_health(self):
        class _Live:
            pid = 4242

            def poll(self):
                return None

        with tempfile.TemporaryDirectory() as td:
            profile = _profile()
            manager = RuntimeManager(AgentConfig(profiles_onboarding_gate=False, models=[profile]),
                                     base_dir=Path(td))
            status = manager._status[profile.id]
            status.managed = True
            status.state = "running"
            status.healthy = True
            manager._managed[profile.id] = _ManagedProcess(
                profile, _Live(), "http://127.0.0.1:9999/v1",
                io.StringIO(), status)
            health = manager.backend_health(profile.id)
            self.assertEqual(health["pid"], 4242)
            self.assertTrue(health["healthy"])
            self.assertEqual(health["port"], 9999)


class CrashHistoryTests(unittest.TestCase):
    def test_record_failure_persists_to_jsonl_and_reads_back(self):
        with tempfile.TemporaryDirectory() as td:
            netdiag.configure_history(Path(td) / "crash.jsonl")
            try:
                entry = netdiag.record_failure(BackendConnectionError(
                    _win_reset(), subsystem="llm",
                    url="http://127.0.0.1:8081/v1/chat/completions",
                    model_id="coder", request_id="req1"))
                netdiag.annotate_recovery(entry, "succeeded on retry 1")
                rows = netdiag.crash_history()
                self.assertEqual(len(rows), 2)
                self.assertEqual(rows[0]["request_id"], "req1")
                self.assertEqual(rows[0]["kind"], "connection_reset")
                self.assertEqual(rows[1]["recovery_for"], "req1")
                self.assertEqual(rows[1]["recovery"], "succeeded on retry 1")
            finally:
                netdiag.configure_history(None)
                netdiag._HISTORY = None

    def test_crash_history_empty_without_config(self):
        old = netdiag._HISTORY
        netdiag._HISTORY = None
        try:
            self.assertEqual(netdiag.crash_history(), [])
        finally:
            netdiag._HISTORY = old


class DiagnosticsEndpointTests(unittest.TestCase):
    def test_diagnostics_endpoint_reports_failures_and_health(self):
        import threading
        from localcodeagent.server import create_server, stop_state

        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / "web").mkdir()
            cfg = AgentConfig(profiles_onboarding_gate=False, 
                models=[ModelProfile(
                    id="fake", endpoint="http://127.0.0.1:9/v1",
                    model="fake-model", roles=["primary_coder"],
                    runtime="external")],
                process_watchdog=False, research_enabled=False,
            )
            server, state = create_server(
                cfg, ws, "127.0.0.1", 0, ws / "web", ws / ".runtime")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                netdiag._FAILURES.clear()
                netdiag.record_failure(BackendConnectionError(
                    _win_reset(), subsystem="llm",
                    url="http://127.0.0.1:9/v1/chat/completions",
                    model_id="fake", streaming=True))
                base = f"http://127.0.0.1:{server.server_address[1]}"
                with urllib.request.urlopen(
                        f"{base}/api/diagnostics", timeout=10) as r:
                    data = json.loads(r.read())
                from localcodeagent.version import version as _ver
                self.assertEqual(data["version"], _ver())
                self.assertEqual(data["models"][0]["id"], "fake")
                self.assertTrue(data["recent_failures"])
                self.assertEqual(data["recent_failures"][-1]["kind"],
                                 "connection_reset")
                self.assertIn("crash_history", data)
                self.assertIsInstance(data["crash_history"], list)
                self.assertIn("hardware", data)
            finally:
                server.shutdown()
                server.server_close()
                stop_state(state)

    def test_watchdog_restart_events_reach_health_and_failures(self):
        """Watchdog auto-restarts must land in health state + the failure
        ring — an unattended overnight recovery must not be invisible."""
        import threading
        from localcodeagent.server import create_server, stop_state

        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / "web").mkdir()
            cfg = AgentConfig(profiles_onboarding_gate=False, 
                models=[ModelProfile(
                    id="fake", endpoint="http://127.0.0.1:9/v1",
                    model="fake-model", roles=["primary_coder"],
                    runtime="external")],
                process_watchdog=False, research_enabled=False,
            )
            server, state = create_server(
                cfg, ws, "127.0.0.1", 0, ws / "web", ws / ".runtime")
            try:
                seen = []
                netdiag._FAILURES.clear()
                state._on_process_event(
                    {"service": "llama:fake", "event": "auto_restart",
                     "attempt": 1, "state": "error"},
                    seen.append)
                self.assertEqual(len(seen), 1)  # still forwarded to the bus
                comp = state.health.components["llm-runtime"]
                self.assertEqual(comp.state, "restarting")
                kinds = [e.get("kind") for e in netdiag._FAILURES]
                self.assertIn("watchdog_restart", kinds)
                state._on_process_event(
                    {"service": "llama:fake", "event": "auto_restart_failed",
                     "error": "spawn failed"},
                    lambda p: None)
                self.assertEqual(comp.state, "crashed")
                kinds = [e.get("kind") for e in netdiag._FAILURES]
                self.assertIn("watchdog_restart_failed", kinds)
            finally:
                server.server_close()
                stop_state(state)


class RealSocketResetTests(unittest.TestCase):
    """Reproduce an actual OS-level TCP reset on loopback — SO_LINGER(0)
    close produces a genuine WinError 10054 on Windows / ECONNRESET on
    POSIX, not a synthetic exception."""

    def _rst_server(self):
        import struct
        import threading

        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(4)
        port = srv.getsockname()[1]
        stop = threading.Event()

        def loop():
            srv.settimeout(0.5)
            while not stop.is_set():
                try:
                    conn, _ = srv.accept()
                except socket.timeout:
                    continue
                except OSError:
                    return
                # Abortive close: unread request bytes + RST.
                conn.setsockopt(
                    socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                conn.close()

        t = threading.Thread(target=loop, daemon=True)
        t.start()
        return srv, port, stop, t

    def test_real_tcp_reset_surfaces_as_connection_reset(self):
        import http.client as hc
        srv, port, stop, t = self._rst_server()
        try:
            with self.assertRaises((ConnectionResetError, ConnectionAbortedError,
                                    BrokenPipeError, hc.RemoteDisconnected,
                                    OSError)) as ctx:
                conn = hc.HTTPConnection("127.0.0.1", port, timeout=5)
                try:
                    conn.request("POST", "/v1/chat/completions", body="{}", headers={"Content-Type": "application/json"})
                    conn.getresponse().read()
                finally:
                    conn.close()
            # Whatever the OS reported must classify as a transport failure.
            self.assertTrue(
                is_transport_failure(ctx.exception) or
                classify_transport_error(ctx.exception) is not None or
                isinstance(ctx.exception, hc.RemoteDisconnected),
                f"unexpected classification for {ctx.exception!r}")
        finally:
            stop.set()
            srv.close()
            t.join(timeout=2)

    def test_real_reset_through_provider_is_classified_backend_error(self):
        srv, port, stop, t = self._rst_server()
        try:
            provider = OpenAICompatibleProvider(
                _profile(endpoint=f"http://127.0.0.1:{port}/v1"))
            with self.assertRaises(BackendConnectionError) as ctx:
                provider.complete(messages=[{"role": "user", "content": "hi"}])
            exc = ctx.exception
            self.assertIn(exc.kind, ("connection_reset", "connection_aborted",
                                     "incomplete_read", "eof", "http_error",
                                     "timeout", "connection_refused"))
            self.assertEqual(exc.host, "127.0.0.1")
            self.assertEqual(exc.port, port)
            self.assertFalse(exc.delivered_output)
        finally:
            stop.set()
            srv.close()
            t.join(timeout=2)


class PortCollisionTests(unittest.TestCase):
    """A stale dev backend squatting on the API port must not kill the
    server with a raw bind traceback — serve() identifies the owner and
    picks a free port."""

    def _cfg(self) -> AgentConfig:
        return AgentConfig(profiles_onboarding_gate=False, 
            models=[ModelProfile(
                id="fake", endpoint="http://127.0.0.1:9/v1",
                model="fake-model", roles=["primary_coder"],
                runtime="external")],
            process_watchdog=False, research_enabled=False)

    def test_create_server_cleans_up_state_on_bind_failure(self):
        import threading
        from localcodeagent.server import create_server

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        held_port = sock.getsockname()[1]
        threads_before = threading.active_count()
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / "web").mkdir()
            with self.assertRaises(OSError):
                create_server(self._cfg(), ws, "127.0.0.1", held_port,
                              ws / "web", ws / ".runtime")
            # The orphaned AppState must have been shut down — no leaked
            # worker threads from the failed attempt. Daemon stragglers
            # can take a beat to observe _shutdown, so settle briefly and
            # name whatever is still alive for the failure message.
            for _ in range(40):
                if threading.active_count() <= threads_before + 1:
                    break
                time.sleep(0.1)
            self.assertLessEqual(
                threading.active_count(), threads_before + 1,
                "leaked threads: " + ", ".join(
                    t.name for t in threading.enumerate()))
        sock.close()

    def test_serve_recovers_on_held_port(self):
        import threading
        from localcodeagent.server import serve

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        held_port = sock.getsockname()[1]

        # serve() emits the real bound port via boot.port_report so the
        # desktop host can health-check it; capture the call directly —
        # stdout redirection races with other tests' serve() threads.
        reported: list[int] = []
        import localcodeagent.boot as boot_mod
        original = boot_mod.port_report
        boot_mod.port_report = lambda p: (reported.append(p), original(p))
        stop = threading.Event()
        t: threading.Thread | None = None
        try:
            with _RetriedTemporaryDirectory(ignore_cleanup_errors=True) as td:
                ws = Path(td)
                (ws / "web").mkdir()
                t = threading.Thread(
                    target=serve,
                    args=(self._cfg(), ws, "127.0.0.1", held_port,
                          Path(__file__).resolve().parents[1] / "web",
                          ws / ".runtime"),
                    kwargs={"config_path": None,
                            "shutdown_event": stop},
                    daemon=True)
                t.start()
                # AppState init can take several seconds under parallel
                # test load; poll until the marker lands or the thread dies.
                deadline = time.time() + 25
                while not reported and t.is_alive() and time.time() < deadline:
                    time.sleep(0.1)
                self.assertTrue(t.is_alive(), "serve() must not die on bind collision")
                self.assertTrue(reported, "serve() must report its bound port")
                self.assertNotEqual(reported[-1], held_port,
                                    "reported port must be the fallback, not the held port")
                # Autonomy GET handlers must not NameError on query parsing
                # (regression: /api/findings referenced an undefined `q`).
                import urllib.request
                for ep, key in (("/api/findings", "findings"),
                                ("/api/procedures", "procedures")):
                    with urllib.request.urlopen(
                            f"http://127.0.0.1:{reported[-1]}{ep}",
                            timeout=10) as resp:
                        self.assertIn(key, json.loads(resp.read()), ep)
        finally:
            stop.set()
            if t is not None:
                t.join(timeout=45)
            boot_mod.port_report = original
        sock.close()


if __name__ == "__main__":
    unittest.main()
