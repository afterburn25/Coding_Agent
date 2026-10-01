from __future__ import annotations

import http.server
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path


class _FakeModelServer:
    """Minimal OpenAI-compatible endpoint scripting a two-turn tool loop.

    First call (no tool-role message yet) requests `system_resources`; once a
    tool result lands in the conversation it answers with final content. Both
    stream and non-stream payloads are handled so the real orchestrator code
    path is exercised end to end — real HTTP, real tool execution, real
    transcript — without needing a GPU or llama.cpp.
    """

    def __init__(self) -> None:
        outer = self
        self.requests: list[dict] = []
        self.fail_next = 0  # when >0, respond 500 to that many POSTs

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                outer.requests.append(body)
                if self.path == "/v1/chat/completions" and outer.fail_next > 0:
                    outer.fail_next -= 1
                    self.send_response(500)
                    self.end_headers()
                    return
                if self.path != "/v1/chat/completions":
                    self.send_response(404)
                    self.end_headers()
                    return
                messages = body.get("messages") or []
                saw_tool = any(m.get("role") == "tool" for m in messages)
                if saw_tool:
                    message = {"role": "assistant", "content": "Resource check complete — all healthy."}
                    chunks = ["Resource check complete", " — all healthy."]
                else:
                    message = {
                        "role": "assistant", "content": "",
                        "tool_calls": [{
                            "id": "call_1", "type": "function",
                            "function": {"name": "system_resources", "arguments": "{}"},
                        }],
                    }
                    chunks = None

                if body.get("stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    if chunks:
                        for piece in chunks:
                            delta = {"choices": [{"index": 0, "delta": {"role": "assistant", "content": piece}}]}
                            self.wfile.write(f"data: {json.dumps(delta)}\n\n".encode())
                    else:
                        for tc in message["tool_calls"]:
                            delta = {"choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [
                                {"index": 0, "id": tc["id"], "type": "function",
                                 "function": {"name": tc["function"]["name"], "arguments": "{}"}}]}}]}
                            self.wfile.write(f"data: {json.dumps(delta)}\n\n".encode())
                    self.wfile.write(b"data: {\"choices\": [{\"index\": 0, \"delta\": {}, \"finish_reason\": \"stop\"}], \"usage\": {\"prompt_tokens\": 12, \"completion_tokens\": 4}}\n\n")
                    self.wfile.write(b"data: [DONE]\n\n")
                    return
                payload = {"choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
                           "usage": {"prompt_tokens": 12, "completion_tokens": 4}}
                raw = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                raw = b'{"status": "ok"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *a):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class EndToEndAgentTests(unittest.TestCase):
    def _state(self, td: str, endpoint: str):
        from localcodeagent.config import AgentConfig, ModelProfile
        from localcodeagent.server import AppState
        cfg = AgentConfig(
            models=[ModelProfile(
                id="fake", endpoint=endpoint, model="fake-model",
                roles=["primary_coder", "utility", "fast_coder"], runtime="external")],
            process_watchdog=False,
        )
        return AppState(cfg, Path(td), Path(td) / ".runtime")

    def test_task_runs_real_tool_call_and_persists_transcript(self):
        fake = _FakeModelServer()
        self.addCleanup(fake.close)
        with tempfile.TemporaryDirectory() as td:
            state = self._state(td, fake.endpoint)
            events: list[dict] = []
            result = state.agent.run("check system resources", event_callback=events.append)

            self.assertEqual(result.task.get("status"), "completed")
            # Two model calls: tool_call request, then final answer after the
            # tool result message.
            self.assertEqual(len(fake.requests), 2)
            tool_msg = [m for m in fake.requests[1]["messages"] if m.get("role") == "tool"]
            self.assertTrue(tool_msg, "second call must include the tool result")
            self.assertIn("system_resources", json.dumps(tool_msg))

            types = [e.get("type") for e in events]
            self.assertIn("tool_start", types)
            self.assertIn("tool", types)
            self.assertIn("task", types)

            log = state.tasks.read_log(result.task["id"])
            self.assertIn("$ system_resources", log)
            self.assertIn("system_resources ok", log)
            self.assertIn("## result", log)
            self.assertIn("Resource check complete", log)

    def test_queued_tasks_drain_through_real_pipeline(self):
        fake = _FakeModelServer()
        self.addCleanup(fake.close)
        with tempfile.TemporaryDirectory() as td:
            state = self._state(td, fake.endpoint)
            state.queue.enqueue("first queued task")
            state.queue.enqueue("second queued task")
            state._dequeue_next()

            deadline = time.time() + 20
            while len(state.queue) and time.time() < deadline:
                time.sleep(0.1)
            # Wait for the chained worker to finish the second item.
            deadline = time.time() + 10
            while True:
                recent = state.tasks.recent(5)
                done = [t for t in recent if t.get("status") == "completed"]
                if len(done) >= 2 or time.time() >= deadline:
                    break
                time.sleep(0.1)
            done = [t for t in state.tasks.recent(5) if t.get("status") == "completed"]
            self.assertEqual(len(state.queue), 0)
            self.assertEqual(len(done), 2)
            # Each task used two model calls (tool turn + answer turn).
            self.assertEqual(len(fake.requests), 4)

    def test_transient_model_failure_recovers(self):
        fake = _FakeModelServer()
        self.addCleanup(fake.close)
        with tempfile.TemporaryDirectory() as td:
            state = self._state(td, fake.endpoint)
            state.config.runtime_recovery_attempts = 2
            state.agent.config.runtime_recovery_attempts = 2
            fake.fail_next = 1  # first chat request 500s; recovery retries
            events: list[dict] = []
            result = state.agent.run("recover from a transient error", event_callback=events.append)

            self.assertEqual(result.task.get("status"), "completed")
            # 1 failed + 2 successful turns.
            self.assertEqual(len(fake.requests), 3)
            model_kinds = [e["event"]["type"] for e in events
                           if e.get("type") == "model" and isinstance(e.get("event"), dict)]
            self.assertIn("transient_retry", model_kinds)

    def test_persistent_model_failure_fails_task(self):
        fake = _FakeModelServer()
        self.addCleanup(fake.close)
        with tempfile.TemporaryDirectory() as td:
            state = self._state(td, fake.endpoint)
            state.config.runtime_recovery_attempts = 1
            state.agent.config.runtime_recovery_attempts = 1
            fake.fail_next = 50  # model never recovers
            with self.assertRaises(RuntimeError):
                state.agent.run("this will keep failing")

            task = state.tasks.recent(1)[0]
            self.assertEqual(task.get("status"), "error")
            self.assertTrue(task.get("error"))
            log = state.tasks.read_log(task["id"])
            self.assertIn("## error", log)
            # Calls are bounded: 1 initial + 1 recovery attempt, not infinite.
            self.assertLessEqual(len(fake.requests), 4)


if __name__ == "__main__":
    unittest.main()
