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
        self.assertFalse(is_transport_failure(
            urllib.error.HTTPError("u", 500, "err", {}, None)))

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
        config = AgentConfig(
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
                       side_effect=fake_urlopen):
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
                       side_effect=dead_urlopen):
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
            manager = RuntimeManager(AgentConfig(models=[profile]),
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
            manager = RuntimeManager(AgentConfig(models=[profile]),
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


class DiagnosticsEndpointTests(unittest.TestCase):
    def test_diagnostics_endpoint_reports_failures_and_health(self):
        import threading
        from localcodeagent.server import create_server, stop_state

        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / "web").mkdir()
            cfg = AgentConfig(
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
                data = json.loads(urllib.request.urlopen(
                    f"{base}/api/diagnostics", timeout=10).read())
                from localcodeagent.version import version as _ver
                self.assertEqual(data["version"], _ver())
                self.assertEqual(data["models"][0]["id"], "fake")
                self.assertTrue(data["recent_failures"])
                self.assertEqual(data["recent_failures"][-1]["kind"],
                                 "connection_reset")
                self.assertIn("hardware", data)
            finally:
                server.shutdown()
                server.server_close()
                stop_state(state)


if __name__ == "__main__":
    unittest.main()
