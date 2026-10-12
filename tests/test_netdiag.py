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

    def test_control_lanes_claim_under_pinned_modes(self):
        # Live: 'turn on the pay rent reminder' sent with mode='chat'
        # (and equivalently any role pin like 'primary_coder') skipped
        # every deterministic lane — they're all gated mode=="auto" —
        # so the model narrated 'Turned on the reminder "pay rent".'
        # and 'Reminder "pay rent" is already active.' over a row that
        # stayed disabled. Scheduler control turns must claim under
        # every user-interactive mode; a role pin chooses WHICH model
        # answers what the lanes don't, never whether lanes exist.
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
                self.rows = [r for r in self.rows
                             if r.get("id") != sid]
                return len(self.rows) != n

            def set_enabled(self, sid, en):
                for r in self.rows:
                    if r["id"] == sid:
                        r["enabled"] = en
                        return True
                return False

            def update(self, sid, **kw):
                for r in self.rows:
                    if r["id"] == sid:
                        r.update(kw)
                        return r

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
            sup.scheduler.add(
                "Reminder: pay rent", "once",
                at=time.time() + 86400,
                action={"kind": "notify", "detail": "pay rent"})
            # Any provider build means the model was asked — control
            # turns must never reach it.
            def _no_provider(*_a, **_kw):
                raise AssertionError("model invoked on a reminder "
                                     "control turn")
            agent._provider_for = _no_provider
            for mode in ("chat", "primary_coder", "auto"):
                sup.scheduler.rows[0]["enabled"] = False
                r = agent.run("turn on the pay rent reminder",
                              mode=mode)
                self.assertIn("Turned on", r.content)
                self.assertTrue(sup.scheduler.rows[0]["enabled"])
            # Internal modes stay out of user-facing lanes.
            for mode in ("work_order", "autonomy", "self_repair"):
                agent._provider_for = None
                sup.scheduler.rows[0]["enabled"] = True
                try:
                    agent.run("turn on the pay rent reminder",
                              mode=mode)
                except Exception:
                    pass  # no model wired — just assert no mutation
                self.assertTrue(sup.scheduler.rows[0]["enabled"])

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
            # 'tonight at 8' — an anchor word + clock is one spec;
            # before this 'tonight' split off into the task slot
            # and the leftover 'at 8' tripped the compound reply.
            spec = parse_reminder_schedule(
                "tonight at 8", time.time())
            self.assertIsNotNone(spec)
            self.assertEqual(time.localtime(spec["at"]).tm_hour, 20)
            # The daypart word supplies the meridiem — 'morning'
            # must survive the bare-hour PM convention, and an
            # evening hour after noon stays PM.
            spec = parse_reminder_schedule(
                "this morning at 6", time.time())
            self.assertEqual(time.localtime(spec["at"]).tm_hour, 6)
            spec = parse_reminder_schedule(
                "tomorrow morning at 6", time.time())
            self.assertEqual(
                (time.localtime(spec["at"]).tm_hour,
                 time.localtime(spec["at"]).tm_wday),
                (6, (time.localtime().tm_wday + 1) % 7))
            spec = parse_reminder_schedule(
                "tomorrow evening at 11", time.time())
            self.assertEqual(time.localtime(spec["at"]).tm_hour, 23)

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

            # Temporal scope — 'for today'/'this weekend' filter by
            # fire date, never match the words as topic ('no reminder
            # about "today"' over a due row was nonsense live).
            sup.scheduler.add(
                "reminder: lunch", "once", at=time.time() + 600,
                action={"kind": "notify", "detail": "lunch"})
            inv = agent._reminder_inventory_reply(
                "are there reminders for today",
                "are there reminders for today")
            self.assertIsNotNone(inv)
            self.assertIn("lunch", inv.text)
            self.assertNotIn("timesheets", inv.text)
            inv = agent._reminder_inventory_reply(
                "any reminders for tomorrow",
                "any reminders for tomorrow")
            self.assertIsNotNone(inv)
            self.assertIn("check the build", inv.text)
            self.assertNotIn("lunch", inv.text)
            # 'upcoming'/'scheduled' describe the pool itself —
            # they scope nothing instead of missing as a topic.
            inv = agent._reminder_inventory_reply(
                "show me upcoming reminders",
                "show me upcoming reminders")
            self.assertIsNotNone(inv)
            self.assertIn("check the build", inv.text)
            # The whole inventory surface is model-optional — the
            # lane claims these, so the readiness gate must too.
            from localcodeagent.agent.orchestrator import (
                AgentOrchestrator)
            for q in ("show me upcoming reminders",
                      "are there reminders for today",
                      "list everything scheduled",
                      "any alarms tomorrow",
                      "am i reminded of anything",
                      "upcoming events",
                      "has my reminder fired yet"):
                self.assertTrue(
                    AgentOrchestrator.can_run_without_coding_model(q),
                    q)

            # State-filtered inventory includes the disabled pool —
            # 'which reminders are off' must not hide off rows.
            off_row = next(
                s for s in sup.scheduler.rows
                if "lunch" in s["name"])
            off_row["enabled"] = False
            inv = agent._reminder_inventory_reply(
                "which reminders are turned off",
                "which reminders are turned off")
            self.assertIsNotNone(inv)
            self.assertIn("lunch", inv.text)
            self.assertNotIn("timesheets", inv.text)
            inv = agent._reminder_inventory_reply(
                "any disabled reminders",
                "any disabled reminders")
            self.assertIsNotNone(inv)
            self.assertIn("lunch", inv.text)
            # 'did i disable X' answers from the row's enabled flag.
            inv = agent._reminder_inventory_reply(
                "did i disable the lunch reminder",
                "did i disable the lunch reminder")
            self.assertIsNotNone(inv)
            self.assertIn("off", inv.text)
            # Noun-less state asks — 'is the lunch off' answers
            # the row's enabled flag; 'is the server off' matches
            # no row and falls through to its real owner.
            r = agent._reminder_reply(
                "is the lunch off", "is the lunch off", task.id)
            self.assertIsNotNone(r)
            self.assertIn("off", r.content)
            self.assertIsNone(
                agent._reminder_reply(
                    "is the server off", "is the server off",
                    task.id))
            off_row["enabled"] = True
            # Cadence asks — 'how often does X fire', 'is X daily
            # or weekly', 'what day does X hit' — answer from the
            # row's kind/hour fields, never model invention.
            inv = agent._reminder_inventory_reply(
                "how often does the build reminder fire",
                "how often does the build reminder fire")
            self.assertIsNotNone(inv)
            self.assertIn("check the build", inv.text)
            inv = agent._reminder_inventory_reply(
                "is the build reminder daily or weekly",
                "is the build reminder daily or weekly")
            self.assertIsNotNone(inv)
            self.assertIn("check the build", inv.text)
            inv = agent._reminder_inventory_reply(
                "what day does the build reminder hit",
                "what day does the build reminder hit")
            self.assertIsNotNone(inv)
            self.assertIn("check the build", inv.text)
            inv = agent._reminder_inventory_reply(
                "when does the timesheets go off",
                "when does the timesheets go off")
            self.assertIsNotNone(inv)
            self.assertIn("timesheets", inv.text)
            for q in ("how often does the standup fire",
                      "is the standup daily or weekly",
                      "what day does the rent reminder hit",
                      "when does the standup go off"):
                self.assertTrue(
                    AgentOrchestrator.can_run_without_coding_model(q),
                    q)
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

    def test_reminder_cancel_conjuncts_and_noun_labels(self):
        # 'cancel the standup and the dentist' — 'and' joins two
        # scopes; each named side is an explicit target and cancels,
        # while a row literally named 'lock and load' is one row,
        # not two. 'cancel the alarm' must hit rows named
        # 'reminder: alarm …' — the noun was a startswith filter on
        # the raw name, so reminder-kind alarm rows were invisible.
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
                self.rows = [r for r in self.rows
                             if r["id"] != sid]
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
                return len(self.rows) < n

        class _Sup:
            def __init__(self):
                self.scheduler = _Sched()

        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(Path(td), _FakeRuntime())
            sup = _Sup()
            agent._autonomy = lambda: sup
            task = agent.tasks.create("x", "auto")
            self._run_cancel_checks(agent, sup, task)

    def _run_cancel_checks(self, agent, sup, task):
        for name in ("reminder: standup", "reminder: the dentist",
                     "reminder: lock and load", "reminder: alarm a",
                     "reminder: alarm b"):
            sup.scheduler.add(
                name, "once", at=time.time() + 3600,
                action={"kind": "notify", "detail": name})
        r = agent._reminder_reply(
            "cancel the standup and the dentist",
            "cancel the standup and the dentist", task.id)
        self.assertIsNotNone(r)
        self.assertIn("2", r.content)
        self.assertEqual(
            [s["name"] for s in sup.scheduler.rows],
            ["reminder: lock and load", "reminder: alarm a",
             "reminder: alarm b"])
        # A stored title containing 'and' is one row, not a join.
        r = agent._reminder_reply(
            "cancel the lock and load",
            "cancel the lock and load", task.id)
        self.assertIsNotNone(r)
        self.assertIn("lock and load", r.content)
        self.assertEqual(
            [s["name"] for s in sup.scheduler.rows],
            ["reminder: alarm a", "reminder: alarm b"])
        # Bare noun objects scope by label — 'cancel the alarm'
        # names the two alarm rows, not 'no alarms set'.
        r = agent._reminder_reply(
            "cancel the alarm", "cancel the alarm", task.id)
        self.assertIsNotNone(r)
        self.assertIn("matches 2 reminders", r.content)
        self.assertIn("alarm a", r.content)
        self.assertEqual(len(sup.scheduler.rows), 2)
        r = agent._reminder_reply(
            "cancel my alarms", "cancel my alarms", task.id)
        self.assertIsNotNone(r)
        self.assertFalse(sup.scheduler.rows)

        # 'rename X to Y' mutates the row's name + detail — never
        # a narrated label over an untouched row.
        sup.scheduler.add(
            "reminder: standup", "once", at=time.time() + 3600,
            action={"kind": "notify", "detail": "standup"})
        r = agent._reminder_reply(
            "rename the standup to scrum",
            "rename the standup to scrum", task.id)
        self.assertIsNotNone(r)
        self.assertIn("scrum", r.content)
        self.assertEqual(sup.scheduler.rows[0]["name"],
                         "reminder: scrum")
        self.assertEqual(
            sup.scheduler.rows[0]["action"]["detail"], "scrum")
        # 'put X on hold' is the pause idiom noun-less — a real
        # off flip on the matched row.
        r = agent._reminder_reply(
            "put the scrum on hold",
            "put the scrum on hold", task.id)
        self.assertIsNotNone(r)
        self.assertFalse(sup.scheduler.rows[0]["enabled"])
        # Cadence words stranded in a task slot clarify instead of
        # minting a row named 'once a week'.
        sup.scheduler.rows.clear()
        r = agent._reminder_reply(
            "remind me once a week on saturdays",
            "remind me once a week on saturdays", task.id)
        self.assertIsNotNone(r)
        self.assertIn("do what", r.content)
        self.assertFalse(sup.scheduler.rows)
        # 'weekly on saturdays' — the connector tail carries the
        # real schedule past the redundant cadence word.
        r = agent._reminder_reply(
            "remind me weekly on saturdays to meal prep",
            "remind me weekly on saturdays to meal prep", task.id)
        self.assertIsNotNone(r)
        self.assertIn("Saturday", r.content)
        self.assertIn("meal prep", r.content)
        self.assertEqual(len(sup.scheduler.rows), 1)

        # Positional ordinals — 'the second one' picks by fire
        # order; an out-of-range index says which exist, never
        # 'already off'/'cancelled 0' over live rows.
        sup.scheduler.rows.clear()
        sup.scheduler.add(
            "reminder: lunch", "once", at=time.time() + 3600,
            action={"kind": "notify", "detail": "lunch"})
        sup.scheduler.add(
            "reminder: dinner", "once", at=time.time() + 7200,
            action={"kind": "notify", "detail": "dinner"})
        r = agent._reminder_reply(
            "mute the third one", "mute the third one", task.id)
        self.assertIsNotNone(r)
        self.assertIn("no third", r.content)
        self.assertTrue(
            all(s["enabled"] for s in sup.scheduler.rows))
        r = agent._reminder_reply(
            "cancel the fourth reminder",
            "cancel the fourth reminder", task.id)
        self.assertIsNotNone(r)
        self.assertIn("no fourth", r.content)
        self.assertEqual(len(sup.scheduler.rows), 2)
        r = agent._reminder_reply(
            "cancel the second reminder",
            "cancel the second reminder", task.id)
        self.assertIsNotNone(r)
        self.assertIn("dinner", r.content)
        self.assertEqual(
            [s["name"] for s in sup.scheduler.rows],
            ["reminder: lunch"])
        # 'what time is X' — a bare copula fire-time ask over a
        # named row; 'when does the train come' isn't ours.
        r = agent._reminder_reply(
            "what time is my lunch",
            "what time is my lunch", task.id)
        self.assertIsNotNone(r)
        self.assertIn("lunch", r.content)
        self.assertIsNone(
            agent._reminder_reply(
                "when does the train come",
                "when does the train come", task.id))

        # 'from X to Y' — the 'from' clock scopes WHICH row (and
        # hands a bare-digit target its meridian); the 'to' clock
        # is the real target. The un-split '6 to 7' parsed as a
        # stray delay and recur_merge re-claimed the old clock.
        sup.scheduler.rows.clear()
        sup.scheduler.add(
            "alarm: morning call", "daily", hour=6, minute=0,
            action={"kind": "notify", "detail": "morning call"})
        sup.scheduler.add(
            "alarm: evening check", "daily", hour=18, minute=0,
            action={"kind": "notify", "detail": "evening check"})
        r = agent._reminder_reply(
            "change my alarm from 6am to 7",
            "change my alarm from 6am to 7", task.id)
        self.assertIsNotNone(r)
        self.assertIn("morning call", r.content)
        self.assertIn("07:00", r.content)
        self.assertEqual(
            (sup.scheduler.rows[0]["hour"],
             sup.scheduler.rows[0]["minute"]), (7, 0))
        self.assertEqual(sup.scheduler.rows[0]["kind"], "daily")
        r = agent._reminder_reply(
            "change my alarm from 6pm to 8",
            "change my alarm from 6pm to 8", task.id)
        self.assertIsNotNone(r)
        self.assertIn("evening check", r.content)
        self.assertIn("20:00", r.content)
        self.assertEqual(
            (sup.scheduler.rows[1]["hour"],
             sup.scheduler.rows[1]["minute"]), (20, 0))
        # A 'from' clock naming no row misses honestly — never a
        # fabricated 'Rescheduled' over nothing.
        r = agent._reminder_reply(
            "change my alarm from 9 to 10",
            "change my alarm from 9 to 10", task.id)
        self.assertIsNotNone(r)
        self.assertIn("don't see", r.content)
        self.assertEqual(
            [s["hour"] for s in sup.scheduler.rows], [7, 20])
        # A body without a schedule noun still falls through.
        self.assertIsNone(
            agent._reminder_reply(
                "change the font",
                "change the font", task.id))
        # 'appointment' is filler next to a real subject — the
        # dentist row resolves by 'dentist' alone.
        sup.scheduler.add(
            "reminder: dentist", "once", at=time.time() + 7200,
            action={"kind": "notify", "detail": "dentist"})
        r = agent._reminder_reply(
            "reschedule my dentist appointment to friday",
            "reschedule my dentist appointment to friday",
            task.id)
        self.assertIsNotNone(r)
        self.assertIn("dentist", r.content)
        self.assertEqual(sup.scheduler.rows[2]["kind"], "once")
        # A row literally named 'appointment' keeps its term.
        sup.scheduler.add(
            "reminder: appointment", "once",
            at=time.time() + 3600,
            action={"kind": "notify", "detail": "appointment"})
        r = agent._reminder_reply(
            "cancel the appointment",
            "cancel the appointment", task.id)
        self.assertIsNotNone(r)
        self.assertIn("appointment", r.content)
        self.assertNotIn(
            "reminder: appointment",
            [s["name"] for s in sup.scheduler.rows])
        # 'skip' can't drop one fire of a recurring row — honest
        # ops offered instead of deleting the cadence.
        sup.scheduler.add(
            "reminder: standup", "daily", hour=9, minute=0,
            action={"kind": "notify", "detail": "standup"})
        n_rows = len(sup.scheduler.rows)
        r = agent._reminder_reply(
            "skip the standup today",
            "skip the standup today", task.id)
        self.assertIsNotNone(r)
        self.assertIn("can't skip", r.content)
        self.assertEqual(len(sup.scheduler.rows), n_rows)
        # 'skip' on a one-shot is a normal cancel — it fires once.
        r = agent._reminder_reply(
            "skip the dentist",
            "skip the dentist", task.id)
        self.assertIsNotNone(r)
        self.assertIn("dentist", r.content)
        self.assertNotIn(
            "reminder: dentist",
            [s["name"] for s in sup.scheduler.rows])

        # Amount-leading shifts — 'move X an hour later' carries the
        # amount before the direction; it retimes the row instead of
        # clarifying 'to when?'. An unquantified direction alone
        # ('move the table back') still falls through.
        sup.scheduler.rows.clear()
        sup.scheduler.add(
            "reminder: standup", "daily", hour=9, minute=0,
            action={"kind": "notify", "detail": "standup"})
        r = agent._reminder_reply(
            "move the standup 30 minutes earlier",
            "move the standup 30 minutes earlier", task.id)
        self.assertIsNotNone(r)
        self.assertIn("08:30", r.content)
        self.assertEqual(
            (sup.scheduler.rows[0]["hour"],
             sup.scheduler.rows[0]["minute"]), (8, 30))
        self.assertEqual(sup.scheduler.rows[0]["kind"], "daily")
        at0 = time.time() + 3600
        sup.scheduler.add(
            "reminder: dentist", "once", at=at0,
            action={"kind": "notify", "detail": "dentist"})
        r = agent._reminder_reply(
            "move the dentist an hour later",
            "move the dentist an hour later", task.id)
        self.assertIsNotNone(r)
        self.assertAlmostEqual(
            float(sup.scheduler.rows[1]["at"]), at0 + 3600,
            delta=30)
        # A quantified shift over a non-row misses honestly — the
        # model never gets to narrate a fake move.
        r = agent._reminder_reply(
            "move the table an hour later",
            "move the table an hour later", task.id)
        self.assertIsNotNone(r)
        self.assertIn("don't see", r.content)
        self.assertIsNone(
            agent._reminder_reply(
                "move the table back",
                "move the table back", task.id))
        # Count ordinals — 'the next two' is an explicit target
        # set, cancelled without a redundant 'say which'.
        r = agent._reminder_reply(
            "cancel the next two reminders",
            "cancel the next two reminders", task.id)
        self.assertIsNotNone(r)
        self.assertIn("2", r.content)
        self.assertFalse(sup.scheduler.rows)

        # 'quarter to 7' — the lazy 'to' split inside the clock;
        # rejoined it's a when-only clause owned by the NOTASK
        # path. Wake verbs shift the PM inference to AM — a wake
        # call is a morning ask, not dinner.
        r = agent._reminder_reply(
            "wake me at quarter to 7",
            "wake me at quarter to 7", task.id)
        self.assertIsNotNone(r)
        self.assertIn("06:45", r.content)
        self.assertIn("wake", r.content)
        self.assertEqual(len(sup.scheduler.rows), 1)
        r = agent._reminder_reply(
            "wake me at half past 6",
            "wake me at half past 6", task.id)
        self.assertIsNotNone(r)
        self.assertIn("06:30", r.content)
        # 'in an hour' is a duration, not a clock — no AM shift.
        r = agent._reminder_reply(
            "wake me up in an hour",
            "wake me up in an hour", task.id)
        self.assertIsNotNone(r)
        self.assertAlmostEqual(
            float(sup.scheduler.rows[-1]["at"]),
            time.time() + 3600, delta=120)
        # Explicit meridian beats the wake-AM convention.
        r = agent._reminder_reply(
            "wake me at 6pm",
            "wake me at 6pm", task.id)
        self.assertIsNotNone(r)
        self.assertIn("18:00", r.content)

        # Clock-confirm asks — 'is my alarm set for 6' answers from
        # the row's real clock: bare digits are meridian-free
        # (06:00 and 18:00 both confirm), 'am'/'pm'/'>12' pin h24.
        sup.scheduler.rows.clear()
        sup.scheduler.add(
            "alarm: wake", "daily", hour=6, minute=0,
            action={"kind": "notify", "detail": "wake"})
        sup.scheduler.add(
            "alarm: dinner bell", "daily", hour=18, minute=0,
            action={"kind": "notify", "detail": "dinner bell"})
        r = agent._reminder_reply(
            "is my alarm set for 6",
            "is my alarm set for 6", task.id)
        self.assertIsNotNone(r)
        self.assertIn("Yes", r.content)
        self.assertIn("wake", r.content)
        self.assertIn("dinner bell", r.content)
        r = agent._reminder_reply(
            "is my alarm set for 6am",
            "is my alarm set for 6am", task.id)
        self.assertIsNotNone(r)
        self.assertIn("wake", r.content)
        self.assertNotIn("dinner bell", r.content)
        r = agent._reminder_reply(
            "is my alarm set for 6pm",
            "is my alarm set for 6pm", task.id)
        self.assertIsNotNone(r)
        self.assertIn("dinner bell", r.content)
        self.assertNotIn("wake", r.content)
        r = agent._reminder_reply(
            "is the wake set for 21",
            "is the wake set for 21", task.id)
        self.assertIsNotNone(r)
        self.assertIn("No", r.content)
        self.assertIn("06:00", r.content)
        r = agent._reminder_reply(
            "is the dentist set for 3",
            "is the dentist set for 3", task.id)
        self.assertIsNotNone(r)
        self.assertIn("No", r.content)
        self.assertEqual(len(sup.scheduler.rows), 2)

        # Creation-order ordinals — 'newest'/'oldest' sort by
        # created_at, distinct from fire-order 'first'/'last';
        # 'the reminder i just made' is the same newest-created
        # selector through the relative clause.
        sup.scheduler.rows.clear()
        base = time.time()
        for name, at, created in (
                ("reminder: alpha", base + 3600, base - 1000),
                ("reminder: beta", base + 7200, base - 500),
                ("reminder: gamma", base + 10800, base - 100)):
            sup.scheduler.add(
                name, "once", at=at, created_at=created,
                action={"kind": "notify", "detail": name})
        r = agent._reminder_reply(
            "delete my newest reminder",
            "delete my newest reminder", task.id)
        self.assertIsNotNone(r)
        self.assertIn("gamma", r.content)
        self.assertEqual(
            [s["name"] for s in sup.scheduler.rows],
            ["reminder: alpha", "reminder: beta"])
        r = agent._reminder_reply(
            "cancel the reminder i just made",
            "cancel the reminder i just made", task.id)
        self.assertIsNotNone(r)
        self.assertIn("beta", r.content)
        r = agent._reminder_reply(
            "delete my oldest reminder",
            "delete my oldest reminder", task.id)
        self.assertIsNotNone(r)
        self.assertIn("alpha", r.content)
        self.assertFalse(sup.scheduler.rows)

        # Clock-scoped existence asks — 'do i have a 6am' names
        # the matching row; a miss lists what's really there.
        # A non-clock object ('a cat') still falls through.
        sup.scheduler.add(
            "reminder: wake", "daily", hour=6, minute=0,
            action={"kind": "notify", "detail": "wake"})
        r = agent._reminder_reply(
            "do i have a 6am", "do i have a 6am", task.id)
        self.assertIsNotNone(r)
        self.assertIn("wake", r.content)
        r = agent._reminder_reply(
            "do i have a 6pm", "do i have a 6pm", task.id)
        self.assertIsNotNone(r)
        self.assertIn("No reminder", r.content)
        self.assertIn("wake", r.content)
        self.assertIsNone(agent._reminder_reply(
            "do i have a cat", "do i have a cat", task.id))

        # Mass shifts — 'push everything back'/'delay all by'
        # retime once-rows AND recurring clocks, with a real count.
        sup.scheduler.rows.clear()
        at0 = time.time() + 3600
        sup.scheduler.add(
            "reminder: once-row", "once", at=at0,
            action={"kind": "notify", "detail": "once-row"})
        sup.scheduler.add(
            "reminder: daily-row", "daily", hour=9, minute=0,
            action={"kind": "notify", "detail": "daily-row"})
        r = agent._reminder_reply(
            "push everything back an hour",
            "push everything back an hour", task.id)
        self.assertIsNotNone(r)
        self.assertIn("2", r.content)
        self.assertAlmostEqual(
            float(sup.scheduler.rows[0]["at"]), at0 + 3600,
            delta=30)
        self.assertEqual(sup.scheduler.rows[1]["hour"], 10)
        r = agent._reminder_reply(
            "delay all reminders by 30 minutes",
            "delay all reminders by 30 minutes", task.id)
        self.assertIsNotNone(r)
        self.assertIn("2", r.content)
        self.assertAlmostEqual(
            float(sup.scheduler.rows[0]["at"]), at0 + 5400,
            delta=30)
        self.assertEqual(
            (sup.scheduler.rows[1]["hour"],
             sup.scheduler.rows[1]["minute"]), (10, 30))

        # 'undo that' — the journaled inverses replay against the
        # real rows: removes come back, toggles flip back, updates
        # restore. An empty journal declines so a non-reminder
        # 'undo' still reaches other owners; read-only turns don't
        # evict the journal.
        sup.scheduler.rows.clear()
        sup.scheduler.add(
            "reminder: wake", "daily", hour=6, minute=0,
            action={"kind": "notify", "detail": "wake"})
        # Earlier checks mutated rows — drain that journal so this
        # assert exercises the truly-empty decline.
        agent._sched_undo = []
        self.assertIsNone(agent._reminder_reply(
            "undo that", "undo that", task.id))
        r = agent._reminder_reply(
            "cancel the wake", "cancel the wake", task.id)
        self.assertIsNotNone(r)
        self.assertFalse(sup.scheduler.rows)
        # A read-only turn in between keeps the journal live.
        r = agent._reminder_reply(
            "what reminders do i have",
            "what reminders do i have", task.id)
        self.assertIsNotNone(r)
        r = agent._reminder_reply(
            "undo that", "undo that", task.id)
        self.assertIsNotNone(r)
        self.assertIn("wake", r.content)
        self.assertEqual(len(sup.scheduler.rows), 1)
        r = agent._reminder_reply(
            "turn off the wake", "turn off the wake", task.id)
        self.assertIsNotNone(r)
        self.assertFalse(sup.scheduler.rows[0]["enabled"])
        r = agent._reminder_reply(
            "undo that", "undo that", task.id)
        self.assertIsNotNone(r)
        self.assertTrue(sup.scheduler.rows[0]["enabled"])
        r = agent._reminder_reply(
            "remind me in 5 minutes to stretch",
            "remind me in 5 minutes to stretch", task.id)
        self.assertIsNotNone(r)
        self.assertEqual(len(sup.scheduler.rows), 2)
        r = agent._reminder_reply(
            "undo that", "undo that", task.id)
        self.assertIsNotNone(r)
        self.assertIn("stretch", r.content)
        self.assertEqual(len(sup.scheduler.rows), 1)
        # Journal consumed — a second undo declines.
        self.assertIsNone(agent._reminder_reply(
            "undo that", "undo that", task.id))

        # 'bring back X' — a disabled row re-enables through the
        # toggle convention, a journaled deletion re-adds the row,
        # an enabled row says 'already on', and a scope that
        # resolves nothing misses honestly instead of narrating a
        # resurrection.
        r = agent._reminder_reply(
            "turn off the wake", "turn off the wake", task.id)
        self.assertIsNotNone(r)
        r = agent._reminder_reply(
            "bring back the wake", "bring back the wake", task.id)
        self.assertIsNotNone(r)
        self.assertIn("Turned on", r.content)
        self.assertTrue(sup.scheduler.rows[0]["enabled"])
        r = agent._reminder_reply(
            "cancel the wake", "cancel the wake", task.id)
        self.assertIsNotNone(r)
        self.assertFalse(sup.scheduler.rows)
        r = agent._reminder_reply(
            "bring back the wake", "bring back the wake", task.id)
        self.assertIsNotNone(r)
        self.assertIn("wake", r.content)
        self.assertEqual(len(sup.scheduler.rows), 1)
        r = agent._reminder_reply(
            "restore the wake", "restore the wake", task.id)
        self.assertIsNotNone(r)
        self.assertIn("already on", r.content)
        agent._sched_undo = []
        sup.scheduler.rows.clear()
        sup.scheduler.add(
            "reminder: other", "daily", hour=8, minute=0,
            action={"kind": "notify", "detail": "other"})
        r = agent._reminder_reply(
            "bring back the dentist reminder",
            "bring back the dentist reminder", task.id)
        self.assertIsNotNone(r)
        self.assertIn("don't see", r.content)
        self.assertEqual(len(sup.scheduler.rows), 1)

        # Clock-scoped retime — 'change the 6am alarm to 6:30'
        # keeps the row's meridian; the bare-clock default would
        # flip it to 18:30. A clock-less scope keeps the default.
        sup.scheduler.rows.clear()
        sup.scheduler.add(
            "alarm: wake", "daily", hour=6, minute=0,
            action={"kind": "notify", "detail": "wake"})
        r = agent._reminder_reply(
            "change the 6am alarm to 6:30",
            "change the 6am alarm to 6:30", task.id)
        self.assertIsNotNone(r)
        self.assertIn("06:30", r.content)
        self.assertEqual(
            (sup.scheduler.rows[0]["hour"],
             sup.scheduler.rows[0]["minute"]), (6, 30))

        # Day-scoped inventory includes recurring rows — 'do i
        # have anything tomorrow' lists the daily rows whose next
        # fire lands in the window, not just dated once-rows.
        sup.scheduler.rows.clear()
        sup.scheduler.add(
            "reminder: standup", "daily", hour=9, minute=30,
            action={"kind": "notify", "detail": "standup"})
        r = agent._reminder_reply(
            "do i have anything tomorrow",
            "do i have anything tomorrow", task.id)
        self.assertIsNotNone(r)
        self.assertIn("standup", r.content)
        # 'what's left today' / 'what's on tonight' — noun-less
        # day-scoped remaining asks claim, never model-narrate.
        r = agent._reminder_reply(
            "what's on tonight", "what's on tonight", task.id)
        self.assertIsNotNone(r)
        self.assertIn("reminder", r.content.lower())

        # Provenance clauses — 'turn on the reminder i turned off'
        # selects from the disabled pool rather than treating
        # 'i turned off' as label terms; 'i just made' resolves to
        # the newest-created row; equal/unknown created_at can't
        # rank, so it says 'say which' instead of guessing.
        sup.scheduler.rows.clear()
        sup.scheduler.add(
            "reminder: wake", "daily", hour=6, minute=0,
            enabled=False,
            action={"kind": "notify", "detail": "wake"})
        sup.scheduler.add(
            "reminder: standup", "daily", hour=9, minute=30,
            action={"kind": "notify", "detail": "standup"})
        r = agent._reminder_reply(
            "turn on the reminder i turned off",
            "turn on the reminder i turned off", task.id)
        self.assertIsNotNone(r)
        self.assertIn("wake", r.content)
        self.assertTrue(sup.scheduler.rows[0]["enabled"])
        # created_at absent → newest can't be told apart.
        r = agent._reminder_reply(
            "turn off the reminder i just made",
            "turn off the reminder i just made", task.id)
        self.assertIsNotNone(r)
        self.assertIn("say which", r.content)
        self.assertTrue(all(
            s["enabled"] for s in sup.scheduler.rows))
        # With distinct created_at the newest row flips.
        now0 = time.time()
        sup.scheduler.rows[0]["created_at"] = now0 - 100
        sup.scheduler.rows[1]["created_at"] = now0 - 50
        r = agent._reminder_reply(
            "turn off the reminder i just made",
            "turn off the reminder i just made", task.id)
        self.assertIsNotNone(r)
        self.assertIn("standup", r.content)
        self.assertFalse(sup.scheduler.rows[1]["enabled"])
        self.assertTrue(sup.scheduler.rows[0]["enabled"])

        # 'what did you just do' — reads the live journal without
        # consuming it, so 'undo' still replays; an empty journal
        # declines so generic asks reach other owners.
        agent._sched_undo = []
        self.assertIsNone(agent._reminder_reply(
            "what did you just do", "what did you just do",
            task.id))
        r = agent._reminder_reply(
            "turn on the standup", "turn on the standup", task.id)
        self.assertIsNotNone(r)
        r = agent._reminder_reply(
            "what did you just do", "what did you just do",
            task.id)
        self.assertIsNotNone(r)
        self.assertIn("standup", r.content)
        # Read-only: the journal survives the ask.
        r = agent._reminder_reply(
            "undo my last change", "undo my last change", task.id)
        self.assertIsNotNone(r)
        self.assertFalse(sup.scheduler.rows[1]["enabled"])

        # 'cancel the last reminder i made' — the provenance clause
        # in tail2 is not the scope; 'last' still picks by fire
        # order over the enabled pool.
        sup.scheduler.rows.clear()
        base2 = time.time()
        sup.scheduler.add(
            "reminder: alpha", "once", at=base2 + 3600,
            created_at=base2 - 500,
            action={"kind": "notify", "detail": "alpha"})
        sup.scheduler.add(
            "reminder: beta", "once", at=base2 + 7200,
            created_at=base2 - 100,
            action={"kind": "notify", "detail": "beta"})
        r = agent._reminder_reply(
            "cancel the last reminder i made",
            "cancel the last reminder i made", task.id)
        self.assertIsNotNone(r)
        self.assertIn("beta", r.content)
        self.assertEqual(
            [s["name"] for s in sup.scheduler.rows],
            ["reminder: alpha"])

        # Day-scoped cancel — 'cancel the dentist on thursday'
        # checks the row's real fire date: out-of-window names the
        # true time and asks; a recurring hit declines to wipe the
        # cadence for a single day.
        import datetime as dt
        sup.scheduler.rows.clear()
        today = dt.date.fromtimestamp(time.time())
        mon = dt.datetime.combine(
            today + dt.timedelta(days=(0 - today.weekday()) % 7 or 7),
            dt.time(10, 0)).timestamp()
        sup.scheduler.add(
            "reminder: dentist", "once", at=mon,
            action={"kind": "notify", "detail": "dentist"})
        sup.scheduler.add(
            "reminder: standup", "daily", hour=9, minute=0,
            action={"kind": "notify", "detail": "standup"})
        r = agent._reminder_reply(
            "cancel the dentist on thursday",
            "cancel the dentist on thursday", task.id)
        self.assertIsNotNone(r)
        self.assertIn("doesn't fire thursday", r.content)
        self.assertIn("Cancel it anyway", r.content)
        self.assertEqual(len(sup.scheduler.rows), 2)
        r = agent._reminder_reply(
            "cancel the dentist on monday",
            "cancel the dentist on monday", task.id)
        self.assertIsNotNone(r)
        self.assertIn("dentist", r.content)
        self.assertEqual(
            [s["name"] for s in sup.scheduler.rows],
            ["reminder: standup"])
        r = agent._reminder_reply(
            "cancel tomorrow's reminders",
            "cancel tomorrow's reminders", task.id)
        self.assertIsNotNone(r)
        self.assertIn("recurring", r.content)
        self.assertEqual(len(sup.scheduler.rows), 1)

        # Fire-verb asks — 'goes off'/'fires'/'rings' are time
        # asks, not state asks; 'which reminder goes off next'
        # names the soonest row, never 'Nothing's off' over live
        # rows. A noun that misses the kind still answers a scoped
        # hit ('the wake alarm' over 'reminder: wake').
        sup.scheduler.rows.clear()
        sup.scheduler.add(
            "reminder: wake", "daily", hour=6, minute=0,
            action={"kind": "notify", "detail": "wake"})
        sup.scheduler.add(
            "reminder: standup", "daily", hour=9, minute=30,
            action={"kind": "notify", "detail": "standup"})
        r = agent._reminder_reply(
            "which reminder goes off next",
            "which reminder goes off next", task.id)
        self.assertIsNotNone(r)
        self.assertIn("wake", r.content)
        r = agent._reminder_reply(
            "what time does the wake alarm go off",
            "what time does the wake alarm go off", task.id)
        self.assertIsNotNone(r)
        self.assertIn("wake", r.content)
        self.assertIn("06:00", r.content)

        # Identical re-create — the row didn't change, so the
        # reply says 'Already set', not 'Moved'.
        sup.scheduler.rows.clear()
        agent._sched_undo = []
        r = agent._reminder_reply(
            "remind me at 9am to stretch",
            "remind me at 9am to stretch", task.id)
        self.assertIsNotNone(r)
        r = agent._reminder_reply(
            "remind me at 9am to stretch",
            "remind me at 9am to stretch", task.id)
        self.assertIsNotNone(r)
        self.assertIn("Already set", r.content)
        self.assertEqual(len(sup.scheduler.rows), 1)

        # 'it' anaphora — the last journaled mutation names the
        # referent: a bare 'turn it off'/'move it' flips THAT row,
        # never mass-applies a singular pronoun.
        r = agent._reminder_reply(
            "remind me daily to call mom",
            "remind me daily to call mom", task.id)
        self.assertIsNotNone(r)
        self.assertEqual(len(sup.scheduler.rows), 2)
        r = agent._reminder_reply(
            "turn it off", "turn it off", task.id)
        self.assertIsNotNone(r)
        self.assertIn("call mom", r.content)
        off_rows = [s["name"] for s in sup.scheduler.rows
                    if not s["enabled"]]
        self.assertEqual(off_rows, ["reminder: call mom"])
        # Reversible lanes bind 'it' to the last-touched row; the
        # destructive lane keeps the pinned contract — a bare
        # pronoun is an empty scope over the enabled pool.
        r = agent._reminder_reply(
            "cancel it", "cancel it", task.id)
        self.assertIsNotNone(r)
        self.assertIn("stretch", r.content)
        self.assertEqual(
            [s["name"] for s in sup.scheduler.rows],
            ["reminder: call mom"])

        # 'turn on the one i just disabled' — a mutation provenance
        # resolves through the journal to that row, not a 'say
        # which' over every opposite-state row.
        r = agent._reminder_reply(
            "turn on the one i just disabled",
            "turn on the one i just disabled", task.id)
        self.assertIsNotNone(r)
        self.assertIn("call mom", r.content)
        self.assertTrue(sup.scheduler.rows[0]["enabled"])
        # Cross-verb compounds decline to run a half: no mutation.
        r = agent._reminder_reply(
            "turn off the call mom and move stretch to 7",
            "turn off the call mom and move stretch to 7",
            task.id)
        self.assertIsNotNone(r)
        self.assertIn("one", r.content.lower())
        self.assertTrue(all(
            s["enabled"] for s in sup.scheduler.rows))
        # 'delete everything' removes rows in ANY state — off rows
        # are still rows. But a scoped miss over an all-off pool
        # can't claim the store is empty.
        for s in sup.scheduler.rows:
            s["enabled"] = False
        r = agent._reminder_reply(
            "delete everything", "delete everything", task.id)
        self.assertIsNotNone(r)
        self.assertIn("call mom", r.content)
        self.assertFalse(sup.scheduler.rows)
        sup.scheduler.add(
            "reminder: wake", "daily", hour=6, minute=0,
            enabled=False,
            action={"kind": "notify", "detail": "wake"})
        r = agent._reminder_reply(
            "cancel the dentist", "cancel the dentist", task.id)
        self.assertIsNotNone(r)
        self.assertNotIn("don't have any", r.content)

        # 'that's going off' resolves by freshest last_run — the
        # row ringing now, never a mass toggle. An hour+ stale
        # last_run isn't ringing.
        for s in list(sup.scheduler.rows):
            sup.scheduler.remove(s["id"])
        sup.scheduler.add(
            "reminder: wake", "daily", hour=6, minute=0,
            last_run=time.time() - 120,
            action={"kind": "notify", "detail": "wake"})
        sup.scheduler.add(
            "reminder: standup", "daily", hour=9, minute=30,
            action={"kind": "notify", "detail": "standup"})
        r = agent._reminder_reply(
            "stop the alarm that's going off",
            "stop the alarm that's going off", task.id)
        self.assertIsNotNone(r)
        self.assertIn("wake", r.content)
        self.assertEqual(
            {s["name"]: s["enabled"] for s in sup.scheduler.rows},
            {"reminder: wake": False, "reminder: standup": True})
        sup.scheduler.rows[0]["enabled"] = True

        # Fired-inventory asks answer from last_run — 'what fired
        # today' scopes last_run to today's date, not next fire.
        r = agent._reminder_reply(
            "which reminders have already fired",
            "which reminders have already fired", task.id)
        self.assertIsNotNone(r)
        self.assertIn("1 of 2", r.content)
        r = agent._reminder_reply(
            "what fired today", "what fired today", task.id)
        self.assertIsNotNone(r)
        self.assertIn("1 of 2", r.content)

        # 'the one i made today' — a day word inside a creation
        # clause scopes created_at, not the fire window; no row
        # created then is an honest miss, never a 'say which'.
        import datetime as _dt
        sup.scheduler.rows[0]["created_at"] = time.mktime(
            (_dt.date.today() - _dt.timedelta(days=1)).timetuple())
        r = agent._reminder_reply(
            "cancel the one i made today",
            "cancel the one i made today", task.id)
        self.assertIsNotNone(r)
        self.assertIn("created today", r.content)
        self.assertEqual(len(sup.scheduler.rows), 2)
        r = agent._reminder_reply(
            "cancel the reminder i set yesterday",
            "cancel the reminder i set yesterday", task.id)
        self.assertIsNotNone(r)
        self.assertIn("wake", r.content)
        self.assertEqual(
            [s["name"] for s in sup.scheduler.rows],
            ["reminder: standup"])

        # 'the 6 o'clock alarm' — 'o'clock' is a clock particle;
        # '6' scopes the 06:00 row even though it's stored under a
        # reminder name, not an 'alarm' name.
        for s in list(sup.scheduler.rows):
            sup.scheduler.remove(s["id"])
        sup.scheduler.add(
            "reminder: wake", "daily", hour=6, minute=0,
            action={"kind": "notify", "detail": "wake"})
        sup.scheduler.add(
            "reminder: trash", "weekly", hour=7, minute=0,
            weekday=4,
            action={"kind": "notify", "detail": "trash"})
        r = agent._reminder_reply(
            "turn off my 6 o'clock alarm",
            "turn off my 6 o'clock alarm", task.id)
        self.assertIsNotNone(r)
        self.assertIn("wake", r.content)
        self.assertEqual(
            {s["name"]: s["enabled"] for s in sup.scheduler.rows},
            {"reminder: wake": False, "reminder: trash": True})
        sup.scheduler.rows[0]["enabled"] = True

        # 'reminders on saturdays' — a fire-day class, not a name;
        # the daily row fires Saturday, the Friday weekly doesn't.
        r = agent._reminder_reply(
            "turn off reminders on saturdays",
            "turn off reminders on saturdays", task.id)
        self.assertIsNotNone(r)
        self.assertIn("wake", r.content)
        self.assertEqual(
            {s["name"]: s["enabled"] for s in sup.scheduler.rows},
            {"reminder: wake": False, "reminder: trash": True})
        sup.scheduler.rows[0]["enabled"] = True

        # 'every other day at 6' on a wake call — the interval
        # spec's PM inference shifts to AM in both hour and the
        # baked 'at' anchor.
        r = agent._reminder_reply(
            "wake me every other day at 6",
            "wake me every other day at 6", task.id)
        self.assertIsNotNone(r)
        iv = sup.scheduler.rows[-1]
        self.assertEqual(iv["kind"], "interval")
        self.assertEqual(iv["hour"], 6)

        # 'set my alarm for 6' — an alarm on a bare clock reads
        # AM; the PM default would ring it at dinner. An explicit
        # meridian stays untouched.
        r = agent._reminder_reply(
            "set my alarm for 6", "set my alarm for 6", task.id)
        self.assertIsNotNone(r)
        self.assertIn("06:00", r.content)
        r = agent._reminder_reply(
            "set my alarm for 6pm", "set my alarm for 6pm", task.id)
        self.assertIsNotNone(r)
        self.assertIn("18:00", r.content)

        # 'am i getting reminded about X' — passive inventory over
        # a real row; 'take down the X' is a cancel.
        sup.scheduler.add(
            "reminder: dentist", "once", at=time.time() + 86400,
            action={"kind": "notify", "detail": "dentist"})
        r = agent._reminder_reply(
            "am i getting reminded about the dentist",
            "am i getting reminded about the dentist", task.id)
        self.assertIsNotNone(r)
        self.assertIn("dentist", r.content)
        r = agent._reminder_reply(
            "take down the dentist reminder",
            "take down the dentist reminder", task.id)
        self.assertIsNotNone(r)
        self.assertIn("dentist", r.content.lower())
        self.assertFalse(
            any("dentist" in s["name"] for s in sup.scheduler.rows))

        # 'when did X last go off' orders last_run, not next-fire —
        # the ordinal lane once answered 'Your last reminder is X'
        # with a future fire. And 'the one ringing' must verify a
        # fresh last_run, not just pick the only enabled row.
        for s in list(sup.scheduler.rows):
            sup.scheduler.remove(s["id"])
        sup.scheduler.add(
            "reminder: wake", "daily", hour=6, minute=0,
            last_run=time.time() - 90,
            action={"kind": "notify", "detail": "wake"})
        sup.scheduler.add(
            "reminder: standup", "daily", hour=9, minute=30,
            action={"kind": "notify", "detail": "standup"})
        r = agent._reminder_reply(
            "when did the wake last go off",
            "when did the wake last go off", task.id)
        self.assertIsNotNone(r)
        self.assertIn("fired", r.content)
        r = agent._reminder_reply(
            "what was the last reminder to fire",
            "what was the last reminder to fire", task.id)
        self.assertIsNotNone(r)
        self.assertIn("wake", r.content)
        r = agent._reminder_reply(
            "stop the alarm that's been going off",
            "stop the alarm that's been going off", task.id)
        self.assertIsNotNone(r)
        self.assertIn("wake", r.content)
        self.assertFalse(sup.scheduler.rows[0]["enabled"])
        self.assertTrue(sup.scheduler.rows[1]["enabled"])
        # 'the one ringing' over rows that didn't fire — an honest
        # miss, never a toggle of an idle row.
        r = agent._reminder_reply(
            "mute the one ringing",
            "mute the one ringing", task.id)
        self.assertIsNotNone(r)
        self.assertIn("ringing", r.content.lower())
        self.assertTrue(sup.scheduler.rows[1]["enabled"])

        # 'change the wake alarm to weekdays only' — a clockless
        # cadence target keeps the row's own clock (06:00) and only
        # rewrites the day set; 'only' is emphasis, not a time.
        sup.scheduler.rows.clear()
        sup.scheduler.add(
            "reminder: wake", kind="daily", hour=6, minute=0)
        sup.scheduler.add(
            "reminder: trash", kind="weekly", weekday=4,
            hour=7, minute=0)
        r = agent._reminder_reply(
            "change the wake alarm to weekdays only",
            "change the wake alarm to weekdays only", task.id)
        self.assertIsNotNone(r)
        self.assertIn("wake", r.content)
        self.assertEqual(sup.scheduler.rows[0]["hour"], 6)
        self.assertEqual(sup.scheduler.rows[0]["minute"], 0)
        self.assertIn(
            sup.scheduler.rows[0].get("weekday"),
            ([0, 1, 2, 3, 4], "weekdays", None))
        # 'the reminder i changed' — provenance binds through the
        # journal's last-touched row (wake, just rescheduled), not
        # a 'say which' or a model guess.
        r = agent._reminder_reply(
            "reschedule the reminder i changed to 5pm",
            "reschedule the reminder i changed to 5pm", task.id)
        self.assertIsNotNone(r)
        self.assertIn("wake", r.content)
        self.assertEqual(sup.scheduler.rows[0]["hour"], 17)
        # 'the one i just toggled' — a mutation-provenance clause
        # plus a 'more'-style duration resolves through the journal
        # and parses the trailing minutes.
        sup.scheduler.rows.clear()
        sup.scheduler.add(
            "reminder: nap", kind="once",
            at=time.time() + 3600)
        sup.scheduler.add(
            "reminder: wake", kind="daily", hour=6, minute=0)
        r = agent._reminder_reply(
            "turn the nap reminder off",
            "turn the nap reminder off", task.id)
        self.assertIsNotNone(r)
        self.assertIn("nap", r.content)
        r = agent._reminder_reply(
            "turn the nap reminder on",
            "turn the nap reminder on", task.id)
        self.assertIsNotNone(r)
        at0 = sup.scheduler.rows[0]["at"]
        r = agent._reminder_reply(
            "snooze the one i just toggled 20 more minutes",
            "snooze the one i just toggled 20 more minutes",
            task.id)
        self.assertIsNotNone(r)
        self.assertIn("nap", r.content)
        self.assertAlmostEqual(
            sup.scheduler.rows[0]["at"], at0 + 1200, delta=5)
        # 'snooze this one' — singular anaphora binds the last
        # journaled row (nap, just snoozed).
        at1 = sup.scheduler.rows[0]["at"]
        r = agent._reminder_reply(
            "snooze this one 5 more minutes",
            "snooze this one 5 more minutes", task.id)
        self.assertIsNotNone(r)
        self.assertIn("nap", r.content)
        self.assertAlmostEqual(
            sup.scheduler.rows[0]["at"], at1 + 300, delta=5)

        # 'clear my old reminders' / 'the ones that fired' — past
        # scope deletes dead one-shots (fired or past `at`), never
        # a recurring row that also fired, never a pending once.
        sup.scheduler.rows.clear()
        sup.scheduler.add(
            "reminder: wake", kind="daily", hour=6, minute=0)
        sup.scheduler.add(
            "reminder: trash", kind="weekly", weekday=4,
            hour=7, minute=0, last_run=time.time() - 86400)
        sup.scheduler.add(
            "reminder: dentist", kind="once",
            at=time.time() - 7200, enabled=False,
            last_run=time.time() - 7200)
        sup.scheduler.add(
            "reminder: lunch", kind="once",
            at=time.time() + 7200)
        r = agent._reminder_reply(
            "clear my old reminders",
            "clear my old reminders", task.id)
        self.assertIsNotNone(r)
        self.assertIn("dentist", r.content)
        self.assertEqual(
            sorted(s["name"] for s in sup.scheduler.rows),
            ["reminder: lunch", "reminder: trash",
             "reminder: wake"])
        # A second past-scope ask over live rows is an honest miss.
        r = agent._reminder_reply(
            "delete my past reminders",
            "delete my past reminders", task.id)
        self.assertIsNotNone(r)
        self.assertIn("No past reminders", r.content)

        # A cadence stranded inside the task tail belongs to the
        # schedule — 'every 30m starting at 9' is an anchored
        # interval, never a one-shot named 'stretch every 30
        # minutes starting'; 'twice a day at 8' anchors the first
        # fire of a 12h interval; 'twice weekly' splits across
        # task/when slots.
        sup.scheduler.rows.clear()
        for q, want_kind, want_iv, want_task in (
                ("remind me to stretch every 30 minutes "
                 "starting at 9am", "interval", 1800.0, "stretch"),
                ("remind me to take meds twice a day at 8am",
                 "interval", 43200.0, "take meds"),
                ("remind me to water plants twice weekly",
                 "interval", 302400.0, "water plants"),
                ("remind me to drink water every other hour",
                 "interval", 7200.0, "drink water")):
            r = agent._reminder_reply(q, q, task.id)
            self.assertIsNotNone(r, q)
            row = sup.scheduler.rows[-1]
            self.assertEqual(row["kind"], want_kind, q)
            self.assertEqual(row["interval_s"], want_iv, q)
            self.assertEqual(
                row["name"], f"reminder: {want_task}", q)
        # 'twice a day at 8am' anchors the first fire at 08:00.
        import datetime as _dt
        at = sup.scheduler.rows[1]["at"]
        self.assertEqual(
            _dt.datetime.fromtimestamp(at).hour, 8)

        # 'advance' is a shift verb — it pulls a recurring row's
        # clock earlier in place. A whole-day shift on a DAILY row
        # is a no-op and says so; on a WEEKLY row it moves the
        # weekday. A quantified shift with no matching row misses
        # honestly instead of falling to the model.
        sup.scheduler.rows.clear()
        sup.scheduler.add(
            "reminder: standup", "daily", hour=9, minute=30,
            action={"kind": "notify", "detail": "standup"})
        sup.scheduler.add(
            "reminder: trash", "weekly", weekday=4,
            hour=7, minute=0,
            action={"kind": "notify", "detail": "trash"})
        r = agent._reminder_reply(
            "advance the standup by an hour",
            "advance the standup by an hour", task.id)
        self.assertIsNotNone(r)
        self.assertIn("Rescheduled", r.content)
        self.assertEqual(sup.scheduler.rows[0]["hour"], 8)
        self.assertEqual(sup.scheduler.rows[0]["minute"], 30)
        self.assertEqual(sup.scheduler.rows[0]["kind"], "daily")
        r = agent._reminder_reply(
            "advance the standup 30 minutes",
            "advance the standup 30 minutes", task.id)
        self.assertIsNotNone(r)
        self.assertEqual(sup.scheduler.rows[0]["hour"], 8)
        self.assertEqual(sup.scheduler.rows[0]["minute"], 0)
        self.assertEqual(sup.scheduler.rows[0]["kind"], "daily")
        r = agent._reminder_reply(
            "advance the standup by 2 days",
            "advance the standup by 2 days", task.id)
        self.assertIsNotNone(r)
        self.assertIn("doesn't change a daily cadence", r.content)
        self.assertEqual(sup.scheduler.rows[0]["hour"], 8)
        r = agent._reminder_reply(
            "advance the trash run 2 days",
            "advance the trash run 2 days", task.id)
        self.assertIsNotNone(r)
        self.assertIn("Rescheduled", r.content)
        self.assertEqual(sup.scheduler.rows[1]["weekday"], 2)
        self.assertEqual(sup.scheduler.rows[1]["hour"], 7)
        r = agent._reminder_reply(
            "push the dentist forward an hour",
            "push the dentist forward an hour", task.id)
        self.assertIsNotNone(r)
        self.assertIn("don't see a reminder", r.content)

        # 're-arm' rides the restore lane — an enabled row is
        # honest 'already on'; a disabled row re-enables for real.
        r = agent._reminder_reply(
            "re-arm the standup reminder",
            "re-arm the standup reminder", task.id)
        self.assertIsNotNone(r)
        self.assertIn("already on", r.content)
        sup.scheduler.set_enabled(
            str(sup.scheduler.rows[1]["id"]), False)
        r = agent._reminder_reply(
            "re-arm the trash reminder",
            "re-arm the trash reminder", task.id)
        self.assertIsNotNone(r)
        self.assertTrue(sup.scheduler.rows[1]["enabled"])

        # A plural scope ('my reminders') is a mass shift — each
        # row's own cadence clock moves by the same delta, not a
        # collapsed single target.
        sup.scheduler.set_enabled(
            str(sup.scheduler.rows[1]["id"]), True)
        r = agent._reminder_reply(
            "delay my reminders by 30 minutes",
            "delay my reminders by 30 minutes", task.id)
        self.assertIsNotNone(r)
        self.assertIn("Snoozed", r.content)
        self.assertEqual(sup.scheduler.rows[0]["hour"], 8)
        self.assertEqual(sup.scheduler.rows[0]["minute"], 30)
        self.assertEqual(sup.scheduler.rows[1]["hour"], 7)
        self.assertEqual(sup.scheduler.rows[1]["minute"], 30)

        # 'swap A and B' exchanges the rows' timing fields through
        # real updates; the undo journal restores both.
        r = agent._reminder_reply(
            "swap the standup and the trash reminders",
            "swap the standup and the trash reminders", task.id)
        self.assertIsNotNone(r)
        self.assertIn("Swapped", r.content)
        self.assertEqual(sup.scheduler.rows[0]["kind"], "weekly")
        self.assertEqual(sup.scheduler.rows[0]["weekday"], 2)
        self.assertEqual(sup.scheduler.rows[1]["kind"], "daily")
        self.assertEqual(sup.scheduler.rows[1]["hour"], 8)
        r = agent._reminder_reply(
            "swap the standup and the dentist",
            "swap the standup and the dentist", task.id)
        self.assertIsNotNone(r)
        self.assertIn("which two", r.content)
        r = agent._reminder_reply(
            "undo that", "undo that", task.id)
        self.assertIsNotNone(r)
        self.assertIn("Undone", r.content)
        self.assertEqual(sup.scheduler.rows[0]["kind"], "daily")
        self.assertEqual(sup.scheduler.rows[0]["hour"], 8)
        self.assertEqual(sup.scheduler.rows[1]["kind"], "weekly")
        self.assertEqual(sup.scheduler.rows[1]["weekday"], 2)

        # 'what time is the trash run' — extra scope words ride
        # along but the row's whole label still resolves.
        r = agent._reminder_reply(
            "what time is the trash run",
            "what time is the trash run", task.id)
        self.assertIsNotNone(r)
        self.assertIn("trash", r.content)
        self.assertNotIn("No reminder", r.content)

        # 'except X'/'but X' is an exclusion scope in every lane —
        # 'cancel all except trash' must NOT delete the trash row.
        sup.scheduler.rows.clear()
        sup.scheduler.add(
            "reminder: standup", "daily", hour=9, minute=30,
            action={"kind": "notify", "detail": "standup"})
        sup.scheduler.add(
            "reminder: trash", "weekly", weekday=4,
            hour=7, minute=0,
            action={"kind": "notify", "detail": "trash"})
        sup.scheduler.add(
            "timer: tea", "once", at=time.time() + 600,
            action={"kind": "notify", "detail": "tea"})
        r = agent._reminder_reply(
            "cancel all reminders except the trash run",
            "cancel all reminders except the trash run", task.id)
        self.assertIsNotNone(r)
        self.assertIn("Cancelled 2", r.content)
        self.assertEqual(
            [s["name"] for s in sup.scheduler.rows],
            ["reminder: trash"])
        sup.scheduler.add(
            "reminder: standup", "daily", hour=9, minute=30,
            action={"kind": "notify", "detail": "standup"})
        sup.scheduler.add(
            "timer: tea", "once", at=time.time() + 600,
            action={"kind": "notify", "detail": "tea"})
        r = agent._reminder_reply(
            "turn off everything but the tea timer",
            "turn off everything but the tea timer", task.id)
        self.assertIsNotNone(r)
        self.assertFalse(sup.scheduler.rows[0]["enabled"])
        self.assertFalse(sup.scheduler.rows[1]["enabled"])
        self.assertTrue(sup.scheduler.rows[2]["enabled"])
        r = agent._reminder_reply(
            "snooze everything except the standup for an hour",
            "snooze everything except the standup for an hour",
            task.id)
        self.assertIsNotNone(r)
        self.assertIn("Snoozed", r.content)

        # Fire-order neighbor and count asks answer from the rows,
        # not a narrated schedule.
        sup.scheduler.set_enabled(
            str(sup.scheduler.rows[0]["id"]), True)
        sup.scheduler.set_enabled(
            str(sup.scheduler.rows[1]["id"]), True)
        r = agent._reminder_reply(
            "what comes after the standup",
            "what comes after the standup", task.id)
        self.assertIsNotNone(r)
        self.assertIn("standup", r.content)
        r = agent._reminder_reply(
            "how many more times will the standup fire",
            "how many more times will the standup fire", task.id)
        self.assertIsNotNone(r)
        self.assertIn("keeps firing", r.content)
        r = agent._reminder_reply(
            "how many more times will the tea timer fire",
            "how many more times will the tea timer fire", task.id)
        self.assertIsNotNone(r)
        self.assertIn("once more", r.content)

        # 'shorten'/'extend' are shift verbs — earlier/later on a
        # once-row's own anchor.
        tea = [s for s in sup.scheduler.rows
               if s["name"] == "timer: tea"][0]
        orig = float(tea["at"])
        r = agent._reminder_reply(
            "shorten the tea timer by 2 minutes",
            "shorten the tea timer by 2 minutes", task.id)
        self.assertIsNotNone(r)
        self.assertAlmostEqual(float(tea["at"]), orig - 120, -1)
        r = agent._reminder_reply(
            "extend the tea timer by 5 minutes",
            "extend the tea timer by 5 minutes", task.id)
        self.assertIsNotNone(r)
        self.assertAlmostEqual(float(tea["at"]), orig + 180, -1)

        # Timed toggles — 'pause X until friday' flips the row off
        # and schedules a real resume nudge; 'turn X back on at
        # 3pm' keeps the row's state and schedules the nudge row.
        # Neither may claim an automatic flip no row performs.
        sup.scheduler.rows.clear()
        sup.scheduler.add(
            "reminder: standup", "daily", hour=9, minute=30,
            action={"kind": "notify", "detail": "standup"})
        sup.scheduler.add(
            "reminder: dentist", "once", at=time.time() - 600,
            enabled=False,
            action={"kind": "notify", "detail": "dentist"})
        r = agent._reminder_reply(
            "pause the standup until friday",
            "pause the standup until friday", task.id)
        self.assertIsNotNone(r)
        self.assertFalse(sup.scheduler.rows[0]["enabled"])
        self.assertIn("nudge", r.content)
        r = agent._reminder_reply(
            "turn the dentist reminder back on at 3pm",
            "turn the dentist reminder back on at 3pm", task.id)
        self.assertIsNotNone(r)
        self.assertIn("stays off", r.content)
        self.assertFalse(sup.scheduler.rows[1]["enabled"])
        self.assertTrue(
            any("dentist" in s["name"] and "turn" in s["name"]
                for s in sup.scheduler.rows))

        # Copy/duplicate — real journaled clones; a clockless
        # target keeps the source's cadence clock. 'copy the
        # file' names no reminder row and falls through.
        sup.scheduler.rows.clear()
        sup.scheduler.add(
            "reminder: standup", "daily", hour=9, minute=30,
            action={"kind": "notify", "detail": "standup"})
        r = agent._reminder_reply(
            "copy the standup to friday",
            "copy the standup to friday", task.id)
        self.assertIsNotNone(r)
        self.assertIn("Copied", r.content)
        cp = [s for s in sup.scheduler.rows
              if s["kind"] == "once"]
        self.assertEqual(len(cp), 1)
        self.assertIn("standup", cp[0]["name"])
        _lt = time.localtime(float(cp[0]["at"]))
        self.assertEqual((_lt.tm_hour, _lt.tm_min), (9, 30))
        self.assertIsNone(agent._reminder_reply(
            "copy the file to desktop",
            "copy the file to desktop", task.id))

        # 'the same thing' — journal anaphora names the last
        # touched row's topic; an empty journal asks instead of
        # minting a literal row.
        r = agent._reminder_reply(
            "remind me about the same thing tomorrow",
            "remind me about the same thing tomorrow", task.id)
        self.assertIsNotNone(r)
        self.assertIn("standup", r.content)
        agent._sched_undo = []
        r = agent._reminder_reply(
            "remind me about the same thing tomorrow",
            "remind me about the same thing tomorrow", task.id)
        self.assertIsNotNone(r)
        self.assertIn("same thing as what", r.content)

        # 'what is overdue' — enabled once-rows past their fire
        # time with no last_run; recurring rows never go overdue.
        for s in list(sup.scheduler.rows):
            sup.scheduler.remove(s["id"])
        sup.scheduler.add(
            "reminder: pills", "once", at=time.time() - 600,
            action={"kind": "notify", "detail": "pills"})
        sup.scheduler.add(
            "reminder: standup", "daily", hour=9, minute=30,
            action={"kind": "notify", "detail": "standup"})
        r = agent._reminder_reply(
            "what is overdue", "what is overdue", task.id)
        self.assertIsNotNone(r)
        self.assertIn("pills", r.content)
        self.assertNotIn("standup", r.content)

        # 'keep X where it is' / 'leave X alone' — a claimed
        # no-op that reports the row's real state.
        r = agent._reminder_reply(
            "keep the standup where it is",
            "keep the standup where it is", task.id)
        self.assertIsNotNone(r)
        self.assertIn("standup", r.content)
        self.assertIn("left alone", r.content)
        r = agent._reminder_reply(
            "leave the pills reminder alone",
            "leave the pills reminder alone", task.id)
        self.assertIsNotNone(r)
        self.assertIn("pills", r.content)

        # 'every weekday but tuesday' — a representable
        # exclusion cadence: weekly minus the named day.
        for s in list(sup.scheduler.rows):
            sup.scheduler.remove(s["id"])
        r = agent._reminder_reply(
            "remind me every weekday but tuesday to stretch",
            "remind me every weekday but tuesday to stretch",
            task.id)
        self.assertIsNotNone(r)
        row = sup.scheduler.rows[-1]
        self.assertEqual(row["kind"], "weekly")
        self.assertEqual(sorted(row["weekday"]), [0, 2, 3, 4])

        # Compound creates — two task+when clauses refuse
        # 'one at a time' rather than minting one malformed
        # merged row; 'call mom and dad at 3pm' is still one
        # reminder since only one side carries a when.
        for s in list(sup.scheduler.rows):
            sup.scheduler.remove(s["id"])
        r = agent._reminder_reply(
            "remind me to call mom in 30 minutes and to water "
            "plants in an hour",
            "remind me to call mom in 30 minutes and to water "
            "plants in an hour", task.id)
        self.assertIsNotNone(r)
        self.assertIn("one reminder at a time", r.content)
        self.assertFalse(sup.scheduler.rows)
        r = agent._reminder_reply(
            "remind me about the dentist on monday and the "
            "pills on tuesday",
            "remind me about the dentist on monday and the "
            "pills on tuesday", task.id)
        self.assertIsNotNone(r)
        self.assertIn("one reminder at a time", r.content)
        self.assertFalse(sup.scheduler.rows)
        r = agent._reminder_reply(
            "remind me to call mom and dad at 3pm",
            "remind me to call mom and dad at 3pm", task.id)
        self.assertIsNotNone(r)
        self.assertIn("mom and dad", r.content)
        self.assertEqual(len(sup.scheduler.rows), 1)
        # 'take pills in the morning and evening' — the
        # cadence splice must not strand 'in the' on the name.
        for s in list(sup.scheduler.rows):
            sup.scheduler.remove(s["id"])
        r = agent._reminder_reply(
            "remind me to take pills in the morning and evening",
            "remind me to take pills in the morning and evening",
            task.id)
        self.assertIsNotNone(r)
        self.assertEqual(
            sup.scheduler.rows[-1]["name"],
            "reminder: take pills")

        # Fire-day windows include recurring rows whose next
        # fire is sooner — a daily row DOES fire Saturday, and
        # 'what fires this week' claims the window ask.
        for s in list(sup.scheduler.rows):
            sup.scheduler.remove(s["id"])
        sup.scheduler.add(
            "reminder: standup", "daily", hour=9, minute=30,
            action={"kind": "notify", "detail": "standup"})
        r = agent._reminder_reply(
            "do i have a reminder set for the weekend",
            "do i have a reminder set for the weekend", task.id)
        self.assertIsNotNone(r)
        self.assertIn("standup", r.content)
        r = agent._reminder_reply(
            "what fires this week", "what fires this week",
            task.id)
        self.assertIsNotNone(r)
        self.assertIn("standup", r.content)
        # 'is anything on right now' — nothing has a fresh
        # last_run; the honest answer names the next fire.
        r = agent._reminder_reply(
            "is anything on right now", "is anything on right now",
            task.id)
        self.assertIsNotNone(r)
        self.assertIn("ringing", r.content.lower())
        self.assertIn("standup", r.content)
        # 'clear todays reminders' — possessive day words scope
        # the window; a once-row firing today cancels.
        sup.scheduler.add(
            "reminder: tea", "once", at=time.time() + 600,
            action={"kind": "notify", "detail": "tea"})
        r = agent._reminder_reply(
            "clear todays reminders",
            "clear todays reminders", task.id)
        self.assertIsNotNone(r)
        self.assertIn("tea", r.content)
        self.assertTrue(
            any(s["name"] == "reminder: standup"
                for s in sup.scheduler.rows))
        # 'at noon and midnight' — two clocks for one reminder
        # refuse instead of minting a garbled name.
        r = agent._reminder_reply(
            "remind me to call mom at noon and midnight",
            "remind me to call mom at noon and midnight", task.id)
        self.assertIsNotNone(r)
        self.assertIn("two times", r.content)
        self.assertFalse(
            any("mom" in s["name"]
                for s in sup.scheduler.rows))

        # Created-ordinal asks — 'i set first'/'most recently'
        # rank created_at, never fire order.
        for s in list(sup.scheduler.rows):
            sup.scheduler.remove(s["id"])
        sup.scheduler.add(
            "reminder: standup", "daily", hour=9, minute=30,
            created_at=time.time() - 300,
            action={"kind": "notify", "detail": "standup"})
        sup.scheduler.add(
            "reminder: tea", "once", at=time.time() + 600,
            created_at=time.time() - 100,
            action={"kind": "notify", "detail": "tea"})
        r = agent._reminder_reply(
            "which reminder did i set first",
            "which reminder did i set first", task.id)
        self.assertIsNotNone(r)
        self.assertIn("standup", r.content)
        r = agent._reminder_reply(
            "what did i set most recently",
            "what did i set most recently", task.id)
        self.assertIsNotNone(r)
        self.assertIn("tea", r.content)
        # 'the Nth reminder i set' — created order, not fire
        # order; 'tea' was created second.
        r = agent._reminder_reply(
            "cancel the second reminder i set",
            "cancel the second reminder i set", task.id)
        self.assertIsNotNone(r)
        self.assertIn("tea", r.content)
        self.assertFalse(
            any("tea" in s["name"] for s in sup.scheduler.rows))
        # 'the reminder before/after X' — a fire-order neighbor
        # scope; an edge anchor answers honestly.
        for s in list(sup.scheduler.rows):
            sup.scheduler.remove(s["id"])
        sup.scheduler.add(
            "reminder: tea", "once", at=time.time() + 600,
            action={"kind": "notify", "detail": "tea"})
        sup.scheduler.add(
            "reminder: dentist", "once", at=time.time() + 3600,
            action={"kind": "notify", "detail": "dentist"})
        r = agent._reminder_reply(
            "cancel the reminder before the dentist",
            "cancel the reminder before the dentist", task.id)
        self.assertIsNotNone(r)
        self.assertFalse(
            any("tea" in s["name"] for s in sup.scheduler.rows))
        self.assertTrue(
            any("dentist" in s["name"]
                for s in sup.scheduler.rows))
        r = agent._reminder_reply(
            "cancel the reminder after the dentist",
            "cancel the reminder after the dentist", task.id)
        self.assertIsNotNone(r)
        self.assertIn("last", r.content)
        self.assertTrue(
            any("dentist" in s["name"]
                for s in sup.scheduler.rows))

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

    def test_reminder_pending_clarify_consumes_bare_fragment(self):
        # 'Snooze until when?' taught a reply shape ('for 20 minutes')
        # no lane consumed — the fragment fell to the model and the
        # snooze never landed. A pending resolver now synthesizes the
        # original scoped op + the fragment into a real mutation.
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

        class _Mgr:
            def __init__(self):
                self._msgs = []

            def set(self, msgs):
                self._msgs = msgs

            def active(self):
                return {"messages": self._msgs}

            def record_exchange(self, u, a):
                pass

        from localcodeagent.workflow.conversation_memory import (
            ConversationMemory)
        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(Path(td), _FakeRuntime())
            agent.conversation_memory = ConversationMemory(
                Path(td) / "cm.json")
            sup = _Sup()
            agent._autonomy = lambda: sup
            agent.conversation_manager = _Mgr()
            task = agent.tasks.create("x", "auto")
            agent._reminder_reply(
                "remind me in 5 minutes to check the oven",
                "remind me in 5 minutes to check the oven", task.id)
            oven = sup.scheduler.rows[-1]

            r = agent._reminder_reply(
                "snooze the oven reminder until later",
                "snooze the oven reminder until later", task.id)
            self.assertIsNotNone(r)
            self.assertIn("until when", r.content.lower())

            agent.conversation_manager.set([
                {"role": "user",
                 "content": "snooze the oven reminder until later"},
                {"role": "assistant", "content": r.content},
                {"role": "user", "content": "for 20 minutes"}])
            self.assertEqual(
                agent._reminder_pending("for 20 minutes"),
                "snooze the oven reminder for 20 minutes")
            before = float(oven.get("next_run") or oven.get("at"))
            r = agent._reminder_reply(
                "for 20 minutes", "for 20 minutes", task.id)
            self.assertIsNotNone(r)
            self.assertIn("Snoozed", r.content)
            self.assertAlmostEqual(
                float(oven.get("next_run") or oven.get("at")),
                before + 1200, -1)

            # The create clarify resolves the same way — the bare
            # fragment completes 'When should I remind you to X?'.
            agent.conversation_manager.set([
                {"role": "user", "content": "remind me to stretch"},
                {"role": "assistant",
                 "content": "When should I remind you to stretch? — "
                            "e.g. “remind me in 10 minutes to "
                            "stretch”."},
                {"role": "user", "content": "in 10 minutes"}])
            self.assertEqual(
                agent._reminder_pending("in 10 minutes"),
                "remind me in 10 minutes to stretch")
            n_rows = len(sup.scheduler.rows)
            r = agent._reminder_reply(
                "in 10 minutes", "in 10 minutes", task.id)
            self.assertIsNotNone(r)
            self.assertIn("Reminder set", r.content)
            self.assertEqual(len(sup.scheduler.rows), n_rows + 1)

            # 'say which' disambiguation consumes ordinals and label
            # names — the picked label splices into the prior op's
            # noun slot so the scoped path owns the turn.
            which = ("That matches 3 reminders: “check the oven” "
                     "(07:48), “stretch” (09:00), “standup” (10:00) — "
                     "say which.")
            agent.conversation_manager.set([
                {"role": "user", "content": "cancel reminders"},
                {"role": "assistant", "content": which},
                {"role": "user", "content": "the second"}])
            self.assertEqual(
                agent._reminder_pending("the second"),
                "cancel stretch reminders")
            agent.conversation_manager.set([
                {"role": "user", "content": "cancel reminders"},
                {"role": "assistant", "content": which},
                {"role": "user", "content": "the oven one"}])
            self.assertEqual(
                agent._reminder_pending("the oven one"),
                "cancel check the oven reminders")
            agent.conversation_manager.set([
                {"role": "user", "content": "cancel reminders"},
                {"role": "assistant", "content": which},
                {"role": "user", "content": "the ninth"}])
            self.assertIsNone(agent._reminder_pending("the ninth"))

            # 'Cancel it anyway?' (a day-scoped miss) resolves 'yes'
            # into the scoped cancel on the quoted row; a non-answer
            # declines the pending.
            anyway = ("“dentist” doesn't fire thursday — it fires "
                      "Mon 10:00. Cancel it anyway?")
            agent.conversation_manager.set([
                {"role": "user",
                 "content": "cancel the dentist on thursday"},
                {"role": "assistant", "content": anyway},
                {"role": "user", "content": "yes"}])
            self.assertEqual(
                agent._reminder_pending("yes"),
                "cancel the dentist reminder")
            agent.conversation_manager.set([
                {"role": "user",
                 "content": "cancel the dentist on thursday"},
                {"role": "assistant", "content": anyway},
                {"role": "user", "content": "hmm"}])
            self.assertIsNone(agent._reminder_pending("hmm"))

            # No outstanding clarify → the fragment declines instead
            # of manufacturing an op.
            agent.conversation_manager.set([
                {"role": "user", "content": "what is the weather"},
                {"role": "assistant", "content": "It is sunny."},
                {"role": "user", "content": "for 20 minutes"}])
            self.assertIsNone(
                agent._reminder_pending("for 20 minutes"))
            # A different clarify in between → stale, no carry-over.
            agent.conversation_manager.set([
                {"role": "user", "content": "some other question"},
                {"role": "assistant", "content": "Here is the answer."},
                {"role": "user", "content": "for 20 minutes"}])
            self.assertIsNone(
                agent._reminder_pending("for 20 minutes"))

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
            result = agent.run("remind me later about the build",
                               mode="auto")
            self.assertIn("couldn't pin a time", result.content)
            # 'in a bit' — vague but conventional; pins ~10min
            # rather than stalling on a pin-fail. 'soon'/'shortly'
            # are the same conventional class (+30m/+20m).
            result = agent.run("remind me in a bit about the deploy",
                               mode="auto")
            self.assertIn("deploy", result.content)
            result = agent.run("remind me soon to nap",
                               mode="auto")
            self.assertIn("Reminder set", result.content)
            self.assertIn("nap", result.content)
            result = agent.run("remind me about the dentist "
                               "in a little while", mode="auto")
            self.assertIn("Reminder set", result.content)

            # 'in 15' — a bare number counts down minutes; 'in
            # 2015' is not a delay.
            self.assertIsNotNone(
                agent._reminder_spec("in 15", time.time()))
            self.assertIsNone(
                agent._reminder_spec("in 2015", time.time()))
            # 'about the meeting in 15' — the when slot held a
            # trailing 'in <dur>'; the left is the task. Fell to a
            # pin-fail before the in-boundary retry.
            n0 = len(sup.scheduler.rows)
            result = agent.run("remind me about the meeting in 15",
                               mode="auto")
            self.assertIn("meeting", result.content)
            self.assertGreater(len(sup.scheduler.rows), n0)
            # 'another reminder for 4' — 'for 4' is still a bare
            # clock, never 'in 4' minutes.
            result = agent.run("another reminder for 4", mode="auto")
            self.assertIn("do what at 4", result.content)
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
            # 'in a few minutes' — vague but conventional (+3m);
            # with no task it asks what to remind, never the
            # model's 'The timer is 30 minutes' invention.
            n0 = len(sup.scheduler.rows)
            result = agent.run("remind me in a few minutes",
                               mode="auto")
            self.assertIn("do what", result.content)
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

            # 'move the pay rent reminder to 11' — a bare-number
            # reschedule target is the clock (11:00), never
            # 'in 11 minutes'.
            row = sup.scheduler.add(
                "Reminder: dentist", "once",
                at=time.time() + 90000)
            result = agent.run(
                "move the dentist reminder to 11", mode="auto")
            self.assertIn("Rescheduled", result.content)
            self.assertIn("11:00", result.content)
            sup.scheduler.remove(str(row["id"]))

            # 'cancel everything' — pool-wide cancel through the
            # deterministic lane; the model fake-refused 'no
            # active reminders' over a live pool.
            sup.scheduler.add(
                "Reminder: dentist", "once",
                at=time.time() + 90000)
            result = agent.run("cancel everything", mode="auto")
            self.assertIn("Cancelled 2", result.content)
            self.assertFalse(sup.scheduler.rows)

            # 'is anything due soon' — 'due' lanes to real
            # inventory; the model hallucinated 'pay rent set
            # for next month' when it was days away.
            sup.scheduler.add(
                "Reminder: pay rent", "once",
                at=time.time() + 86400)
            result = agent.run("is anything due soon",
                               mode="auto")
            self.assertIn("pay rent", result.content)

            # Unsupported nth-weekday — 'first monday of the
            # month' must pin-fail with the FULL when and the
            # clean task, not the lazy split's 'the month to'.
            result = agent.run(
                "remind me the first monday of the month to "
                "pay dues", mode="auto")
            self.assertIn("couldn't pin", result.content)
            self.assertIn("first monday of the month",
                          result.content)
            self.assertIn("pay dues", result.content)
            sup.scheduler.rows.clear()

            # Worded hour + meridiem — 'at five pm' pin-failed
            # while digit clocks parsed.
            result = agent.run("remind me at five pm to leave",
                               mode="auto")
            self.assertIn("leave", result.content)
            row = sup.scheduler.rows[-1]
            self.assertEqual(time.localtime(row["at"]).tm_hour, 17)
            sup.scheduler.rows.clear()

            # 'every hour on the hour' — the redundant tail broke
            # the interval fullmatch into a pin-fail.
            result = agent.run(
                "remind me every hour on the hour to breathe",
                mode="auto")
            self.assertIn("breathe", result.content)
            self.assertEqual(
                sup.scheduler.rows[-1].get("interval_s"), 3600.0)
            sup.scheduler.rows.clear()

            # 'half past noon' — _hour_num now knows noon/midnight;
            # the live probe mangled it to task 'at half past' at
            # 12:00. Quarter-to-midnight resolves to 23:45, not
            # the naive 11:45.
            from localcodeagent.autonomy.scheduler import (
                parse_reminder_when as _prw)
            now = time.time()
            self.assertEqual(
                time.localtime(_prw("at half past noon", now))
                .tm_hour, 12)
            self.assertEqual(
                time.localtime(_prw("at half past noon", now))
                .tm_min, 30)
            self.assertEqual(
                time.localtime(_prw("at quarter to midnight", now))
                .tm_hour, 23)
            self.assertEqual(
                time.localtime(_prw("at half past midnight", now))
                .tm_hour, 0)
            result = agent.run(
                "remind me at half past noon to eat", mode="auto")
            self.assertIn("eat", result.content)
            row = sup.scheduler.rows[-1]
            self.assertEqual(time.localtime(row["at"]).tm_min, 30)
            sup.scheduler.rows.clear()

            # 'at 6 o'clock' — the redundant tail broke the clock
            # match; o'clock now strips in both parser entries.
            result = agent.run(
                "remind me at 6 o'clock to leave", mode="auto")
            self.assertIn("leave", result.content)
            row = sup.scheduler.rows[-1]
            self.assertEqual(time.localtime(row["at"]).tm_hour, 18)
            sup.scheduler.rows.clear()

            # 'nudge me at 3' — new verbs lane and self-task; the
            # model narrated 'Nudge set for 3:00 PM' over no row.
            result = agent.run("nudge me at 3 tomorrow",
                               mode="auto")
            self.assertIn("nudge you", result.content)
            self.assertTrue(sup.scheduler.rows)
            sup.scheduler.rows.clear()

            # 'on friday at 3pm' — 'on <weekday>' singular is the
            # coming Friday once; only 'on fridays'/'every friday'
            # recurs. The live probe banked a weekly row.
            result = agent.run(
                "remind me on friday at 3pm to meet", mode="auto")
            row = sup.scheduler.rows[-1]
            self.assertEqual(row["kind"], "once")
            sup.scheduler.rows.clear()

            # 'twice daily' — the model narrated 'I'll remind you
            # twice daily — first at 8 AM, then 6 PM' over zero
            # rows; it's the 12h interval 'twice a day' is.
            result = agent.run("remind me twice daily to stretch",
                               mode="auto")
            self.assertEqual(
                sup.scheduler.rows[-1].get("interval_s"), 43200.0)
            sup.scheduler.rows.clear()

            # 'at dawn' — the model promised to 'track sunrise';
            # honest pin-fail instead. 'in like 5 minutes' — the
            # filler word broke the duration match.
            result = agent.run("remind me at dawn", mode="auto")
            self.assertIn("couldn't pin", result.content)
            self.assertFalse(sup.scheduler.rows)
            result = agent.run(
                "remind me in like 5 minutes to check",
                mode="auto")
            self.assertIn("check", result.content)
            self.assertTrue(sup.scheduler.rows)
            sup.scheduler.rows.clear()

            # 'in an hour or so' — the 'or so' tail broke the
            # duration fullmatch into a pin-fail.
            result = agent.run(
                "remind me in an hour or so to check",
                mode="auto")
            self.assertIn("check", result.content)
            self.assertTrue(sup.scheduler.rows)
            sup.scheduler.rows.clear()

            # 'start a timer' — 'start' wasn't a DECL lead; it
            # fell to the app-launch lane ('needs approval').
            result = agent.run("start a timer for 10 minutes",
                               mode="auto")
            self.assertIn("Timer set", result.content)
            self.assertTrue(sup.scheduler.rows)

            # 'how much time left on my timer' — countdown
            # phrasing without 'next' skipped the soonest
            # answer; the model claimed no timer over a live row.
            result = agent.run(
                "how much time is left on my timer",
                mode="auto")
            self.assertIn("timer", result.content.lower())
            self.assertIn("in ", result.content)

            # 'at 6 tomorrow morning to get up' — the connector
            # tail kept 'to get up' inside the when; the 'to'
            # split now resolves clock vs task.
            result = agent.run(
                "i'll need a reminder at 6 tomorrow morning to "
                "get up", mode="auto")
            self.assertIn("get up", result.content)
            row = sup.scheduler.rows[-1]
            self.assertEqual(time.localtime(row["at"]).tm_hour, 6)
            sup.scheduler.rows.clear()

            # 'in a half hour' / 'in an hour and twenty
            # minutes' — idiom fraction and compound-unit
            # durations pin-failed over real grammar gaps.
            now = time.time()
            self.assertAlmostEqual(
                _prw("in a half hour", now), now + 1800,
                delta=2)
            self.assertAlmostEqual(
                _prw("in an hour and twenty minutes", now),
                now + 4800, delta=2)

            # 'i want to be reminded' — the passive had no
            # 'me' for the verb phrase; the model false-refused
            # 'no access to scheduling'. Normalizes to active.
            result = agent.run(
                "i want to be reminded every morning to stretch",
                mode="auto")
            self.assertIn("stretch", result.content)
            self.assertEqual(
                sup.scheduler.rows[-1]["kind"], "daily")

            # 'reminder: stand up every hour' — a colon-labelled
            # create; the model refused scheduling outright.
            sup.scheduler.rows.clear()
            result = agent.run("reminder: stand up every hour",
                               mode="auto")
            self.assertIn("stand up", result.content)
            self.assertTrue(sup.scheduler.rows)
            sup.scheduler.rows.clear()

            # 'remind me twice' — 'twice' is timeish; honest
            # pin-fail, not a model 'can't do that' refusal.
            result = agent.run("remind me twice", mode="auto")
            self.assertIn("couldn't pin", result.content)
            self.assertFalse(sup.scheduler.rows)

            # 'every 90 seconds' — seconds were missing from
            # the interval units.
            result = agent.run(
                "remind me every 90 seconds to blink",
                mode="auto")
            self.assertIn("blink", result.content)
            self.assertTrue(sup.scheduler.rows)
            sup.scheduler.rows.clear()

            # Precision hedges — 'at 7pm sharp' / 'at about 6' /
            # 'around 6' pin-failed over filler the clock grammar
            # doesn't carry. 'in about 10 minutes' stays a
            # duration (the about→at rewrite is 'in'-guarded).
            self.assertIsNotNone(_prw("at 7pm sharp", now))
            self.assertIsNotNone(_prw("at about 6", now))
            self.assertIsNotNone(_prw("around 6", now))
            self.assertAlmostEqual(
                _prw("in about 10 minutes", now), now + 600,
                delta=2)

            # 'what's on my calendar' — 'calendar' lanes to
            # inventory; the sm-capture scope strips it like
            # the other schedule nouns.
            sup.scheduler.add(
                "Reminder: pay rent", "once",
                at=time.time() + 86400)
            result = agent.run("what's on my calendar",
                               mode="auto")
            self.assertIn("pay rent", result.content)

            # State asks — 'are my reminders on' answered 'I don't
            # have a system to check' from the model live; the
            # answer is the inventory.
            task = agent.tasks.create("x", "auto")
            for state_q in ("are my reminders on",
                            "are my reminders paused",
                            "are reminders enabled"):
                r = agent._reminder_reply(
                    state_q, state_q, task.id)
                self.assertIsNotNone(r, state_q)
                self.assertIn("pay rent", r.content)
            r = agent._reminder_reply(
                "is my alarm on", "is my alarm on", task.id)
            self.assertIsNotNone(r)
            self.assertIn("pay rent", r.content)

            # 'forget to' wrappers and the 'that i need to'
            # connector normalize into the create lane.
            for raw, pt in (
                    ("don't forget to remind me at 7",
                     " remind me at 7"),
                    ("i'll forget to call mom",
                     "remind me to call mom"),
                    ("remind me that i need to call mom",
                     "remind me to call mom"),
                    ("remind me!!! at 5", "remind me at 5")):
                r = agent._reminder_reply(raw, pt, task.id)
                self.assertIsNotNone(r, raw)
            # 'i'll forget to X' normalizes to a no-time create —
            # the honest ask names the task.
            r = agent._reminder_reply(
                "i'll forget to call mom",
                "remind me to call mom", task.id)
            self.assertIn("call mom", r.content)

            # Out-of-range clocks — 'at 25pm' silently rolled to
            # 1am via mktime; it must pin-fail, never schedule.
            self.assertIsNone(_prw("at 25pm", now))
            self.assertIsNone(_prw("at 25 o'clock", now))
            self.assertIsNone(_prw("at 6:99", now))
            self.assertIsNotNone(_prw("at 14:30", now))
            self.assertIsNotNone(_prw("at 12pm", now))

            # 'when the meeting starts remind me' — the model
            # fabricated 'The meeting is at 3 — I'll remind you
            # then'. A leading trigger refuses honestly.
            for trig_q in ("when the meeting starts remind me",
                           "when it rains remind me",
                           "when you can remind me"):
                r = agent._reminder_reply(trig_q, trig_q, task.id)
                self.assertIsNotNone(r, trig_q)
                self.assertIn("trigger", r.content)

            # 'undo that reminder' is a cancel verb; the single
            # real row is the referent.
            r = agent._reminder_reply(
                "undo that reminder", "undo that reminder",
                task.id)
            self.assertIsNotNone(r)
            self.assertIn("pay rent", r.content)
            self.assertFalse(
                any(s.get("enabled") for s in sup.scheduler.rows))
            sup.scheduler.add(
                "Reminder: pay rent", "once",
                at=time.time() + 86400)

            # 'on 10/15' numeric date and '7a'/'7p' shorthand
            # meridiems resolve to real timestamps.
            self.assertIsNotNone(_prw("on 10/15", now))
            self.assertIsNotNone(_prw("on 12/31", now))
            self.assertIsNotNone(_prw("at 7a", now))
            self.assertIsNotNone(_prw("at 7p", now))

            # 'move it up an hour' — the model refused ('I can't
            # move...'). 'up'/'down' on 'move' snoozes earlier/
            # later; 'move X to friday' still reschedules.
            sup.scheduler.rows.clear()
            sup.scheduler.add(
                "Reminder: pay rent", "once",
                at=time.time() + 86400)
            before = float(sup.scheduler.rows[0]["at"])
            r = agent._reminder_reply(
                "move it up an hour", "move it up an hour",
                task.id)
            self.assertIsNotNone(r)
            self.assertIn("Snoozed", r.content)
            self.assertAlmostEqual(
                float(sup.scheduler.rows[0]["at"]),
                before - 3600, delta=5)
            r = agent._reminder_reply(
                "move my reminder to friday",
                "move my reminder to friday", task.id)
            self.assertIsNotNone(r)
            self.assertIn("Rescheduled", r.content)

            # 'monday through friday' — the parser ate 'friday'
            # as the day and banked 'monday through' as the task
            # live. Range phrasing is the weekdays cadence.
            sup.scheduler.rows.clear()
            r = agent._reminder_reply(
                "remind me monday through friday to stand",
                "remind me monday through friday to stand",
                task.id)
            self.assertIsNotNone(r)
            row = sup.scheduler.rows[0]
            self.assertEqual(row["kind"], "weekly")
            self.assertEqual(row["weekday"], [0, 1, 2, 3, 4])
            self.assertIn("stand", row["name"])

            # 'N units before|after CLOCK' — '5 minutes before
            # 3pm' booked 15:00 with the garbage task '5 minutes
            # before' live; it is a real clock offset.
            ts = _prw("5 minutes before 3pm", now)
            self.assertIsNotNone(ts)
            self.assertEqual(
                time.localtime(ts).tm_hour, 14)
            self.assertEqual(
                time.localtime(ts).tm_min, 55)
            self.assertIsNotNone(
                _prw("5 minutes after 3", now))

            # 'cancel them all' — noun 'all' + body 'them' scoped
            # the topic to the pronoun; it wiped nothing live.
            sup.scheduler.add(
                "Reminder: stretch", "once",
                at=time.time() + 3600)
            r = agent._reminder_reply(
                "cancel them all", "cancel them all", task.id)
            self.assertIsNotNone(r)
            self.assertIn("2", r.content)
            self.assertFalse(sup.scheduler.rows)

            # 'reschedule it' — no target → the deterministic ask,
            # not the model's 'which reminder' narration.
            r = agent._reminder_reply(
                "reschedule it", "reschedule it", task.id)
            self.assertIsNotNone(r)
            self.assertIn("when", r.content.lower())

            # 'reschedule pay rent to next monday' — noun-less body
            # fell to the model which narrated 'no tool exists to
            # modify reminders' while the lane mutates `at` fine.
            sup.scheduler.rows.clear()
            sup.scheduler.add(
                "Reminder: pay rent", "once", at=time.time() + 86400,
                action={"kind": "notify", "detail": "pay rent"})
            r = agent._reminder_reply(
                "reschedule pay rent to next monday",
                "reschedule pay rent to next monday", task.id)
            self.assertIsNotNone(r)
            self.assertIn("Rescheduled", r.content)
            # Scoped miss on a noun-less body names the real pool.
            r = agent._reminder_reply(
                "reschedule my dentist appointment to friday",
                "reschedule my dentist appointment to friday",
                task.id)
            self.assertIsNotNone(r)
            self.assertIn("pay rent", r.content)

            # 'cancel my pay rent' — noun-less cancel fell to the
            # model which narrated a fake 'no permission' refusal
            # (invented a lease excuse) over a live row.
            r = agent._reminder_reply(
                "cancel my pay rent", "cancel my pay rent", task.id)
            self.assertIsNotNone(r)
            self.assertIn("Cancelled", r.content)
            self.assertFalse(sup.scheduler.rows)
            # Scoped miss on a noun-less body names the real pool;
            # file/object shapes stay with the action lanes.
            sup.scheduler.add(
                "Reminder: pay rent", "once", at=time.time() + 86400,
                action={"kind": "notify", "detail": "pay rent"})
            r = agent._reminder_reply(
                "cancel my netflix subscription",
                "cancel my netflix subscription", task.id)
            self.assertIsNotNone(r)
            self.assertIn("pay rent", r.content)
            self.assertIsNone(
                agent._REMINDER_CANCEL_BARE_RE.match("delete junk.txt"))
            self.assertIsNone(
                agent._REMINDER_CANCEL_BARE_RE.match(
                    "delete the folder"))
            sup.scheduler.rows.clear()

            # 'timer for 10' — the bare number is minutes, never
            # the 10:00 clock the 'at' fallback reached for.
            r = agent._reminder_reply(
                "a timer for 10", "a timer for 10", task.id)
            self.assertIsNotNone(r)
            row = sup.scheduler.rows[0]
            self.assertGreater(
                float(row["at"]) - time.time(), 500)
            self.assertLess(
                float(row["at"]) - time.time(), 800)

            # Vague timer asks — 'soon'/'a bit'/'a little while'
            # parse as once-offsets for reminders, but a countdown
            # has no honest duration to honor. They must refuse
            # rather than mint 'Timer set for <random clock>'.
            sup.scheduler.rows.clear()
            for vague in ("set a timer for soon",
                          "set a timer soon",
                          "set a timer in a bit",
                          "set a timer for a little while",
                          "set a timer soon to stretch"):
                r = agent._reminder_reply(vague, vague, task.id)
                self.assertIsNotNone(r, vague)
                self.assertIn("needs a duration", r.content, vague)
            self.assertEqual(len(sup.scheduler.rows), 0)
            # The same spans remain valid for reminders, and a real
            # duration still makes a real timer row.
            r = agent._reminder_reply(
                "set a timer for 10 minutes to stretch",
                "set a timer for 10 minutes to stretch", task.id)
            self.assertIsNotNone(r)
            self.assertIn("Timer set", r.content)
            self.assertIn("stretch", r.content)
            self.assertEqual(len(sup.scheduler.rows), 1)

            # 'at six thirty' — the TRAIL courtesy stripped 'ty'
            # off 'thirty', leaving 'at six thir' to the model.
            r = agent._reminder_reply(
                "remind me at six thirty",
                "remind me at six thirty", task.id)
            self.assertIsNotNone(r)
            self.assertIn("do what", r.content)

            # 'i have a reminder at 5 right' — a confirmation
            # ask; the model guessed 'No reminder at 5 right now'
            # from narration. Confirm-tags lane to real inventory.
            # The pool must not hold a floating-fire row here —
            # 'at 5' legitimately scopes onto a row firing in the
            # 5 o'clock hour, so seed one at a fixed clock instead.
            sup.scheduler.rows.clear()
            lt = time.localtime(time.time() + 86400)
            sup.scheduler.add(
                "Reminder: standup", "once",
                at=time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                                10, 0, 0, 0, 0, -1)),
                action={"kind": "notify", "detail": "standup"})
            r = agent._reminder_reply(
                "i have a reminder at 5 right",
                "i have a reminder at 5 right", task.id)
            self.assertIsNotNone(r)
            self.assertIn("you've got", r.content)

            # 'same time tomorrow' — model narrated '"Same time
            # tomorrow" — set to 10:00 AM. Confirmed.' over zero
            # rows. 'same time' lanes to the honest pin-fail.
            r = agent._reminder_reply(
                "same time tomorrow", "same time tomorrow",
                task.id)
            self.assertIsNotNone(r)
            self.assertIn("couldn't pin", r.content)

            # 'is there anything tomorrow' — no schedule noun;
            # the model narrated 'No scheduled reminders'. The
            # anything+daypart noun variant answers the real pool.
            sup.scheduler.add(
                "Reminder: pay rent", "once",
                at=time.time() + 86400)
            r = agent._reminder_reply(
                "is there anything tomorrow",
                "is there anything tomorrow", task.id)
            self.assertIsNotNone(r)
            self.assertIn("pay rent", r.content)

            # 'every few hours' — informal three-unit span.
            spec = agent._reminder_spec(
                "every few hours", time.time())
            self.assertIsNotNone(spec)
            self.assertEqual(spec["interval_s"], 10800.0)

            # "how's my reminders"/'reminders status' — the model
            # hallucinated 'pay rent set for tomorrow at 8:00 PM'
            # (it's Friday 10:00). how/status interrogatives answer
            # the real pool.
            for q in ("how's my reminders", "reminders status",
                      "how are my reminders"):
                r = agent._reminder_reply(q, q, task.id)
                self.assertIsNotNone(r, q)
                self.assertIn("pay rent", r.content)

            # 'nag me at 5' — 'nag' wasn't a verb; the model
            # narrated 'I'll nudge you at 5:00 PM tomorrow.'
            r = agent._reminder_reply(
                "nag me at 5", "nag me at 5", task.id)
            self.assertIsNotNone(r)
            self.assertIn("nag", r.content.lower())
            self.assertTrue(sup.scheduler.rows)
            sup.scheduler.rows.clear()

            # 'cancel everything except X' — the tail was treated
            # as a topic ('I don't see a reminder matching
            # "except pay rent"'). The except-clause is exclusion
            # scope: cancel every other row.
            sup.scheduler.add(
                'Reminder: pay rent', 'once',
                at=time.time() + 86400)
            sup.scheduler.add(
                'Reminder: call mom', 'once',
                at=time.time() + 86400)
            r = agent._reminder_reply(
                "cancel everything except pay rent",
                "cancel everything except pay rent", task.id)
            self.assertIsNotNone(r)
            self.assertIn("call mom", r.content)
            self.assertEqual(
                [s["name"] for s in sup.scheduler.rows],
                ["Reminder: pay rent"])
            # Excluding the only row names the dead end honestly.
            r = agent._reminder_reply(
                "cancel everything except pay rent",
                "cancel everything except pay rent", task.id)
            self.assertIsNotNone(r)
            self.assertIn("nothing else", r.content)
            sup.scheduler.rows.clear()

            # 'annual reminder for taxes' — the model fake-set
            # 'Annual tax reminder set for October 11th' over zero
            # rows. annual/yearly/quarterly/semiannual and
            # 'twice a month/week/year' resolve real intervals.
            for when, secs in (
                    ("annual", 31536000.0), ("yearly", 31536000.0),
                    ("quarterly", 91 * 86400.0),
                    ("semiannual", 182 * 86400.0),
                    ("twice a month", 15 * 86400.0),
                    ("twice a week", 302400.0)):
                spec = agent._reminder_spec(when, time.time())
                self.assertIsNotNone(spec, when)
                self.assertEqual(spec["kind"], "interval")
                self.assertEqual(spec["interval_s"], secs)
            r = agent._reminder_reply(
                "annual reminder for taxes",
                "annual reminder for taxes", task.id)
            self.assertIsNotNone(r)
            self.assertIn("taxes", r.content)
            self.assertTrue(sup.scheduler.rows)
            sup.scheduler.rows.clear()

            # 'occasionally'/'periodically' — no frequency exists;
            # pin-fail with the parsed task, never a fake cadence.
            for q, task_word in (
                    ("remind me occasionally to stretch", "stretch"),
                    ("remind me periodically to save", "save")):
                r = agent._reminder_reply(q, q, task.id)
                self.assertIsNotNone(r, q)
                self.assertIn("couldn't pin a time", r.content)
                self.assertIn(task_word, r.content)
            self.assertFalse(sup.scheduler.rows)

            # 'remind me to X at T' — the clarify regex swallowed
            # the clock into the task ('When should I remind you to
            # call mom at 3?'). Longest parseable tail is the when.
            r = agent._reminder_reply(
                "remind me to call mom at 3",
                "remind me to call mom at 3", task.id)
            self.assertIsNotNone(r)
            self.assertIn("call mom", r.content)
            self.assertIn("15:00", r.content)
            r = agent._reminder_reply(
                "remind me to eat at noon tomorrow",
                "remind me to eat at noon tomorrow", task.id)
            self.assertIsNotNone(r)
            self.assertIn("12:00", r.content)
            self.assertIn("eat", r.content)
            # 'at the top of the hour' — the split cut inside the
            # clock phrase; re-joined it resolves to the next :00
            # and asks for the task honestly.
            r = agent._reminder_reply(
                "remind me at the top of the hour",
                "remind me at the top of the hour", task.id)
            self.assertIsNotNone(r)
            self.assertIn("do what", r.content)
            # 'roughly at 6' / '7ish' / '8 and a half' / 'seven-
            # thirty' / 'school days' resolve real times.
            spec = agent._reminder_spec(
                "at the top of the hour", time.time())
            self.assertIsNotNone(spec)
            for when in ("at 7ish", "at 8 and a half",
                         "seven-thirty", "6 am weekdays",
                         "on school days at 7",
                         "at 5pm on the 20th"):
                self.assertIsNotNone(
                    agent._reminder_spec(when, time.time()), when)
            # 'at 5 then again at 7' — the second clock landed in
            # the task slot; never a row named 'at 5 then again'.
            r = agent._reminder_reply(
                "remind me at 5 then again at 7",
                "remind me at 5 then again at 7", task.id)
            self.assertIsNotNone(r)
            self.assertIn("pin a time", r.content)
            self.assertFalse([
                s for s in sup.scheduler.rows
                if "then again" in s["name"]])
            # 'set two reminders' — a count lead can't be one row;
            # ask for the task/time honestly.
            r = agent._reminder_reply(
                "set two reminders for tomorrow",
                "set two reminders for tomorrow", task.id)
            self.assertIsNotNone(r)
            self.assertIn("do what", r.content)
            sup.scheduler.rows.clear()

            # 'in a quarter of an hour' — the model echoed 'Got it —
            # remind you in 15 minutes' over zero rows.
            spec = agent._reminder_spec(
                "in a quarter of an hour", time.time())
            self.assertIsNotNone(spec)
            spec = agent._reminder_spec(
                "in three quarters of an hour", time.time())
            self.assertIsNotNone(spec)
            # 'first thing tomorrow' → next-day 07:00, not a task
            # named 'first thing'.
            spec = agent._reminder_spec(
                "first thing tomorrow", time.time())
            self.assertIsNotNone(spec)
            # 'end of the day/week' — vague boundaries pin-fail on
            # the joined text rather than narrating 'I'll remind
            # you at the end of the day'.
            r = agent._reminder_reply(
                "remind me at the end of the day",
                "remind me at the end of the day", task.id)
            self.assertIsNotNone(r)
            self.assertIn("end of the day", r.content)
            # 'snooze pay rent by an hour' — scope+dur captured as
            # one blob; the 'by <dur>' tail splits off as delta.
            sup.scheduler.add(
                'Reminder: pay rent', 'once',
                at=time.time() + 86400)
            r = agent._reminder_reply(
                "snooze pay rent by an hour",
                "snooze pay rent by an hour", task.id)
            self.assertIsNotNone(r)
            self.assertIn("Snoozed", r.content)
            self.assertIn("pay rent", r.content)
            sup.scheduler.rows.clear()

            # Meal dayparts — 'around lunchtime' was a model
            # fake-set; lunch/dinner/breakfast pin real clocks.
            r = agent._reminder_reply(
                "remind me around lunchtime to eat",
                "remind me around lunchtime to eat", task.id)
            self.assertIsNotNone(r)
            self.assertIn("12:00", r.content)
            self.assertIn("eat", r.content)
            spec = agent._reminder_spec("at dinner", time.time())
            self.assertIsNotNone(spec)
            # 'sometime tonight' made a row named 'sometime'; the
            # qualifier strips, the daypart resolves, the reply
            # asks for the task.
            r = agent._reminder_reply(
                "remind me sometime tonight",
                "remind me sometime tonight", task.id)
            self.assertIsNotNone(r)
            self.assertIn("do what", r.content)
            self.assertFalse([
                s for s in sup.scheduler.rows
                if "sometime" in s["name"]])
            sup.scheduler.rows.clear()

            # 'every hour until 10pm' — the until-tail split off as
            # the time and made task 'every hour until'. The base
            # cadence creates with an honest no-end-bound note.
            r = agent._reminder_reply(
                "remind me every hour until 10pm to stretch",
                "remind me every hour until 10pm to stretch",
                task.id)
            self.assertIsNotNone(r)
            self.assertIn("every", r.content.lower())
            self.assertIn("can't hold", r.content)
            self.assertEqual(
                sup.scheduler.rows[-1]["kind"], "interval")
            sup.scheduler.rows.clear()
            # 'the first of every month' — worded ordinal is the
            # day (was monthly on today's day-of-month).
            spec = agent._reminder_spec(
                "the first of every month", time.time())
            self.assertIsNotNone(spec)
            self.assertEqual(spec["day"], 1)
            # 'when it's 5pm' — the when-it's lead is the clock,
            # not a task name.
            r = agent._reminder_reply(
                "remind me when it's 5pm to leave",
                "remind me when it's 5pm to leave", task.id)
            self.assertIsNotNone(r)
            self.assertIn("17:00", r.content)
            self.assertIn("leave", r.content)
            # 'in a bit'/'in a while' — vague small delays pin
            # conventional spans.
            self.assertIsNotNone(
                agent._reminder_spec("in a bit", time.time()))
            self.assertIsNotNone(
                agent._reminder_spec("in a while", time.time()))
            # 'of each month' — the cadence tail rejoins the when;
            # 'each' joins 'every' in the monthly grammar.
            r = agent._reminder_reply(
                "remind me on the 15th of each month to pay dues",
                "remind me on the 15th of each month to pay dues",
                task.id)
            self.assertIsNotNone(r)
            self.assertIn("monthly on the 15th", r.content)
            self.assertEqual(
                sup.scheduler.rows[-1]["kind"], "monthly")
            self.assertEqual(sup.scheduler.rows[-1]["day"], 15)
            sup.scheduler.rows.clear()
            r = agent._reminder_reply(
                "remind me on the 15th of each month",
                "remind me on the 15th of each month", task.id)
            self.assertIsNotNone(r)
            self.assertIn("do what", r.content)
            self.assertIn("15th of each month", r.content)
            self.assertFalse(sup.scheduler.rows)
            # 'on the 5th of each week' — 'of each <period>'
            # doesn't fit a day anchor; pin-fail on the joined
            # phrase, never a row named 'each week to …'.
            r = agent._reminder_reply(
                "remind me on the 5th of each week to check in",
                "remind me on the 5th of each week to check in",
                task.id)
            self.assertIsNotNone(r)
            self.assertIn("pin a time", r.content)
            self.assertFalse([
                s for s in sup.scheduler.rows
                if "each week" in s["name"]])
            # 'last day of the month' — unsupported ordinal;
            # honest pin-fail that echoes the when once, not
            # 'of the month of the month'.
            r = agent._reminder_reply(
                "remind me on the last day of the month",
                "remind me on the last day of the month", task.id)
            self.assertIsNotNone(r)
            self.assertIn("pin a time", r.content)
            self.assertNotIn("of the month of the month",
                             r.content)
            # 'for <dur>' on a cadence is an end bound the
            # scheduler can't hold — strip it, never bank 'an
            # hour' as a task. 'for <nondur>' is the task itself.
            r = agent._reminder_reply(
                "remind me every 15 minutes for an hour",
                "remind me every 15 minutes for an hour", task.id)
            self.assertIsNotNone(r)
            self.assertIn("do what", r.content)
            self.assertFalse([
                s for s in sup.scheduler.rows
                if "an hour" in s["name"]])
            r = agent._reminder_reply(
                "remind me every hour for 2 days to drink water",
                "remind me every hour for 2 days to drink water",
                task.id)
            self.assertIsNotNone(r)
            self.assertIn("can't hold", r.content)
            self.assertEqual(
                sup.scheduler.rows[-1]["kind"], "interval")
            sup.scheduler.rows.clear()
            r = agent._reminder_reply(
                "remind me every sunday at noon for lunch",
                "remind me every sunday at noon for lunch",
                task.id)
            self.assertIsNotNone(r)
            self.assertIn("lunch", r.content)
            self.assertEqual(
                sup.scheduler.rows[-1]["kind"], "weekly")
            sup.scheduler.rows.clear()
            # 'at 3 for a bit' — a vague bound still isn't a task.
            r = agent._reminder_reply(
                "remind me at 3 for a bit",
                "remind me at 3 for a bit", task.id)
            self.assertIsNotNone(r)
            self.assertIn("do what", r.content)
            self.assertFalse([
                s for s in sup.scheduler.rows
                if "a bit" in s["name"]])
            # 'later tonight' — 'later' hedges a real daypart;
            # bare 'later' stays a pin-fail.
            self.assertIsNotNone(
                agent._reminder_spec("later tonight", time.time()))
            self.assertIsNone(
                agent._reminder_spec("later", time.time()))
            # 'at 930' / 'at a quarter after 3' / 'at seven
            # tomorrow morning' — informal clock forms resolve;
            # 'every single day' and 'on the weekend' (once Sat).
            for when in ("at 930", "at 930pm", "at 1030",
                         "at a quarter after 3", "at a quarter to 5",
                         "at seven tomorrow morning",
                         "every single day",
                         "in a quarter of an hour"):
                self.assertIsNotNone(
                    agent._reminder_spec(when, time.time()), when)
            sup.scheduler.rows.clear()

            # 'don't remind me tomorrow' — a day-scoped skip:
            # once-rows on that date disable (real, reversible);
            # recurring rows can't skip one fire, honestly said.
            tom = time.localtime(time.time() + 86400)
            sup.scheduler.add(
                "Reminder: lunch", "once",
                at=time.mktime((tom.tm_year, tom.tm_mon,
                                tom.tm_mday, 9, 0, 0,
                                -1, -1, -1)))
            sup.scheduler.add(
                "Reminder: gym", "daily", hour=8)
            result = agent.run("don't remind me tomorrow",
                               mode="auto")
            self.assertIn("are off", result.content)
            lunch = [s for s in sup.scheduler.rows
                     if "lunch" in s["name"]][0]
            self.assertFalse(lunch["enabled"])
            gym = [s for s in sup.scheduler.rows
                   if "gym" in s["name"]][0]
            self.assertTrue(gym["enabled"])
            self.assertIn("recurring", result.content)
            sup.scheduler.rows.clear()
            # Empty pool → honest 'nothing active'.
            result = agent.run("don't remind me tomorrow",
                               mode="auto")
            self.assertIn("don't have any active reminders",
                          result.content)

            # 'remind me at 3pm and cancel the X reminder' — a
            # compound ask gets an honest one-at-a-time, never a
            # row named 'X and cancel…' nor a 'two times' claim.
            result = agent.run(
                "remind me at 3pm and cancel the pay rent "
                "reminder", mode="auto")
            self.assertIn("one thing at a time", result.content)
            self.assertFalse(sup.scheduler.rows)
            # 'and move on' / 'change my shirt' aren't schedule
            # ops — the verb needs a schedule noun to trigger.
            result = agent.run(
                "remind me to stretch and move on", mode="auto")
            self.assertIn("stretch and move on", result.content)
            sup.scheduler.rows.clear()

            # 'mute reminders tomorrow' / 'silence X on friday' —
            # a day after the toggle verb scopes the flip to rows
            # actually firing that date; a miss says so honestly.
            sup.scheduler.add(
                "Reminder: lunch", "once",
                at=time.mktime((tom.tm_year, tom.tm_mon,
                                tom.tm_mday, 9, 0, 0,
                                -1, -1, -1)))
            _lt = time.localtime(time.time())
            _df = (4 - _lt.tm_wday) % 7 or 7
            sup.scheduler.add(
                "Reminder: pay rent", "once",
                at=time.mktime((_lt.tm_year, _lt.tm_mon,
                                _lt.tm_mday + _df, 10, 0, 0,
                                -1, -1, -1)),
                action={"kind": "notify", "detail": "rent"})
            result = agent.run("mute reminders tomorrow",
                               mode="auto")
            lunch = [s for s in sup.scheduler.rows
                     if "lunch" in s["name"]][0]
            rent = [s for s in sup.scheduler.rows
                    if "rent" in s["name"]][0]
            self.assertFalse(lunch["enabled"])
            self.assertTrue(rent["enabled"])
            result = agent.run("turn reminders on tomorrow",
                               mode="auto")
            lunch = [s for s in sup.scheduler.rows
                     if "lunch" in s["name"]][0]
            self.assertTrue(lunch["enabled"])
            result = agent.run(
                "silence the pay rent reminder on monday",
                mode="auto")
            self.assertIn("isn't set for monday", result.content)
            rent = [s for s in sup.scheduler.rows
                    if "rent" in s["name"]][0]
            self.assertTrue(rent["enabled"])
            sup.scheduler.rows.clear()

            # 'every now and then' / 'every so often' — vague
            # cadences pin-fail honestly, never narrated.
            result = agent.run(
                "remind me every now and then to stretch",
                mode="auto")
            self.assertIn("couldn't pin", result.content)
            self.assertFalse(sup.scheduler.rows)
            result = agent.run(
                "remind me every so often to breathe",
                mode="auto")
            self.assertIn("couldn't pin", result.content)
            self.assertFalse(sup.scheduler.rows)

            # 'unremind me about rent' — a negated remind verb is
            # a cancel; it resolves the row and removes it for
            # real, never a model-narrated miss.
            sup.scheduler.add(
                "Reminder: pay rent", "once",
                at=time.time() + 86400,
                action={"kind": "notify", "detail": "rent"})
            result = agent.run("unremind me about rent",
                               mode="auto")
            self.assertIn("Cancelled", result.content)
            self.assertFalse(sup.scheduler.rows)

            # 'the 15th of the month' — 'of the month' is the same
            # monthly intent as 'of each month'; with a task it
            # makes a real monthly row, without it asks honestly.
            result = agent.run(
                "remind me the 15th of the month to pay dues",
                mode="auto")
            dues = [s for s in sup.scheduler.rows
                    if "dues" in s["name"]][0]
            self.assertEqual(dues["kind"], "monthly")
            self.assertEqual(dues["day"], 15)
            result = agent.run(
                "remind me on the 5th of the month", mode="auto")
            self.assertIn("do what", result.content)
            sup.scheduler.rows.clear()

            # 'in 2 days at 3pm' — one anchor, not two times:
            # the duration picks the date, the clock its time.
            result = agent.run(
                "remind me in 2 days at 3pm to submit",
                mode="auto")
            sub = [s for s in sup.scheduler.rows
                   if "submit" in s["name"]][0]
            self.assertEqual(sub["kind"], "once")
            lt = time.localtime(sub["at"])
            self.assertEqual((lt.tm_hour, lt.tm_min), (15, 0))
            sup.scheduler.rows.clear()

            # 'tomorrow at noon' — the NOTASK dangling-connector
            # strip once ate the 'on' inside 'noon' (\s* matched
            # zero whitespace), pin-failing a good anchor.
            result = agent.run(
                "nudge me tomorrow at noon", mode="auto")
            nudge = [s for s in sup.scheduler.rows
                     if "nudge" in s["name"]][0]
            lt = time.localtime(nudge["at"])
            self.assertEqual((lt.tm_hour, lt.tm_min), (12, 0))
            # 'put off'/'hold off' — postponement verbs join the
            # snooze lane; the model fabricated 'it's not active'
            # over a live row.
            sup.scheduler.add(
                "Reminder: rent", "once",
                at=time.time() + 5 * 86400,
                action={"kind": "notify", "detail": "rent"})
            result = agent.run(
                "put off the rent reminder until monday",
                mode="auto")
            self.assertIn("Snoozed", result.content)
            sup.scheduler.rows.clear()

            # 'turn off the reminder i set for friday' — the
            # relative clause is scope, not topic; the model
            # narrated 'no record of one' over a live row.
            _lt2 = time.localtime(time.time())
            _df2 = (4 - _lt2.tm_wday) % 7 or 7
            sup.scheduler.add(
                "Reminder: rent", "once",
                at=time.mktime((_lt2.tm_year, _lt2.tm_mon,
                                _lt2.tm_mday + _df2, 10, 0, 0,
                                -1, -1, -1)),
                action={"kind": "notify", "detail": "rent"})
            result = agent.run(
                "turn off the reminder i set for friday",
                mode="auto")
            rent = [s for s in sup.scheduler.rows
                    if "rent" in s["name"]][0]
            self.assertFalse(rent["enabled"])
            result = agent.run(
                "turn off the reminder i set for sunday",
                mode="auto")
            self.assertIn("Nothing fires sunday", result.content)
            sup.scheduler.rows.clear()

            # 'every monday at 8 and every friday at 9' — two
            # cadences can't share a row; no 'every monday at 8'
            # task name gets banked.
            result = agent.run(
                "remind me every monday at 8 and every friday "
                "at 9", mode="auto")
            self.assertFalse(sup.scheduler.rows)

            # 'move pay rent to X' — a row-hitting move/change is
            # a reschedule; a miss like 'move the table' falls
            # through to the action lanes (and 'move it'/'change
            # it' stay generic asks).
            sup.scheduler.add(
                "Reminder: rent", "once",
                at=time.time() + 86400,
                action={"kind": "notify", "detail": "rent"})
            result = agent.run(
                "move pay rent to friday at 2pm", mode="auto")
            self.assertIn("Rescheduled", result.content)
            task = agent.tasks.create("x", "auto")
            self.assertIsNone(agent._reminder_reply(
                "move the table to the garage",
                "move the table to the garage", task.id))
            sup.scheduler.rows.clear()
            # 'set the X reminder for T instead' banked a garbled
            # fact ('Got it — pay is rent reminder…') live — it's a
            # reschedule on a row hit; 'set a reminder for T' stays
            # a create ask. 'switch X to Y' joins too.
            sup.scheduler.add(
                "reminder: pay rent", "once",
                at=time.time() + 86400,
                action={"kind": "notify", "detail": "pay rent"})
            result = agent.run(
                "set the pay rent reminder for 9am instead",
                mode="auto")
            self.assertIn("Rescheduled", result.content)
            self.assertFalse(
                agent.conversation_memory.recall_facts("pay"))
            result = agent.run(
                "switch the pay rent reminder to sunday",
                mode="auto")
            self.assertIn("Rescheduled", result.content)
            task = agent.tasks.create("x", "auto")
            ask = agent._reminder_reply(
                "set a reminder for 9am",
                "set a reminder for 9am", task.id)
            self.assertIsNotNone(ask)
            self.assertIn("do what", ask.content)
            sup.scheduler.rows.clear()
            # 'every fortnight' — pin-failed; resolves a 14d
            # interval like 'fortnightly'.
            spec = agent._reminder_spec(
                "every fortnight", time.time())
            self.assertIsNotNone(spec)
            self.assertEqual(spec["kind"], "interval")
            # 'in 2 and a half hours' — the 'and a half' → :30
            # clock normalization fired inside the duration and
            # made 'in 2:30 hours'; guarded for 'in'-lead strings.
            self.assertIsNotNone(
                agent._reminder_spec(
                    "in 2 and a half hours", time.time()))
            self.assertIsNotNone(
                agent._reminder_spec("at 8 and a half", time.time()))
            # 'at noon today' / 'today at noon' — 'today' joins
            # 'tomorrow' as a day anchor in both positions.
            self.assertIsNotNone(
                agent._reminder_spec("at noon today", time.time()))
            self.assertIsNotNone(
                agent._reminder_spec("today at 3", time.time()))
            # 'ten til 4' — worded minutes-til like 'quarter to';
            # 'before <clock>' is a deadline pin like 'by';
            # 'the day after tomorrow' is a date anchor, not an
            # event trigger.
            self.assertIsNotNone(
                agent._reminder_spec("at ten til 4", time.time()))
            self.assertIsNotNone(
                agent._reminder_spec("before 8pm", time.time()))
            self.assertIsNotNone(
                agent._reminder_spec("before noon", time.time()))
            self.assertIsNotNone(
                agent._reminder_spec(
                    "the day after tomorrow", time.time()))
            self.assertIsNone(
                agent._reminder_spec(
                    "remind me before the meeting", time.time()))
            # 'remind me about the rent' — a connector lead makes
            # the whole when-slot the task; ask for the time like
            # 'remind me to X', never drop to the model.
            result = agent.run(
                "remind me about the rent", mode="auto")
            self.assertIn("When should I remind you", result.content)
            # 'at the crack of dawn' — 'dawn' landed in the task
            # slot and the turn fell to the model; the rejoin
            # covers clock-phrase heads (crack/end/top/…).
            result = agent.run(
                "remind me at the crack of dawn", mode="auto")
            self.assertIn("couldn't pin a time", result.content)
            # 'i have a dentist appointment tomorrow at 2' — a
            # declarative appointment is a create; the model
            # narrated a fake 'say confirm' flow over zero rows.
            n0 = len(sup.scheduler.rows)
            result = agent.run(
                "i have a dentist appointment tomorrow at 2",
                mode="auto")
            self.assertIn("Reminder set", result.content)
            self.assertEqual(len(sup.scheduler.rows), n0 + 1)
            self.assertIn("dentist appointment",
                          sup.scheduler.rows[-1]["name"])
            sup.scheduler.rows.pop()
            # 'snooze X til friday' — 'til' joins until/till.
            sup.scheduler.add(
                "Reminder: rent", "once",
                at=time.time() + 86400,
                action={"kind": "notify", "detail": "rent"})
            result = agent.run(
                "snooze the rent reminder til friday", mode="auto")
            self.assertIn("Snoozed", result.content)
            sup.scheduler.rows.clear()
            # 'every third day' — an ordinal picks every Nth unit.
            spec = agent._reminder_spec(
                "every third day", time.time())
            self.assertIsNotNone(spec)
            self.assertEqual(spec["kind"], "interval")
            self.assertEqual(spec["interval_s"], 259200.0)
            # Worded snooze deltas — 'push back X two days' asked
            # 'until when?' live; worded numbers map like digits.
            sup.scheduler.add(
                "reminder: nap", "once",
                at=time.time() + 86400,
                action={"kind": "notify", "detail": "nap"})
            before = float(sup.scheduler.rows[-1]["at"])
            result = agent.run(
                "push back the nap reminder two days", mode="auto")
            self.assertIn("Snoozed", result.content)
            self.assertAlmostEqual(
                float(sup.scheduler.rows[-1]["at"]),
                before + 172800.0, delta=5)
            # 'mute everything until tuesday' hit the VOICE-mute
            # lane live — 'everything' is an all-scope reminder
            # noun too; a timed mute schedules the re-enable
            # nudge like 'silence until monday'.
            sup.scheduler.rows.clear()
            sup.scheduler.add(
                "reminder: rent", "once",
                at=time.time() + 86400,
                action={"kind": "notify", "detail": "rent"})
            result = agent.run(
                "mute everything until tuesday", mode="auto")
            self.assertIn("Turned off", result.content)
            self.assertFalse(sup.scheduler.rows[0]["enabled"])
            # 'monday wednesday friday at 8' — a bare weekday list
            # is the same weekly cadence as the 'and'-joined one.
            spec = agent._reminder_spec(
                "monday wednesday friday at 8", time.time())
            self.assertIsNotNone(spec)
            self.assertEqual(spec["kind"], "weekly")
            self.assertEqual(spec["weekday"], [0, 2, 4])

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

    def test_reminder_toggle_resume_verbs_and_scoped_state(self):
        # Grammar sweep: 'bring back'/'restore'/'reactivate' fell to
        # the model (TOGGLE_RE lacked the resume family); 'turn off
        # the timer' over a disabled timer claimed 'no active
        # reminders' while other rows stayed on — the noun-scoped
        # pool was already-off, not empty.
        class _Sched:
            def __init__(self):
                self.rows = []
                self._n = 0

            def add(self, name, kind, **kw):
                self._n += 1
                row = {"id": f"sc-{self._n}", "name": name,
                       "kind": kind, "enabled": kw.pop("enabled", True),
                       **kw}
                self.rows.append(row)
                return row

            def list(self):
                return list(self.rows)

            def remove(self, sid):
                n = len(self.rows)
                self.rows = [r for r in self.rows
                             if r.get("id") != sid]
                return len(self.rows) != n

            def set_enabled(self, sid, en):
                for r in self.rows:
                    if r["id"] == sid:
                        r["enabled"] = en
                        return True
                return False

            def update(self, sid, **kw):
                for r in self.rows:
                    if r["id"] == sid:
                        r.update(kw)
                        return r

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
            sup.scheduler.add(
                "Reminder: build", "once", at=time.time() + 86400,
                action={"kind": "notify", "detail": "build"},
                enabled=False)
            for verb in ("bring back", "restore", "reactivate",
                         "put back", "renew", "revive"):
                sup.scheduler.rows[0]["enabled"] = False
                r = agent.run(f"{verb} the build reminder",
                              mode="auto")
                self.assertIn("Turned on", r.content, verb)
                self.assertTrue(sup.scheduler.rows[0]["enabled"],
                                verb)
            # Noun-scoped already-off — 'turn off the timer' over a
            # disabled timer says 'already off', never 'no active
            # reminders' while other rows stay on.
            sup.scheduler.add(
                "Timer: timer", "once", at=time.time() + 600,
                action={"kind": "notify", "detail": "timer"},
                enabled=False)
            r = agent.run("turn off the timer", mode="auto")
            self.assertIn("already off", r.content)
            self.assertNotIn("no active", r.content)
            # Whole-pool already-off — every noun-scoped row in the
            # target state reads 'already off', not 'no active'.
            sup.scheduler.rows[0]["enabled"] = False
            r = agent.run("turn off all my reminders", mode="auto")
            self.assertIn("already off", r.content)
            self.assertNotIn("no active", r.content)
            # Labels strip every schedule-noun prefix, not just
            # 'reminder:' — a server-created 'Timer: timer' row
            # names 'timer' in the reply.
            r = agent.run("cancel the timer", mode="auto")
            self.assertIn("“timer”", r.content)
            self.assertNotIn("Timer:", r.content)
            # Scoped state asks — 'is my build reminder on' answers
            # the row's enabled flag, never 'no reminder' over a
            # disabled row and never a model guess (soak: the model
            # replied 'No pay rent reminder found' over a live row).
            r = agent.run("is my build reminder on", mode="auto")
            self.assertIn("build", r.content)
            self.assertIn("off", r.content)
            self.assertNotIn("no reminder", r.content.lower())
            sup.scheduler.rows[0]["enabled"] = True
            r = agent.run("is the build reminder enabled",
                          mode="auto")
            self.assertIn("build", r.content)
            self.assertIn("on", r.content)
            # Unscoped state ask — count the pool incl. disabled.
            sup.scheduler.rows[0]["enabled"] = False
            r = agent.run("are my reminders on", mode="auto")
            self.assertIn("of", r.content)
            # Scoped miss stays honest.
            r = agent.run("is my dentist reminder on", mode="auto")
            self.assertIn("no reminder", r.content.lower())
            # Recall-miss topic drops a trailing copula — 'your
            # editor', not 'your editor is'.
            r = agent.run("remind me what my editor is",
                          mode="auto")
            self.assertIn("your editor", r.content)
            self.assertNotIn("editor is", r.content)
            # Noun-less determiner toggles — 'unpause the standup'
            # names the row without a schedule noun; the lane
            # claims on a row hit and falls through on a miss so
            # 'pause the movie'/'stop the car' stay action turns.
            sup.scheduler.rows[0]["enabled"] = False
            r = agent.run("unpause the build", mode="auto")
            self.assertIn("build", r.content)
            self.assertTrue(sup.scheduler.rows[0]["enabled"])
            sup.scheduler.rows[0]["enabled"] = True
            r = agent.run("pause the build", mode="auto")
            self.assertFalse(sup.scheduler.rows[0]["enabled"])
            t = agent.tasks.create("x", "auto")
            for q in ("pause the movie", "stop the car",
                      "mute the tv"):
                self.assertIsNone(
                    agent._reminder_reply(q, q, t.id), q)
            # 'set my alarm for 7' — the DECL article group lacked
            # 'my'/'the'; the create fell through to the model.
            for q in ("set my alarm for 7", "set the alarm for 8"):
                r = agent.run(q, mode="auto")
                self.assertIn("Alarm set", r.content, q)
            # Noun-less turn/switch claim only WITH a direction —
            # 'switch the pay rent reminder to sunday' is a
            # reschedule, not a toggle clarify; 'turn the oven'
            # (direction-less) is not the lane's call.
            sup.scheduler.rows[0]["enabled"] = False
            for q in ("turn the build back on",
                      "switch the build on"):
                sup.scheduler.rows[0]["enabled"] = False
                r = agent.run(q, mode="auto")
                self.assertTrue(sup.scheduler.rows[0]["enabled"], q)
            for q in ("turn the build off",
                      "switch the build off"):
                sup.scheduler.rows[0]["enabled"] = True
                r = agent.run(q, mode="auto")
                self.assertFalse(sup.scheduler.rows[0]["enabled"], q)
            sup.scheduler.rows[0]["enabled"] = False
            r = agent.run("activate the build", mode="auto")
            self.assertTrue(sup.scheduler.rows[0]["enabled"])
            sup.scheduler.rows[0]["enabled"] = True
            r = agent.run("deactivate the build", mode="auto")
            self.assertFalse(sup.scheduler.rows[0]["enabled"])
            t = agent.tasks.create("x", "auto")
            self.assertIsNone(
                agent._reminder_reply(
                    "turn the oven", "turn the oven", t.id))
            # 'make sure i remember the dentist at 3' — the 'make'
            # bare-reschedule verb stole the HELP-idiom create and
            # returned None on the row miss; a None reschedule now
            # falls through so the idiom still creates a row.
            sup.scheduler.rows.clear()
            r = agent.run(
                "make sure i remember the dentist at 3",
                mode="auto")
            self.assertIn("Reminder set", r.content)
            self.assertTrue(
                any("dentist" in str(s.get("name", ""))
                    for s in sup.scheduler.rows))
            sup.scheduler.add(
                "reminder: pay rent", "once",
                at=time.time() + 86400,
                action={"kind": "notify", "detail": "pay rent"})
            r = agent.run(
                "switch the pay rent reminder to sunday",
                mode="auto")
            self.assertIn("Rescheduled", r.content)
            # Disabled rows are visible to mutation and state asks —
            # 'push the build reminder back an hour' over a
            # switched-off row reported "no reminder matching
            # 'build'" live. Mutations land + disclose the off
            # state; a when-ask names the row instead of missing.
            sup.scheduler.rows[-1]["enabled"] = False
            sup.scheduler.rows[-1]["kind"] = "once"
            sup.scheduler.rows[-1]["at"] = time.time() + 3600
            r = agent.run(
                "snooze the pay rent reminder 30 minutes",
                mode="auto")
            self.assertIn("Snoozed", r.content)
            self.assertIn("won't fire", r.content)
            r = agent.run(
                "reschedule the pay rent reminder to 7pm",
                mode="auto")
            self.assertIn("Rescheduled", r.content)
            self.assertIn("won't fire", r.content)
            r = agent.run(
                "when does the pay rent reminder fire",
                mode="auto")
            self.assertIn("is off", r.content)
            self.assertNotIn("No reminder", r.content)
            # Ordinal selectors pick extreme-firing rows across
            # every mutation lane — 'the first one'/'the latest
            # one' are selection, not topic terms.
            sup.scheduler.rows.clear()
            sup.scheduler.add(
                "reminder: standup", "daily", hour=9, minute=0,
                next_run=time.time() + 3600,
                action={"kind": "notify", "detail": "standup"})
            sup.scheduler.add(
                "reminder: stretch", "daily", hour=17, minute=30,
                next_run=time.time() + 7200,
                action={"kind": "notify", "detail": "stretch"})
            r = agent.run(
                "reschedule the first one to 5pm", mode="auto")
            self.assertIn("Rescheduled", r.content)
            self.assertEqual(sup.scheduler.rows[0]["hour"], 17)
            r = agent.run(
                "when is my last reminder", mode="auto")
            self.assertIn("last reminder", r.content)
            self.assertIn("stretch", r.content)
            r = agent.run(
                "cancel the earliest reminder", mode="auto")
            self.assertIn("Cancel", r.content)
            self.assertEqual(
                [s["name"] for s in sup.scheduler.rows],
                ["reminder: stretch"])
            # 'turn off the last one' — the direction leads the
            # body; the oven falls through to the action lanes.
            r = agent.run("turn off the last one", mode="auto")
            self.assertIn("off", r.content)
            self.assertFalse(sup.scheduler.rows[0]["enabled"])
            self.assertIsNone(agent._reminder_reply(
                "turn off the oven", "turn off the oven", t.id))
            # 'every other day at 9' — an interval row anchored to
            # the clock, not a pin-fail or a fabricated daily.
            r = agent.run(
                "remind me every other day at 9 to water plants",
                mode="auto")
            self.assertIn("Reminder set", r.content)
            self.assertIn("starting", r.content)
            row = sup.scheduler.rows[-1]
            self.assertEqual(row["kind"], "interval")
            self.assertEqual(row["interval_s"], 172800.0)
            self.assertEqual((row["hour"], row["minute"]), (9, 0))

    def test_clock_scoped_inventory_asks(self):
        # Grammar sweep: 'what happens at 9am', 'whats at 9am',
        # 'do i have anything at noon' returned None — the INV_LEAD
        # grammar had no clock/day-scoped alternative, so the asks
        # fell to the model which invents schedule state. The hay
        # already carries clock/day tokens; the claim was the gap.
        class _Sched:
            def __init__(self):
                self.rows = []
                self._n = 0

            def add(self, name, kind, **kw):
                self._n += 1
                row = {"id": f"sc-{self._n}", "name": name,
                       "kind": kind, "enabled": kw.pop("enabled", True),
                       **kw}
                self.rows.append(row)
                return row

            def list(self):
                return list(self.rows)

        class _Sup:
            def __init__(self):
                self.scheduler = _Sched()

        import datetime
        def _at(hour):
            # Always today's date — the 'tonight' window assertion
            # must hold at any wall-clock time the suite runs.
            return datetime.datetime.now().replace(
                hour=hour, minute=0, second=0,
                microsecond=0).timestamp()

        from localcodeagent.workflow.conversation_memory import (
            ConversationMemory)
        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(Path(td), _FakeRuntime())
            agent.conversation_memory = ConversationMemory(
                Path(td) / "cm.json")
            sup = _Sup()
            agent._autonomy = lambda: sup
            sup.scheduler.add(
                "Reminder: standup", "daily", at=_at(9),
                action={"kind": "notify", "detail": "standup"})
            sup.scheduler.add(
                "Reminder: stretch", "daily", at=_at(15),
                action={"kind": "notify", "detail": "stretch"})
            # Clock scope matches the row's fire-time, not topic
            # text — the 9am ask names 'standup', never 'stretch'.
            for q, want in (("what happens at 9am", "standup"),
                            ("whats at 9am", "standup"),
                            ("anything at 3pm", "stretch"),
                            ("whats at 3pm", "stretch"),
                            ("whats happening at 9", "standup"),
                            ("what am i doing at 9", "standup"),
                            ("what have i got at 9", "standup"),
                            ("any meetings at 9am", "standup"),
                            ("whats on at 3pm", "stretch")):
                r = agent.run(q, mode="auto")
                self.assertIn(want, r.content, q)
            r = agent.run("what happens at 9am", mode="auto")
            self.assertNotIn("stretch", r.content)
            # Clock/day misses stay honest — report the miss and
            # the real pool, never 'no reminders set'.
            for q in ("do i have anything at noon",
                      "what happens at noon",
                      "what do i have at 8pm"):
                r = agent.run(q, mode="auto")
                self.assertIn("no reminder", r.content.lower(), q)
                self.assertIn("standup", r.content, q)
            # 'whats on friday' — daily rows DO fire Friday;
            # the fire-day window must name them, not miss.
            r = agent.run("whats on friday", mode="auto")
            self.assertIn("standup", r.content)
            self.assertIn("stretch", r.content)
            # 'is anything on for tonight' scopes to TODAY's fire
            # window — the 'for' connector must not leak into the
            # scope terms ('no reminder about "for"'). Whichever
            # rows fire today must be listed.
            r = agent.run("is anything on for tonight", mode="auto")
            self.assertIn("for today", r.content)
            self.assertNotIn("“for”", r.content)
            # Loose noun + clock scope widens past the noun
            # filter — 'the 9am alarm' over a 'Reminder: standup'
            # row IS that row; 'no reminder about "9am"' was a
            # false miss. No-scope noun asks stay noun-strict.
            for q, want in (("did the 9am alarm go off", "standup"),
                            ("is the 9am alarm still set", "standup"),
                            ("did anything happen at 9", "standup"),
                            ("what did i miss at 9", "standup"),
                            ("did i miss the 9am", "standup")):
                r = agent.run(q, mode="auto")
                self.assertIn(want, r.content, q)
            r = agent.run("did the timer go off", mode="auto")
            self.assertIn("No timers set", r.content)
            r = agent.run("what happened at 9", mode="auto")
            self.assertIn("standup", r.content)
            r = agent.run("was i supposed to do something at 9",
                          mode="auto")
            self.assertIn("standup", r.content)
            # Non-scheduler asks must not claim — 'do i have
            # anything to do' / 'whats the weather' aren't inventory.
            t = agent.tasks.create("x", "auto")
            for q in ("what did i miss",
                      "was i supposed to call him",
                      "do i have anything to do",
                      "whats the weather like",
                      "what do i have for dinner",
                      "whats happening",
                      "what is the time",
                      "is anything on tv",
                      "whats at chapter 5",
                      "any meetings"):
                self.assertIsNone(
                    agent._reminder_reply(q, q, t.id), q)

    def test_push_repeat_again_forms(self):
        # Grammar sweep: 'push the standup to 10am' fell to the
        # model (push/bump/shift weren't reschedule verbs); 'push
        # to origin' is a git command and must never even gate-
        # pass. 'remind me again about X' garbled the task into
        # 'again of the X'; 'repeat the X reminder' had no claim.
        class _Sched:
            def __init__(self):
                self.rows = []
                self._n = 0

            def add(self, name, kind, **kw):
                self._n += 1
                row = {"id": f"sc-{self._n}", "name": name,
                       "kind": kind, "enabled": kw.pop("enabled", True),
                       **kw}
                self.rows.append(row)
                return row

            def list(self):
                return list(self.rows)

            def update(self, sid, **kw):
                for r in self.rows:
                    if r["id"] == sid:
                        r.update(kw)
                        return r

        class _Sup:
            def __init__(self):
                self.scheduler = _Sched()

        import datetime
        def _at(hour):
            now = datetime.datetime.now()
            t = now.replace(hour=hour, minute=0, second=0,
                            microsecond=0)
            if t <= now:
                t += datetime.timedelta(days=1)
            return t.timestamp()

        from localcodeagent.workflow.conversation_memory import (
            ConversationMemory)
        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(Path(td), _FakeRuntime())
            agent.conversation_memory = ConversationMemory(
                Path(td) / "cm.json")
            sup = _Sup()
            agent._autonomy = lambda: sup
            sup.scheduler.add(
                "Reminder: standup", "daily", at=_at(9),
                action={"kind": "notify", "detail": "standup"})
            # Real cadence-preserving reschedule — kind stays
            # 'daily', the clock moves to 10:00.
            r = agent.run("push the standup to 10am", mode="auto")
            self.assertIn("standup", r.content)
            row = sup.scheduler.rows[0]
            self.assertEqual("daily", row["kind"])
            self.assertEqual(10, row["hour"])
            # Git/deploy pushes never claim — connector-lead
            # forms can't even match the pattern (gate stays
            # False); bare-noun forms match but the handler
            # declines them on the row miss.
            for q in ("push to origin", "push to main",
                      "bump the version", "shift the release"):
                self.assertIsNone(
                    agent._reminder_reply(q, q, "t"), q)
            for q in ("push to origin", "push to main"):
                self.assertFalse(
                    AgentOrchestrator.can_answer_locally(q), q)
            # Re-fire asks normalize onto the create grammar —
            # 'again' is stripped, 'repeat' needs the noun.
            r = agent.run("remind me again about the standup",
                          mode="auto")
            self.assertIn("standup", r.content.lower())
            self.assertNotIn("again", r.content.lower())
            r = agent.run("repeat the standup reminder", mode="auto")
            self.assertIn("standup", r.content)
            r = agent.run("remind me again at 3pm to call",
                          mode="auto")
            self.assertIn("15:00", r.content)
            t = agent.tasks.create("x", "auto")
            for q in ("repeat the command", "repeat the message"):
                self.assertIsNone(
                    agent._reminder_reply(q, q, t.id), q)
            # 'how much longer till' is a countdown ask — answers
            # the soonest row's real next_run delta, whichever row
            # that is at the suite's wall-clock time.
            r = agent.run("how much longer till my reminder",
                          mode="auto")
            self.assertIn("next reminder is", r.content)
            self.assertIn(" in ", r.content)
            # Noun-less countdown claims — bare 'how much longer'
            # reached the model before the countdown leads joined
            # the inventory shape; scoped countdowns ('how long
            # until X') count down to THAT row and miss honestly.
            r = agent.run("how much longer", mode="auto")
            self.assertIn("next reminder is", r.content)
            self.assertIn(" in ", r.content)
            r = agent.run("how long until the call",
                          mode="auto")
            self.assertIn("call", r.content)
            r = agent.run("how long until the movie ends",
                          mode="auto")
            self.assertIn("No reminder", r.content)
            # A recurring row with no next_run counts down to its
            # next cadence occurrence, not '0 minutes'.
            sup.scheduler.add(
                "reminder: standup", "daily", hour=9, minute=0,
                action={"kind": "notify", "detail": "standup"})
            r = agent.run("how much longer till the standup",
                          mode="auto")
            self.assertIn("standup", r.content)
            self.assertNotIn(" in 0 minutes", r.content)

    def test_nounless_fired_and_cadence_asks(self):
        # Grammar sweep: 'has the standup fired', 'did the standup
        # already go off', 'does the standup repeat', 'is the
        # standup daily' all fell to the model — the fired/cadence
        # grammars required the reminder noun. Noun-less claims
        # resolve the name against rows and decline honestly on a
        # miss ('does the song repeat' can't narrate a cadence).
        # 'move the standup to 11' claimed 'Rescheduled' while a
        # bare-digit when kept the old hour — a fake mutation.
        class _Sched:
            def __init__(self):
                self.rows = []
                self._n = 0

            def add(self, name, kind, **kw):
                self._n += 1
                row = {"id": f"sc-{self._n}", "name": name,
                       "kind": kind,
                       "enabled": kw.pop("enabled", True), **kw}
                self.rows.append(row)
                return row

            def list(self):
                return list(self.rows)

            def update(self, sid, **kw):
                for r in self.rows:
                    if r["id"] == sid:
                        r.update(kw)
                        return r

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
            sup.scheduler.add(
                "Reminder: standup", "daily", hour=9, minute=0,
                at=time.time() + 3600,
                action={"kind": "notify", "detail": "standup"})
            # Noun-less fired asks answer last_run honestly.
            for q in ("has the standup fired",
                      "did the standup already go off",
                      "did the standup go off",
                      "did the standup already fire"):
                r = agent.run(q, mode="auto")
                self.assertIn("Not yet", r.content, q)
                self.assertIn("standup", r.content, q)
            # Noun-less cadence asks answer the row's kind.
            for q in ("does the standup repeat",
                      "does the standup reminder repeat",
                      "is the standup recurring",
                      "is the standup daily",
                      "when does the standup repeat",
                      "how often is the standup"):
                r = agent.run(q, mode="auto")
                self.assertIn("every day", r.content, q)
            # Scoped misses name real rows — never narrate cadence.
            for q in ("does the song repeat",
                      "is the report daily",
                      "how often is the train"):
                r = agent.run(q, mode="auto")
                self.assertIn("No reminder about", r.content, q)
                self.assertIn("standup", r.content, q)
            # 'remind myself' is the same create ask.
            r = agent.run("remind myself at 5 to leave",
                          mode="auto")
            self.assertIn("17:00", r.content)
            self.assertEqual(2, len(sup.scheduler.rows))
            # Bare-digit when retimes the cadence — 'to 11' is a
            # clock, and the reply can't claim a reschedule that
            # didn't happen.
            r = agent.run("move the standup to 11", mode="auto")
            self.assertIn("Rescheduled", r.content)
            row = sup.scheduler.rows[0]
            self.assertEqual("daily", row["kind"])
            self.assertEqual(11, row["hour"])
            # A duration target is a one-shot — like a full date —
            # not a silent cadence keep.
            r = agent.run("move the standup to in 30 minutes",
                          mode="auto")
            row = sup.scheduler.rows[0]
            self.assertEqual("once", row["kind"])
            # Generic questions still don't claim.
            t = agent.tasks.create("x", "auto")
            for q in ("did something happen", "did it happen",
                      "did the toast pop up",
                      "when does the train come"):
                self.assertIsNone(
                    agent._reminder_reply(q, q, t.id), q)
            # Gate parity — locally handled forms must pass the
            # model-down gate.
            from localcodeagent.agent.orchestrator import (
                AgentOrchestrator)
            for q in ("has the standup fired",
                      "did the standup already go off",
                      "does the standup repeat",
                      "is the standup daily",
                      "when does the standup repeat",
                      "how often is the standup",
                      "remind myself at 5 to leave"):
                self.assertTrue(
                    AgentOrchestrator.can_answer_locally(q), q)

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

    def test_agent_complaint_files_repair_incident(self):
        # A complaint about the agent's own surface takes the
        # bug-report lane: real report_failure intake, honest ack —
        # no model wait.
        class _Repair:
            def __init__(self):
                self.calls = []

            def report_failure(self, **kw):
                self.calls.append(kw)
                return ({"id": "ri-9", "state": "detected"}, "new")

        class _Sup:
            def __init__(self):
                self.repair = _Repair()

        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(Path(td), _FakeRuntime())
            sup = _Sup()
            agent._autonomy = lambda: sup
            for text in ("the api keeps timing out",
                         "the ui freezes every time i open it",
                         "nexus is too slow today",
                         "the api times out after 30 seconds",
                         "the reminders didn't fire this morning",
                         "the voice output is distorted",
                         "my reminders are not working",
                         "image generation keeps failing",
                         "you're too slow",
                         "this thing is broken"):
                sup.repair.calls.clear()
                r = agent.run(text, mode="chat")
                self.assertIn("logged", r.content)
                self.assertEqual(len(sup.repair.calls), 1, text)
                self.assertEqual(
                    sup.repair.calls[0]["source"], "user_report")
                # Model-down is exactly when failures get reported —
                # the server readiness gate must let the lane through.
                self.assertTrue(
                    AgentOrchestrator.can_run_without_coding_model(
                        text), text)
            for text in ("my code keeps crashing",
                         "the build is not working"):
                self.assertFalse(
                    AgentOrchestrator.can_run_without_coding_model(
                        text), text)

    def test_agent_complaint_recurrence_reports_recurred(self):
        class _Repair:
            def report_failure(self, **kw):
                return ({"id": "ri-9", "occurrences": 3}, "recurred")

        class _Sup:
            repair = _Repair()

        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(Path(td), _FakeRuntime())
            agent._autonomy = lambda: _Sup()
            r = agent.run("the api keeps timing out", mode="chat")
            self.assertIn("another occurrence", r.content)

    def test_complaint_without_intake_acks_without_claiming(self):
        # No repair intake → the ack must NOT claim it logged anything.
        class _Sup:
            repair = None

        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(Path(td), _FakeRuntime())
            agent._autonomy = lambda: _Sup()
            r = agent.run("the api keeps timing out", mode="chat")
            self.assertNotIn("logged", r.content)
            self.assertIn("can't file", r.content)

    def test_agent_complaint_survives_persona_veto(self):
        # A named persona vetoes canned small talk — but the bug-report
        # ack is a fact ("I logged X"), not style. Without the
        # exemption the ack is dropped and the turn falls to the model
        # lane — the ~180s stall seen live under a starved GPU.
        class _Repair:
            def __init__(self):
                self.calls = []

            def report_failure(self, **kw):
                self.calls.append(kw)
                return ({"id": "ri-9"}, "new")

        class _Sup:
            def __init__(self):
                self.repair = _Repair()

        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(Path(td), _FakeRuntime())
            sup = _Sup()
            agent._autonomy = lambda: sup
            agent._persona_active = lambda: True
            provider = _ScriptedProvider([{
                "role": "assistant", "content": "model should not run"}])
            agent._provider_for = lambda p, **kw: provider
            r = agent.run("the api keeps timing out", mode="chat")
            self.assertIn("logged", r.content)
            self.assertNotIn("model should not run", r.content)
            self.assertEqual(len(sup.repair.calls), 1)

    def test_git_verbs_never_claim_the_snooze_lane(self):
        # 'push to origin' parsed as snooze(verb=push, way=to,
        # dur='origin') — a repo command hijacked by the scheduler
        # lane and a false pass in the model-down gate. The bare
        # verbs push/bump/shift now require a reminder noun.
        for text in ("push to origin", "push to prod",
                     "bump the version", "shift the release"):
            self.assertIsNone(
                AgentOrchestrator._REMINDER_SNOOZE_RE.match(text),
                f"{text!r} must not claim the snooze lane")
        for text in ("push it to 4pm", "push the rent reminder to 4pm",
                     "bump my alarm an hour", "shift it forward"):
            self.assertIsNotNone(
                AgentOrchestrator._REMINDER_SNOOZE_RE.match(text),
                f"{text!r} should still snooze")
        self.assertFalse(
            AgentOrchestrator.can_answer_locally("push to origin"))
        # 'push it to origin' — pronoun + unparseable tail declines at
        # the handler so the git lane gets the turn.
        class _Sched:
            def list(self):
                return [{"id": "s1", "name": "reminder: x",
                         "enabled": True, "at": 9999999999.0,
                         "kind": "once"}]

        class _Sup:
            scheduler = _Sched()

        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(Path(td), _FakeRuntime())
            sup = _Sup()
            agent._autonomy = lambda: sup
            self.assertIsNone(agent._reminder_reply(
                "push it to origin", "push it to origin", "t"))

    def test_empty_state_asks_answer_from_ledger_not_model(self):
        # 'What did we settle on' with an empty decision ledger must
        # answer 'nothing on record' deterministically — punting to
        # the model lets it invent a settlement (or stall 180s on a
        # cold load — observed live).
        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(Path(td), _FakeRuntime())
            from localcodeagent.workflow.conversation_manager import (
                ConversationManager)
            agent.conversation_manager = ConversationManager(
                Path(td) / "cm.json")
            provider = _ScriptedProvider([{
                "role": "assistant", "content": "We chose Postgres."}])
            agent._provider_for = lambda p, **kw: provider
            r = agent.run("what did we settle on earlier", mode="chat")
            self.assertIn("nothing", r.content.lower())
            self.assertNotIn("Postgres", r.content)
            self.assertTrue(
                AgentOrchestrator.can_run_without_coding_model(
                    "what did we settle on earlier"))

    def test_world_complaint_stays_on_model_path(self):
        # 'my code keeps crashing' is a help-me request, not a product
        # complaint — the lane must not claim it or file an incident.
        class _Repair:
            def __init__(self):
                self.calls = []

            def report_failure(self, **kw):
                self.calls.append(kw)
                return ({"id": "ri-9"}, "new")

        class _Sup:
            def __init__(self):
                self.repair = _Repair()

        with tempfile.TemporaryDirectory() as td:
            agent = self._agent(Path(td), _FakeRuntime())
            sup = _Sup()
            agent._autonomy = lambda: sup
            provider = _ScriptedProvider([{
                "role": "assistant",
                "content": "Tell me the error message."}] * 4)
            agent._provider_for = lambda p, **kw: provider
            for text in ("my code keeps crashing",
                         "the build is not working",
                         "my python script segfaults",
                         "i'm not working today"):
                agent.run(text, mode="chat")
            self.assertEqual(sup.repair.calls, [])


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
