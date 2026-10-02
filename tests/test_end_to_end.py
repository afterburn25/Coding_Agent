from __future__ import annotations

import http.server
import json
import tempfile
import threading
import time
import unittest
import urllib.request
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
        self.delay = 0.0    # seconds to stall each chat response
        self.tool_name = "system_resources"
        self.tool_args = "{}"
        self.tool_calls: list[tuple[str, str]] | None = None  # multi-call batch

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
                if outer.delay:
                    time.sleep(outer.delay)
                messages = body.get("messages") or []
                saw_tool = any(
                    m.get("role") == "tool"
                    or "Recovered pending tool action" in str(m.get("content", ""))
                    for m in messages
                )
                if saw_tool:
                    message = {"role": "assistant", "content": "Resource check complete — all healthy."}
                    chunks = ["Resource check complete", " — all healthy."]
                else:
                    calls = outer.tool_calls or [(outer.tool_name, outer.tool_args)]
                    message = {
                        "role": "assistant", "content": "",
                        "tool_calls": [{
                            "id": f"call_{i}", "type": "function",
                            "function": {"name": n, "arguments": a},
                        } for i, (n, a) in enumerate(calls)],
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
                        for i, tc in enumerate(message["tool_calls"]):
                            delta = {"choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [
                                {"index": i, "id": tc["id"], "type": "function",
                                 "function": {"name": tc["function"]["name"],
                                              "arguments": tc["function"]["arguments"]}}]}}]}
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
            # Auto-research would try a real outbound GitHub request with a
            # ~15s timeout on every coding prompt — orthogonal to what these
            # tests exercise and the reason each run stalls ~15s.
            research_enabled=False,
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
            # The tool ran and its result line was recorded (state is
            # ok/failed/timeout depending on probe speed under load).
            self.assertIn(" system_resources ", log.replace("$", " "))
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

            deadline = time.time() + 30
            while len(state.queue) and time.time() < deadline:
                time.sleep(0.1)
            # Wait for the chained worker to finish the second item.
            deadline = time.time() + 30
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
            # Queue workers write .agent/ bookkeeping after the status flips to
            # completed — join them before the tempdir teardown or rmtree races
            # a late file create ("Directory not empty: '.agent'").
            deadline = time.time() + 10
            while time.time() < deadline:
                workers = [
                    t for t in threading.enumerate()
                    if t.name.startswith("queue-") and t.is_alive()
                ]
                if not workers:
                    break
                time.sleep(0.05)

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

    def test_task_cancelled_mid_run(self):
        fake = _FakeModelServer()
        self.addCleanup(fake.close)
        with tempfile.TemporaryDirectory() as td:
            state = self._state(td, fake.endpoint)
            fake.delay = 0.6  # slow first response -> cancel window
            outcome: list = []

            def worker():
                try:
                    outcome.append(state.agent.run("cancel me"))
                except Exception as exc:
                    outcome.append(exc)

            t = threading.Thread(target=worker, daemon=True)
            t.start()
            # Wait for the task to exist, then cancel it while the model call
            # is still stalled on the fake endpoint.
            deadline = time.time() + 5
            task = None
            while time.time() < deadline:
                recent = state.tasks.recent(1)
                if recent and recent[0].get("status") == "running":
                    task = recent[0]
                    break
                time.sleep(0.05)
            self.assertIsNotNone(task)
            state.tasks.update(task["id"], status="cancelled")
            t.join(timeout=15)
            self.assertFalse(t.is_alive(), "run() did not return after cancel")
            result = outcome[0]
            self.assertEqual(result.task.get("status"), "cancelled")

    def test_approval_pause_and_resume_end_to_end(self):
        fake = _FakeModelServer()
        self.addCleanup(fake.close)
        with tempfile.TemporaryDirectory() as td:
            state = self._state(td, fake.endpoint)
            # filesystem.write defaults to "ask" — the run pauses for approval.
            fake.tool_name = "write_file"
            fake.tool_args = json.dumps({"path": "note.txt", "content": "hello nexus"})
            result = state.agent.run("write a note file")

            self.assertEqual(result.task.get("status"), "waiting_approval")
            self.assertTrue(result.pending_approval)
            # Paused before the follow-up model call.
            self.assertEqual(len(fake.requests), 1)
            self.assertFalse((Path(td) / "note.txt").exists())

            resumed = state.agent.resume(result.task["id"], approved=True)
            self.assertEqual(resumed.task.get("status"), "completed")
            self.assertEqual((Path(td) / "note.txt").read_text(encoding="utf-8"), "hello nexus")
            # Resume executed the tool and sent its result back to the model.
            self.assertGreaterEqual(len(fake.requests), 2)
            tool_msg = [m for m in fake.requests[1]["messages"] if m.get("role") == "tool"]
            self.assertIn("WROTE", json.dumps(tool_msg))

            log = state.tasks.read_log(result.task["id"])
            self.assertIn("## approval", log)

    def test_approval_denial_reaches_model_end_to_end(self):
        fake = _FakeModelServer()
        self.addCleanup(fake.close)
        with tempfile.TemporaryDirectory() as td:
            state = self._state(td, fake.endpoint)
            fake.tool_name = "write_file"
            fake.tool_args = json.dumps({"path": "note.txt", "content": "hello nexus"})
            result = state.agent.run("write a note file")
            self.assertEqual(result.task.get("status"), "waiting_approval")

            resumed = state.agent.resume(result.task["id"], approved=False)
            self.assertEqual(resumed.task.get("status"), "completed")
            self.assertFalse((Path(td) / "note.txt").exists())
            tool_msg = [m for m in fake.requests[1]["messages"] if m.get("role") == "tool"]
            self.assertIn("PERMISSION_DENIED", json.dumps(tool_msg))

    def test_batched_tool_calls_execute_sequentially(self):
        fake = _FakeModelServer()
        self.addCleanup(fake.close)
        with tempfile.TemporaryDirectory() as td:
            state = self._state(td, fake.endpoint)
            fake.tool_calls = [
                ("system_resources", "{}"),
                ("list_files", "{}"),
            ]
            result = state.agent.run("inspect the workspace")

            self.assertEqual(result.task.get("status"), "completed")
            # Both calls ran before the follow-up model turn.
            self.assertEqual(len(fake.requests), 2)
            tool_msgs = [m for m in fake.requests[1]["messages"] if m.get("role") == "tool"]
            self.assertEqual(len(tool_msgs), 2)
            log = state.tasks.read_log(result.task["id"])
            self.assertIn("$ system_resources", log)
            self.assertIn("$ list_files", log)

    def test_pending_approval_survives_restart(self):
        fake = _FakeModelServer()
        self.addCleanup(fake.close)
        with tempfile.TemporaryDirectory() as td:
            state = self._state(td, fake.endpoint)
            fake.tool_name = "write_file"
            fake.tool_args = json.dumps({"path": "note.txt", "content": "survived restart"})
            result = state.agent.run("write a note file")
            self.assertEqual(result.task.get("status"), "waiting_approval")
            task_id = result.task["id"]

            # Simulate a backend restart: brand-new AppState on the same
            # workspace — the live session is gone, the ledger persists.
            state2 = self._state(td, fake.endpoint)
            resumed = state2.agent.resume(task_id, approved=True)

            self.assertEqual(resumed.task.get("status"), "completed")
            self.assertEqual(
                (Path(td) / "note.txt").read_text(encoding="utf-8"),
                "survived restart")
            # The executed tool result reached the model via the recovered
            # system note (rebuilt context has no dangling tool_calls).
            recovery = [m for m in fake.requests[1]["messages"]
                        if "Recovered pending tool action" in str(m.get("content", ""))]
            self.assertIn("WROTE", json.dumps(recovery))

    def test_mini_soak_mixed_outcomes(self):
        """Queue several tasks while the endpoint alternately succeeds and
        fails — every task must reach a terminal state and the queue must
        drain, which is the 'run all night' invariant."""
        fake = _FakeModelServer()
        self.addCleanup(fake.close)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            state = self._state(td, fake.endpoint)
            state.config.runtime_recovery_attempts = 1
            state.agent.config.runtime_recovery_attempts = 1
            for i in range(6):
                state.queue.enqueue(f"soak task {i}")
            state._dequeue_next()

            deadline = time.time() + 90
            while time.time() < deadline:
                terminal = {"completed", "error", "cancelled"}
                recent = state.tasks.recent(10)
                done = [t for t in recent if t.get("status") in terminal]
                if len(done) >= 6 and not len(state.queue):
                    break
                # Inject a transient blip mid-run: some tasks see a 500 first.
                if len(fake.requests) % 3 == 0:
                    fake.fail_next = 1
                time.sleep(0.2)
            self.assertEqual(len(state.queue), 0)
            done = [t for t in state.tasks.recent(10) if t.get("status") in {"completed", "error", "cancelled"}]
            self.assertEqual(len(done), 6)
            for t in done:
                log = state.tasks.read_log(t["id"])
                self.assertTrue(log.strip(), f"task {t['id']} wrote no transcript")

    def test_full_stack_sse_stream_end_to_end(self):
        """Real HTTP server + SSE + event bus + fake model — the exact path
        the desktop UI drives."""
        fake = _FakeModelServer()
        self.addCleanup(fake.close)
        # Daemon threads may still be flushing state during teardown;
        # Windows holds directory locks briefly.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            from localcodeagent.config import AgentConfig, ModelProfile
            from localcodeagent.server import create_server, stop_state
            ws = Path(td)
            cfg = AgentConfig(
                models=[ModelProfile(
                    id="fake", endpoint=fake.endpoint, model="fake-model",
                    roles=["primary_coder", "utility", "fast_coder"],
                    runtime="external")],
                process_watchdog=False,
                research_enabled=False,
                autonomy_enabled=False,
            )
            server, state = create_server(cfg, ws, "127.0.0.1", 0, ws / "web", ws / ".runtime")
            t = threading.Thread(target=server.serve_forever, daemon=True)
            t.start()
            self.addCleanup(lambda: (server.shutdown(), server.server_close(), stop_state(state)))
            base = f"http://127.0.0.1:{server.server_address[1]}"
            fake.delay = 1.5  # long enough that a post-selection heartbeat fires

            req = urllib.request.Request(
                f"{base}/api/chat/stream",
                data=json.dumps({
                    "message": "Inspect the workspace files and check system resources, then report what tools you used",
                    "mode": "auto"}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            events: list[tuple[str, dict]] = []
            with urllib.request.urlopen(req, timeout=60) as resp:
                self.assertIn("text/event-stream", resp.headers.get("Content-Type", ""))
                buf = ""
                for raw in resp:
                    buf += raw.decode("utf-8", errors="replace")
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        line = line.strip()
                        if line.startswith("event:"):
                            events.append((line[6:].strip(), {}))
                        elif line.startswith("data:") and events:
                            try:
                                events[-1] = (events[-1][0], json.loads(line[5:].strip()))
                            except json.JSONDecodeError:
                                pass

            kinds = [k for k, _ in events]
            self.assertIn("tool_start", kinds)
            self.assertIn("tool", kinds)
            self.assertIn("result", kinds)
            done = [d for k, d in events if k == "task" and isinstance(d.get("task"), dict)
                    and d["task"].get("status") == "completed"]
            self.assertTrue(done, "no completed task event on the stream")
            # Heartbeats must name the model once routing selected it. On a
            # fast runner the task may finish before any post-selection
            # heartbeat — only assert on heartbeats emitted after selection.
            sel_idx = next((i for i, (k, d) in enumerate(events)
                            if k == "model" and isinstance(d.get("event"), dict)
                            and d["event"].get("type") == "selected"), None)
            self.assertIsNotNone(sel_idx, "no model selection event on the stream")
            post = [d for k, d in events[sel_idx + 1:] if k == "heartbeat"]
            for h in post:
                self.assertEqual(h.get("model_id"), "fake",
                                 "post-selection heartbeat lost the model id")

            # The API ledger reflects the same completed task.
            with urllib.request.urlopen(f"{base}/api/tasks", timeout=10) as resp:
                tasks = json.loads(resp.read())
            rows = tasks.get("recent") or tasks.get("tasks") or []
            self.assertTrue(any(t.get("status") == "completed" for t in rows))

    def test_events_bus_streams_live_task_events(self):
        """The shared /api/events bus — the browser's EventSource path —
        must deliver parseable task/tool frames while a task runs."""
        fake = _FakeModelServer()
        self.addCleanup(fake.close)
        # Daemon threads may still be flushing state during teardown;
        # Windows holds directory locks briefly.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            from localcodeagent.config import AgentConfig, ModelProfile
            from localcodeagent.server import create_server, stop_state
            ws = Path(td)
            cfg = AgentConfig(
                models=[ModelProfile(
                    id="fake", endpoint=fake.endpoint, model="fake-model",
                    roles=["primary_coder", "utility", "fast_coder"],
                    runtime="external")],
                process_watchdog=False,
                research_enabled=False,
                autonomy_enabled=False,
            )
            server, state = create_server(cfg, ws, "127.0.0.1", 0, ws / "web", ws / ".runtime")
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(lambda: (server.shutdown(), server.server_close(), stop_state(state)))
            base = f"http://127.0.0.1:{server.server_address[1]}"

            bus_events: list[tuple[str, dict]] = []
            bus_ready = threading.Event()

            def subscribe():
                req = urllib.request.Request(f"{base}/api/events?replay=0")
                with urllib.request.urlopen(req, timeout=60) as resp:
                    bus_ready.set()
                    buf = ""
                    deadline = time.time() + 60
                    while time.time() < deadline:
                        chunk = resp.read1(4096)
                        if not chunk:
                            break
                        buf += chunk.decode("utf-8", errors="replace")
                        while "\n" in buf:
                            line, buf = buf.split("\n", 1)
                            line = line.strip()
                            if line.startswith("event:"):
                                bus_events.append((line[6:].strip(), {}))
                            elif line.startswith("data:") and bus_events:
                                try:
                                    bus_events[-1] = (bus_events[-1][0],
                                                      json.loads(line[5:].strip()))
                                except json.JSONDecodeError:
                                    pass
                        if any(k == "task" and isinstance(d.get("task"), dict)
                               and d["task"].get("status") == "completed"
                               for k, d in bus_events):
                            break

            sub = threading.Thread(target=subscribe, daemon=True)
            sub.start()
            self.assertTrue(bus_ready.wait(10))
            time.sleep(0.2)  # let the subscription register before the task

            # Production path: queue worker wires event_callback=_bus_emit.
            state.queue.enqueue("inspect the workspace and fix the failing tests")
            state._dequeue_next()
            sub.join(timeout=60)

            kinds = [k for k, _ in bus_events]
            self.assertIn("task", kinds)
            self.assertIn("tool_start", kinds)
            self.assertTrue(
                any(k == "task" and isinstance(d.get("task"), dict)
                    and d["task"].get("status") == "completed"
                    for k, d in bus_events),
                "bus never delivered the completed task event")


    def test_mcp_servers_register_as_processes(self):
        with tempfile.TemporaryDirectory() as td:
            from localcodeagent.config import AgentConfig, ModelProfile
            from localcodeagent.server import AppState
            cfg = AgentConfig(
                models=[ModelProfile(
                    id="fake", endpoint="http://127.0.0.1:1/v1",
                    model="fake-model", roles=["primary_coder"],
                    runtime="external")],
                process_watchdog=False,
                autonomy_enabled=False,
                research_enabled=False,
                mcp_servers=[{
                    "id": "testsrv", "name": "Test MCP",
                    "command": ["python", "-c", "pass"],
                    "auto_start": False,
                }])
            state = AppState(cfg, Path(td), Path(td) / ".runtime")
            try:
                svc = state.processes.get("mcp:testsrv")
                self.assertIsNotNone(svc)
                self.assertEqual(svc.kind, "mcp_server")
                row = svc.describe()
                self.assertEqual(row["id"], "testsrv")
                self.assertEqual(row["state"], "disconnected")
                ids = state.processes.service_ids(prefix="mcp:")
                self.assertEqual(ids, ["mcp:testsrv"])
                self.assertEqual(state._probe_mcp(), "degraded")
                self.assertEqual(state.health.check("mcp"), "degraded")
            finally:
                state.mcp.shutdown()


class AutonomyApiTests(unittest.TestCase):
    """Autonomy CRUD endpoints — triggers expose the signal vocabulary for
    the UI form, and enable/disable/delete actually mutate the store."""

    def test_trigger_crud_and_signals(self):
        with tempfile.TemporaryDirectory() as td:
            from localcodeagent.config import AgentConfig, ModelProfile
            from localcodeagent.server import create_server, stop_state
            ws = Path(td)
            cfg = AgentConfig(
                models=[ModelProfile(
                    id="fake", endpoint="http://127.0.0.1:1/v1",
                    model="fake-model", roles=["primary_coder"],
                    runtime="external")],
                process_watchdog=False, research_enabled=False,
                autonomy_enabled=False,
            )
            server, state = create_server(
                cfg, ws, "127.0.0.1", 0, ws / "web", ws / ".runtime")
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(lambda: (server.shutdown(),
                                     server.server_close(),
                                     stop_state(state)))
            base = f"http://127.0.0.1:{server.server_address[1]}"

            def post(path, body):
                req = urllib.request.Request(
                    base + path, data=json.dumps(body).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST")
                return json.loads(urllib.request.urlopen(req, timeout=10).read())

            # Signals vocabulary is exposed so the UI can build the event
            # picker without hardcoding.
            data = json.loads(urllib.request.urlopen(
                f"{base}/api/triggers", timeout=10).read())
            self.assertIn("file_changed", data["signals"])
            self.assertIn("ci_failed", data["signals"])

            out = post("/api/triggers", {
                "name": "watch build", "event": "file_changed",
                "watch": str(ws), "debounce_s": 5,
                "action": {"kind": "mission", "objective": "rebuild"}})
            self.assertTrue(out["ok"], out)
            tid = out["trigger"]["id"]

            out = post(f"/api/triggers/{tid}/disable", {})
            self.assertTrue(out["ok"])
            trig = next(t for t in json.loads(urllib.request.urlopen(
                f"{base}/api/triggers", timeout=10).read())["triggers"]
                        if t["id"] == tid)
            self.assertFalse(trig["enabled"])

            out = post(f"/api/triggers/{tid}/delete", {})
            self.assertTrue(out["ok"])
            remaining = json.loads(urllib.request.urlopen(
                f"{base}/api/triggers", timeout=10).read())["triggers"]
            self.assertFalse(any(t["id"] == tid for t in remaining))

    def test_schedule_create_validates_kind(self):
        with tempfile.TemporaryDirectory() as td:
            from localcodeagent.config import AgentConfig, ModelProfile
            from localcodeagent.server import create_server, stop_state
            ws = Path(td)
            cfg = AgentConfig(
                models=[ModelProfile(
                    id="fake", endpoint="http://127.0.0.1:1/v1",
                    model="fake-model", roles=["primary_coder"],
                    runtime="external")],
                process_watchdog=False, research_enabled=False,
                autonomy_enabled=False,
            )
            server, state = create_server(
                cfg, ws, "127.0.0.1", 0, ws / "web", ws / ".runtime")
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(lambda: (server.shutdown(),
                                     server.server_close(),
                                     stop_state(state)))
            base = f"http://127.0.0.1:{server.server_address[1]}"

            req = urllib.request.Request(
                f"{base}/api/schedules",
                data=json.dumps({"name": "bad", "kind": "fortnightly",
                                 "action": {"kind": "mission",
                                            "objective": "x"}}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            try:
                urllib.request.urlopen(req, timeout=10)
                self.fail("unknown kind accepted")
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 400)


class ChatAttachmentTests(unittest.TestCase):
    """Chat '+' attachments: text files inline into the model's user
    message; images persist locally and reach the image lane as source
    paths. Task titles/routing/memory see only the bare prompt."""

    def _state(self, td: str, endpoint: str):
        from localcodeagent.config import AgentConfig, ModelProfile
        from localcodeagent.server import AppState
        cfg = AgentConfig(
            models=[ModelProfile(
                id="fake", endpoint=endpoint, model="fake-model",
                roles=["primary_coder", "utility", "fast_coder"],
                runtime="external")],
            process_watchdog=False, research_enabled=False,
        )
        return AppState(cfg, Path(td), Path(td) / ".runtime")

    def test_text_attachment_reaches_model_but_not_task_title(self):
        fake = _FakeModelServer()
        self.addCleanup(fake.close)
        fake.tool_calls = []  # answer directly, no tool calls
        with tempfile.TemporaryDirectory() as td:
            state = self._state(td, fake.endpoint)
            result = state.agent.run(
                "summarize this file",
                attachments=[{"name": "notes.txt", "kind": "file",
                              "content": "the secret code is ALBACORE-7"}],
            )
            self.assertEqual(result.task.get("status"), "completed")
            sent = json.dumps(fake.requests[-1]["messages"])
            self.assertIn("ALBACORE-7", sent)
            self.assertIn("notes.txt", sent)
            # Routing/title/memory see the bare prompt, not the file body.
            self.assertEqual(result.task.get("prompt"), "summarize this file")
            self.assertNotIn("ALBACORE-7", json.dumps(result.task))

    def test_image_attachment_saved_and_routed_to_image_lane(self):
        fake = _FakeModelServer()
        self.addCleanup(fake.close)
        png = ("data:image/png;base64," + __import__("base64").b64encode(
            b"\x89PNG\r\n\x1a\nfakeimagebytes").decode())
        with tempfile.TemporaryDirectory() as td:
            state = self._state(td, fake.endpoint)
            # Stub the actual generation; we only verify the source path
            # reaches the image tool arguments.
            captured = {}

            def fake_exec(name, args, **kw):
                captured["name"] = name
                captured["args"] = args
                return "IMAGE GENERATED"

            state.agent.tools.execute = fake_exec
            state.agent.tools.permission_for = (
                lambda name: ("image.generate", "allow"))
            result = state.agent.run(
                "generate an image of a cat",
                attachments=[{"name": "photo.png", "kind": "image",
                              "data_url": png}],
            )
            args = captured.get("args") or {}
            src = args.get("source_image", "")
            self.assertTrue(src, "image lane never received a source_image")
            self.assertTrue(Path(src).is_file())
            self.assertIn("data\\attachments", src.replace("/", "\\"))
            refs = args.get("reference_images")
            self.assertFalse(refs)

    def test_oversized_and_binary_attachments_bounded(self):
        fake = _FakeModelServer()
        self.addCleanup(fake.close)
        with tempfile.TemporaryDirectory() as td:
            state = self._state(td, fake.endpoint)
            out = state.agent._prepare_attachments([
                {"name": "big.txt", "kind": "file",
                 "content": "x" * 500_000},
                {"name": "a.bin", "kind": "file"},  # no content
                {"name": "evil/../x.txt", "kind": "file", "content": "ok"},
            ])
            self.assertEqual(len(out["blocks"]), 2)
            self.assertLessEqual(len(out["blocks"][0]),
                                 state.agent._ATTACH_MAX_TEXT_PER_FILE + 64)
            self.assertTrue(any("a.bin" in n for n in out["notes"]))
            self.assertNotIn("..", out["blocks"][1].split("\n")[0])

    def test_server_clean_attachments_drops_malformed(self):
        from localcodeagent.server import _clean_attachments
        body = {"attachments": [
            {"name": "ok.txt", "kind": "file", "content": "hi"},
            {"name": "no-content"},                       # dropped
            "not-a-dict",                                  # dropped
            {"name": "img.png", "kind": "image",
             "data_url": "data:image/png;base64,AAA="},
            {"name": "bad.png", "kind": "image",
             "data_url": "not-a-data-url"},                # dropped
        ]}
        out = _clean_attachments(body)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["name"], "ok.txt")
        self.assertEqual(out[1]["kind"], "image")

    def test_empty_message_with_attachment_accepted(self):
        from localcodeagent.server import _clean_attachments
        body = {"message": "", "attachments": [
            {"name": "f.txt", "kind": "file", "content": "data"}]}
        self.assertTrue(_clean_attachments(body))


if __name__ == "__main__":
    unittest.main()
