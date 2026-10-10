from __future__ import annotations

import http.server
import itertools
import json
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path


class _RetriedTemporaryDirectory(tempfile.TemporaryDirectory):
    """Windows-safe variant for these tests: per-call sqlite connections
    (answer_memory `-wal`) can still be mid-flush on a queue worker thread
    when the with-block exits, and deleting an open file is a hard
    PermissionError there (ignore_cleanup_errors does not cover the
    _resetperms chmod path). The handle closes within milliseconds, so a
    short retry makes teardown deterministic."""

    def cleanup(self) -> None:
        for _ in range(40):
            try:
                return super().cleanup()
            except PermissionError:
                time.sleep(0.05)
        return super().cleanup()


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
        self.static_reply: str | None = None  # when set, answer with prose only
        # The canned tool-result answer gets a unique serial each call —
        # two queued tasks returning byte-identical prose IS the parrot
        # signature, and the anti-parrot gate correctly discards+retries
        # it, adding model calls beyond the two-turn loop this fixture is
        # meant to script.
        self._answer_seq = itertools.count(1)

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
                if outer.static_reply is not None:
                    message = {"role": "assistant", "content": outer.static_reply}
                    chunks = [outer.static_reply]
                elif saw_tool:
                    n = next(outer._answer_seq)
                    message = {"role": "assistant",
                               "content": f"Resource check complete — all healthy. (check {n})"}
                    chunks = ["Resource check complete", " — all healthy.", f" (check {n})"]
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
        cfg = AgentConfig(profiles_onboarding_gate=False, 
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

    def test_mission_node_ok_maps_real_task_statuses(self):
        # Regression: the mission executor mapped ok={"done","reverted"} —
        # "done" isn't a ledger status, so every successful agent node
        # reported ok=false and wedged its dependents.
        from localcodeagent.server import _task_status_succeeded
        self.assertTrue(_task_status_succeeded("completed"))
        self.assertTrue(_task_status_succeeded("completed_with_warnings"))
        self.assertTrue(_task_status_succeeded("reverted"))
        for st in ("error", "failed", "step_limit", "cancelled",
                   "waiting_approval", "running", "interrupted", ""):
            self.assertFalse(_task_status_succeeded(st), st)

    def test_mission_run_strips_persona_but_keeps_tools(self):
        # Regression: mission agent nodes inherited the interactive persona
        # ("Father, I've completed…") — small models narrated work in-character
        # instead of emitting tool calls, so every soak mission fabricated.
        # A mission run must drop the persona/personal-memory block yet still
        # transmit tool schemas.
        fake = _FakeModelServer()
        self.addCleanup(fake.close)
        with _RetriedTemporaryDirectory(ignore_cleanup_errors=True) as td:
            state = self._state(td, fake.endpoint)
            state.agent.profile_context = (
                lambda *a, **k: "You are Isabella. Address the user as Father.")
            if state.agent.conversation_manager is not None:
                state.agent.conversation_manager.personality_prompt = (
                    lambda: "PERSONA_SENTINEL")
            result = state.agent.run(
                "create a file named hello.txt containing one line: hi",
                event_callback=lambda e: None,
                mission_id="m-persona-test")
            self.assertTrue(fake.requests)
            payload = fake.requests[0]
            system_text = "\n".join(
                str(m.get("content") or "")
                for m in payload.get("messages") or []
                if m.get("role") == "system")
            self.assertNotIn("PERSONA_SENTINEL", system_text)
            self.assertNotIn("Isabella", system_text)
            self.assertNotIn("Father", system_text)
            # Tools must still be offered — the node was classified onto a
            # tool-capable lane, so schemas go out in the request.
            self.assertTrue(payload.get("tools"))

    def test_bundled_manifest_overrides_stale_on_disk_copy(self):
        # Regression: the install-time copy of tools/manifests under
        # runtime_root was never re-synced — a stale invokeai.json using
        # detect.files (all paths must exist, including a POSIX-only path)
        # reported a working InvokeAI venv as "not detected on disk" forever.
        # Bundled manifests ship with the binary and must win for shared ids.
        import sys
        fake = _FakeModelServer()
        self.addCleanup(fake.close)
        with _RetriedTemporaryDirectory(ignore_cleanup_errors=True) as td:
            root = Path(td)
            runtime_root = root / ".runtime"
            # Stale on-disk manifest: detect.files requires BOTH paths.
            disk_dir = runtime_root / "tools" / "manifests"
            disk_dir.mkdir(parents=True)
            stale = {
                "id": "invokeai", "name": "InvokeAI", "version": "6.x",
                "category": "images", "permissions": ["packages.install"],
                "install": {"method": "venv", "package": "invokeai",
                            "dest": "tools/InvokeAI"},
                "detect": {"files": ["tools/InvokeAI/Scripts/invokeai-web.exe",
                                     "tools/InvokeAI/bin/invokeai-web"]},
            }
            (disk_dir / "invokeai.json").write_text(
                json.dumps(stale), encoding="utf-8")
            # Bundled manifest (newer binary): files_any — either path is ok.
            bundle_dir = root / "bundle" / "tools" / "manifests"
            bundle_dir.mkdir(parents=True)
            fresh = dict(stale)
            fresh["detect"] = {"files_any": ["tools/InvokeAI/Scripts/invokeai-web.exe",
                                             "tools/InvokeAI/bin/invokeai-web"]}
            (bundle_dir / "invokeai.json").write_text(
                json.dumps(fresh), encoding="utf-8")
            # The venv's Windows entrypoint exists; the POSIX one never will.
            exe = runtime_root / "tools" / "InvokeAI" / "Scripts" / "invokeai-web.exe"
            exe.parent.mkdir(parents=True)
            exe.write_text("exe", encoding="utf-8")

            sys._MEIPASS = str(bundle_dir.parent.parent)
            try:
                state = self._state(td, fake.endpoint)
            finally:
                del sys._MEIPASS
            spec = state._tool_spec("invokeai")
            self.assertIsNotNone(spec)
            self.assertEqual(spec.install_status, "installed")

    def test_mission_park_does_not_block_lane(self):
        # A mission-attributed waiting_approval row is mission work — its own
        # approval flow resumes it — and must never freeze the agent lane
        # (an abandoned mission park used to block every future mission).
        with _RetriedTemporaryDirectory(ignore_cleanup_errors=True) as td:
            state = self._state(td, "http://127.0.0.1:9")
            mission_task = state.tasks.create("mission node work", "auto")
            state.tasks.update(
                mission_task.id, status="waiting_approval",
                phase="waiting_approval",
                pending_approval={"kind": "tool", "name": "run_shell",
                                  "arguments": {"command": "ls"}},
                mission_id="m-abc")
            self.assertFalse(
                state._agent_lane_active(include_waiting_approval=True))
            # An interactive park still outranks background missions.
            interactive = state.tasks.create("user work", "auto")
            state.tasks.update(
                interactive.id, status="waiting_approval",
                phase="waiting_approval",
                pending_approval={"kind": "tool", "name": "run_shell",
                                  "arguments": {"command": "ls"}})
            self.assertTrue(
                state._agent_lane_active(include_waiting_approval=True))

    def test_mission_owned_tasks_excluded_from_chat_recovery(self):
        # Mission-node tasks are supervisor-owned — node retries and replans
        # handle them. Chat-level auto-resume/error retry must not re-drive
        # them: the recorded node result is final, and re-driving an orphan
        # of a terminal mission just clogs the agent lane.
        with _RetriedTemporaryDirectory(ignore_cleanup_errors=True) as td:
            state = self._state(td, "http://127.0.0.1:9")

            class _Missions:
                def list(self, include_archived=True):
                    return [{"graph": {"nodes": [
                        {"result": {"task_id": "t-claimed"}},
                        {"result": {"ok": True}},
                    ]}}]

            class _Sup:
                missions = _Missions()

            state.autonomy = _Sup()
            owned = state._mission_owned_task_ids()
            self.assertIn("t-claimed", owned)
            self.assertNotIn("t-free", owned)

            state.autonomy = None
            self.assertEqual(state._mission_owned_task_ids(), set())

    def test_task_runs_real_tool_call_and_persists_transcript(self):
        fake = _FakeModelServer()
        self.addCleanup(fake.close)
        with _RetriedTemporaryDirectory(ignore_cleanup_errors=True) as td:
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
        with _RetriedTemporaryDirectory(ignore_cleanup_errors=True) as td:
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

    def test_waiting_approval_does_not_wedge_queue(self):
        # Regression: a task parked on an approval card holds no driver
        # thread — it must not block unrelated queued work (observed: a
        # stale waiting_approval row froze every queued prompt until the
        # user happened to resolve it).
        fake = _FakeModelServer()
        self.addCleanup(fake.close)
        with _RetriedTemporaryDirectory(ignore_cleanup_errors=True) as td:
            state = self._state(td, fake.endpoint)
            parked = state.tasks.create(
                prompt="parked approval from an earlier session",
                mode="auto")
            state.tasks.update(parked.id, status="waiting_approval",
                               phase="waiting_approval")
            state.queue.enqueue("queued while a card waits")
            state._dequeue_next()
            deadline = time.time() + 30
            while len(state.queue) and time.time() < deadline:
                time.sleep(0.1)
            self.assertEqual(len(state.queue), 0)
            parked = [t for t in state.tasks.recent(10)
                      if t.get("status") == "waiting_approval"]
            self.assertEqual(len(parked), 1)  # still parked, undisturbed
            workers = [t for t in threading.enumerate()
                       if t.name.startswith("queue-") and t.is_alive()]
            deadline = time.time() + 10
            while workers and time.time() < deadline:
                time.sleep(0.05)
                workers = [t for t in threading.enumerate()
                           if t.name.startswith("queue-") and t.is_alive()]

    def test_queued_chat_counts_as_interactive_lane_demand(self):
        # A user prompt waiting in the queue must register as interactive
        # lane demand — otherwise mission nodes keep dispatching while the
        # user's request sits starved behind background work.
        with _RetriedTemporaryDirectory(ignore_cleanup_errors=True) as td:
            state = self._state(td, "http://127.0.0.1:9")
            self.assertFalse(state._agent_lane_active())
            state.queue.enqueue("user question while missions run")
            self.assertTrue(state._agent_lane_active())
            # Mission-attributed queue items do NOT count as interactive
            # demand — the supervisor owns them and must keep working.
            state.queue.pop()
            state.autonomy._lane.mission_id = "m-test"
            try:
                state.queue.enqueue("mission subtask prompt")
            finally:
                state.autonomy._lane.mission_id = None
            self.assertFalse(state._agent_lane_active())

    def test_preempt_for_chat_cancels_mission_task_only(self):
        # Interactive chat preempts mission work holding the lane — but must
        # never cancel a foreground (non-mission) task.
        with _RetriedTemporaryDirectory(ignore_cleanup_errors=True) as td:
            state = self._state(td, "http://127.0.0.1:9")
            mission_task = state.tasks.create("mission node work", "auto")
            state.tasks.update(mission_task.id, mission_id="m-abc")
            self.assertTrue(state._preempt_for_chat(mission_task))
            self.assertEqual(
                state.tasks.get(mission_task.id).status, "cancelled")
            user_task = state.tasks.create("user work", "auto")
            self.assertFalse(state._preempt_for_chat(user_task))
            self.assertEqual(
                state.tasks.get(user_task.id).status, "running")

    def test_preempt_for_chat_finds_mission_beyond_current_row(self):
        # current() returns the newest row regardless of status — a newer
        # terminal row must not shield a mission task still holding the lane.
        with _RetriedTemporaryDirectory(ignore_cleanup_errors=True) as td:
            state = self._state(td, "http://127.0.0.1:9")
            mission_task = state.tasks.create("mission node work", "auto")
            state.tasks.update(mission_task.id, mission_id="m-abc")
            newer = state.tasks.create("finished user turn", "auto")
            state.tasks.update(newer.id, status="completed", phase="done")
            self.assertTrue(state._preempt_for_chat(newer))
            self.assertEqual(
                state.tasks.get(mission_task.id).status, "cancelled")

    def test_preempt_for_chat_cancel_survives_running_restamp(self):
        # Mid-drive stamps (verify/review/repair) write 'running'
        # unconditionally — a preempt must stay cancelled through them or
        # the drive silently runs to step_limit (observed live: the
        # preempted mission task ended 'step_limit', never 'cancelled').
        from types import SimpleNamespace
        with _RetriedTemporaryDirectory(ignore_cleanup_errors=True) as td:
            state = self._state(td, "http://127.0.0.1:9")
            mission_task = state.tasks.create("mission node work", "auto")
            state.tasks.update(mission_task.id, mission_id="m-abc")
            state._preempt_for_chat(mission_task)
            state.tasks.update(
                mission_task.id, status="running", phase="researching_failure")
            self.assertTrue(state.agent._task_cancelled(
                SimpleNamespace(task_id=mission_task.id)))

    def test_request_cancel_signals_inflight_command(self):
        # The per-command kill flag must be SET (not popped) so a running
        # subprocess aborts instead of running to completion.
        from types import SimpleNamespace
        with _RetriedTemporaryDirectory(ignore_cleanup_errors=True) as td:
            state = self._state(td, "http://127.0.0.1:9")
            task = state.tasks.create("long command", "auto")
            flag = threading.Event()
            state.agent.tools.context.setdefault(
                "command_cancel", {})[task.id] = flag
            state.agent.request_cancel(task.id, reason="test")
            self.assertTrue(flag.is_set())
            self.assertEqual(
                state.tasks.get(task.id).status, "cancelled")
            self.assertTrue(state.agent._task_cancelled(
                SimpleNamespace(task_id=task.id)))

    def test_queue_item_completed_carries_response_content(self):
        # The chat placeholder swap depends on a terminal bus event carrying
        # the queue item and the response content.
        fake = _FakeModelServer()
        self.addCleanup(fake.close)
        with _RetriedTemporaryDirectory(ignore_cleanup_errors=True) as td:
            state = self._state(td, fake.endpoint)
            bus = state.events.subscribe(replay=0)
            item = state.queue.enqueue("queued user question")
            state._dequeue_next()
            deadline = time.time() + 30
            completed = None
            while time.time() < deadline:
                try:
                    ev = bus.get(timeout=0.5)
                except Exception:
                    continue
                if (ev.get("type") == "task"
                        and ev.get("event") == "queue_item_completed"):
                    completed = ev
                    break
            self.assertIsNotNone(
                completed, "queue_item_completed must publish on the bus")
            self.assertEqual(completed["queue_item"]["id"], item["id"])
            self.assertTrue(completed["ok"])
            self.assertIn("Resource check complete", completed["content"])
            deadline = time.time() + 10
            while time.time() < deadline:
                workers = [
                    t for t in threading.enumerate()
                    if t.name.startswith("queue-") and t.is_alive()
                ]
                if not workers:
                    break
                time.sleep(0.05)

    def test_dequeue_prefers_user_items_over_mission_items(self):
        # When both a mission subtask and a user prompt wait in the queue,
        # the user prompt runs first even though it was enqueued later.
        fake = _FakeModelServer()
        self.addCleanup(fake.close)
        with _RetriedTemporaryDirectory(ignore_cleanup_errors=True) as td:
            state = self._state(td, fake.endpoint)
            state.autonomy._lane.mission_id = "m-test"
            try:
                state.queue.enqueue("mission subtask prompt")
            finally:
                state.autonomy._lane.mission_id = None
            state.queue.enqueue("user question")
            state._dequeue_next()
            deadline = time.time() + 30
            while len(state.queue) and time.time() < deadline:
                time.sleep(0.1)
            deadline = time.time() + 30
            while True:
                done = [t for t in state.tasks.recent(5)
                        if t.get("status") == "completed"]
                if len(done) >= 2 or time.time() >= deadline:
                    break
                time.sleep(0.1)
            self.assertEqual(len(state.queue), 0)
            self.assertEqual(len(done), 2)
            # recent() is newest-first — the user task ran first so it is
            # the OLDER of the two rows.
            self.assertEqual(done[1]["prompt"], "user question")
            self.assertEqual(done[0]["prompt"], "mission subtask prompt")
            deadline = time.time() + 10
            while time.time() < deadline:
                workers = [
                    t for t in threading.enumerate()
                    if t.name.startswith("queue-") and t.is_alive()
                ]
                if not workers:
                    break
                time.sleep(0.05)

    def test_mission_executor_never_opens_voice_lane(self):
        # Regression: mission node runs called _voice_begin → begin_task →
        # stop_all, which killed user-facing speech every time a background
        # node dispatched. Background work must not touch the voice lane.
        fake = _FakeModelServer()
        self.addCleanup(fake.close)
        with _RetriedTemporaryDirectory(ignore_cleanup_errors=True) as td:
            state = self._state(td, fake.endpoint)
            executor = getattr(getattr(state, "autonomy", None), "_executor", None)
            if executor is None:
                self.skipTest("autonomy executor unavailable")
            calls = {"begin": 0, "finish": 0}
            orig_begin = state._voice_begin
            orig_finish = state._voice_finish
            def spy_begin(*a, **k):
                calls["begin"] += 1
                return orig_begin(*a, **k)
            def spy_finish(*a, **k):
                calls["finish"] += 1
                return orig_finish(*a, **k)
            state._voice_begin = spy_begin
            state._voice_finish = spy_finish
            try:
                out = executor(
                    {"id": "m-1", "title": "m"},
                    {"instruction": "check system resources", "title": "n"},
                    lambda e: None)
            finally:
                state._voice_begin = orig_begin
                state._voice_finish = orig_finish
            self.assertTrue(out.get("ok"), out)
            self.assertEqual(calls["begin"], 0)
            self.assertEqual(calls["finish"], 0)

    def test_mission_agent_node_fails_on_unverified_claims(self):
        # Regression: an agent node whose reply asserted completed actions
        # with zero tool calls recorded ok=True — the mission advanced on a
        # fabrication until the artifact check caught it downstream. The
        # node itself must fail so retries get a shot at a real tool run.
        fake = _FakeModelServer()
        self.addCleanup(fake.close)
        fake.static_reply = (
            "I've completed the work and generated the file for you.")
        with _RetriedTemporaryDirectory(ignore_cleanup_errors=True) as td:
            state = self._state(td, fake.endpoint)
            executor = getattr(
                getattr(state, "autonomy", None), "_executor", None)
            if executor is None:
                self.skipTest("autonomy executor unavailable")
            out = executor(
                {"id": "m-1", "title": "m"},
                {"instruction": "create soak.txt", "title": "work",
                 "kind": "agent"},
                lambda e: None)
            self.assertFalse(out.get("ok"), out)
            self.assertIn("unverified", str(out.get("error") or ""))

    def test_mission_node_retry_feeds_failure_back(self):
        # A retried node reruns the instruction — the failure reason must
        # reach the model so it can correct instead of repeating the lie.
        fake = _FakeModelServer()
        self.addCleanup(fake.close)
        with _RetriedTemporaryDirectory(ignore_cleanup_errors=True) as td:
            state = self._state(td, fake.endpoint)
            executor = getattr(
                getattr(state, "autonomy", None), "_executor", None)
            if executor is None:
                self.skipTest("autonomy executor unavailable")
            out = executor(
                {"id": "m-1", "title": "m"},
                {"instruction": "create soak.txt", "title": "work",
                 "kind": "agent", "retries": 1,
                 "result": {"ok": False,
                            "error": "unverified action claims"}},
                lambda e: None)
            sent = fake.requests[-1]["messages"]
            user_msgs = [str(m.get("content") or "")
                         for m in sent if m.get("role") == "user"]
            self.assertTrue(
                any("previous attempt failed" in m for m in user_msgs),
                user_msgs)
            self.assertTrue(
                any("unverified action claims" in m for m in user_msgs),
                user_msgs)
            self.assertTrue(out.get("ok"), out)
        fake = _FakeModelServer()
        self.addCleanup(fake.close)
        with _RetriedTemporaryDirectory(ignore_cleanup_errors=True) as td:
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
        with _RetriedTemporaryDirectory(ignore_cleanup_errors=True) as td:
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
        with _RetriedTemporaryDirectory(ignore_cleanup_errors=True) as td:
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
        with _RetriedTemporaryDirectory(ignore_cleanup_errors=True) as td:
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
        with _RetriedTemporaryDirectory(ignore_cleanup_errors=True) as td:
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
        with _RetriedTemporaryDirectory(ignore_cleanup_errors=True) as td:
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
        with _RetriedTemporaryDirectory(ignore_cleanup_errors=True) as td:
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
        with _RetriedTemporaryDirectory(ignore_cleanup_errors=True) as td:
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

    def test_stalled_task_reaper_unwedges_queue(self):
        """A task record stuck 'running' with no live driver thread must not
        wedge the single-flight queue — the watchdog marks it failed so the
        queued item can start."""
        fake = _FakeModelServer()
        self.addCleanup(fake.close)
        with _RetriedTemporaryDirectory(ignore_cleanup_errors=True) as td:
            state = self._state(td, fake.endpoint)
            stranded = state.tasks.create("ghost task", "auto")
            state.tasks.update(
                stranded.id, status="running", phase="researching_failure")
            # Push updated_at past the reap grace so the watchdog treats the
            # driverless row as stranded (update() always refreshes it, so
            # backdate the record directly).
            state.tasks.get(stranded.id).updated_at = time.time() - 600.0
            self.assertFalse(state.agent.has_live_driver(stranded.id))

            state.queue.enqueue("real follow-up task")
            state._reap_stalled_tasks()
            self.assertEqual(state.tasks.get(stranded.id).status, "error")
            state._dequeue_next()

            deadline = time.time() + 30
            while time.time() < deadline:
                done = [t for t in state.tasks.recent(5)
                        if t.get("status") in {"completed", "error", "cancelled"}]
                if len(done) >= 2 and not len(state.queue):
                    break
                time.sleep(0.1)
            self.assertEqual(len(state.queue), 0)
            self.assertEqual(
                state.tasks.get(stranded.id).status, "error")
            completed = [t for t in state.tasks.recent(5)
                         if t.get("status") == "completed"]
            self.assertEqual(len(completed), 1)

    def test_reaper_leaves_live_drive_alone(self):
        """A running task with a live registered driver is never reaped."""
        fake = _FakeModelServer()
        self.addCleanup(fake.close)
        with _RetriedTemporaryDirectory(ignore_cleanup_errors=True) as td:
            state = self._state(td, fake.endpoint)
            task = state.tasks.create("still driving", "auto")
            state.tasks.update(task.id, status="running", phase="working")
            state.tasks.get(task.id).updated_at = time.time() - 600.0
            state.agent._drive_threads[task.id] = threading.current_thread()
            state._reap_stalled_tasks()
            self.assertEqual(state.tasks.get(task.id).status, "running")
            # And a stale waiting_approval task is a legitimate parked state,
            # not a dead driver — never reaped.
            parked = state.tasks.create("awaiting user", "auto")
            state.tasks.update(
                parked.id, status="waiting_approval", phase="waiting_approval",
                pending_approval={"kind": "tool", "name": "x"})
            state.tasks.get(parked.id).updated_at = time.time() - 600.0
            state._reap_stalled_tasks()
            self.assertEqual(
                state.tasks.get(parked.id).status, "waiting_approval")

    def test_dequeue_respects_live_driver_beyond_ledger_window(self):
        """An active task pushed out of the recent() window by newer rows
        must still hold single-flight — the driver registry, not ledger
        position, is the authoritative liveness check."""
        fake = _FakeModelServer()
        self.addCleanup(fake.close)
        with _RetriedTemporaryDirectory(ignore_cleanup_errors=True) as td:
            state = self._state(td, fake.endpoint)
            deep = state.tasks.create("deep running task", "auto")
            state.tasks.update(deep.id, status="running", phase="working")
            for i in range(55):  # push it beyond recent(50)
                pad = state.tasks.create(f"pad {i}", "auto")
                state.tasks.update(pad.id, status="completed")
            state.agent._drive_threads[deep.id] = threading.current_thread()
            state.queue.enqueue("queued work")
            state._dequeue_next()
            self.assertEqual(len(state.queue), 1)  # driver seen — held

    def test_evict_idle_pins_live_driver_models(self):
        """A live drive's model must stay resident even when its task row
        has aged out of the recent() window — evicting it mid-drive would
        kill the in-flight request."""
        fake = _FakeModelServer()
        self.addCleanup(fake.close)
        with _RetriedTemporaryDirectory(ignore_cleanup_errors=True) as td:
            state = self._state(td, fake.endpoint)
            deep = state.tasks.create("deep running task", "auto")
            state.tasks.update(
                deep.id, status="running", model_id="qwen3-14b")
            for i in range(55):  # push it beyond recent(50)
                pad = state.tasks.create(f"pad {i}", "auto")
                state.tasks.update(pad.id, status="completed")
            state.agent._drive_threads[deep.id] = threading.current_thread()
            captured: dict = {}
            orig = state.runtime.evict_idle
            state.runtime.evict_idle = lambda **kw: (
                captured.update(kw) or [])
            try:
                state._evict_idle_models()
            finally:
                state.runtime.evict_idle = orig
            self.assertIn("qwen3-14b", captured.get("busy_models") or set())

    def test_evict_idle_unknown_live_driver_pins_all_residents(self):
        """A live drive whose task row has no model_id could be serving any
        resident runtime — pressure eviction must pin all of them rather
        than kill an in-flight request."""
        fake = _FakeModelServer()
        self.addCleanup(fake.close)
        with _RetriedTemporaryDirectory(ignore_cleanup_errors=True) as td:
            state = self._state(td, fake.endpoint)
            task = state.tasks.create("unattributed drive", "auto")
            state.tasks.update(task.id, status="running")  # model_id stays ""
            state.agent._drive_threads[task.id] = threading.current_thread()
            state.runtime.resident_model_ids = lambda: ["qwen3-14b", "qwen3-4b"]
            captured: dict = {}
            orig = state.runtime.evict_idle
            state.runtime.evict_idle = lambda **kw: (
                captured.update(kw) or [])
            try:
                state._evict_idle_models()
            finally:
                state.runtime.evict_idle = orig
            busy = captured.get("busy_models") or set()
            self.assertIn("qwen3-14b", busy)
            self.assertIn("qwen3-4b", busy)

    def test_full_stack_sse_stream_end_to_end(self):
        """Real HTTP server + SSE + event bus + fake model — the exact path
        the desktop UI drives."""
        fake = _FakeModelServer()
        self.addCleanup(fake.close)
        # Daemon threads may still be flushing state during teardown;
        # Windows holds directory locks briefly.
        with _RetriedTemporaryDirectory(ignore_cleanup_errors=True) as td:
            from localcodeagent.config import AgentConfig, ModelProfile
            from localcodeagent.server import create_server, stop_state
            ws = Path(td)
            cfg = AgentConfig(profiles_onboarding_gate=False, 
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
        with _RetriedTemporaryDirectory(ignore_cleanup_errors=True) as td:
            from localcodeagent.config import AgentConfig, ModelProfile
            from localcodeagent.server import create_server, stop_state
            ws = Path(td)
            cfg = AgentConfig(profiles_onboarding_gate=False, 
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
        with _RetriedTemporaryDirectory(ignore_cleanup_errors=True) as td:
            from localcodeagent.config import AgentConfig, ModelProfile
            from localcodeagent.server import AppState
            cfg = AgentConfig(profiles_onboarding_gate=False, 
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
                # Watchdog opt-in — crashed MCP servers must auto-restart.
                self.assertTrue(svc.metadata.get("auto_restart"))
            finally:
                state.mcp.shutdown()


class AutonomyApiTests(unittest.TestCase):
    """Autonomy CRUD endpoints — triggers expose the signal vocabulary for
    the UI form, and enable/disable/delete actually mutate the store."""

    def test_trigger_crud_and_signals(self):
        with _RetriedTemporaryDirectory(ignore_cleanup_errors=True) as td:
            from localcodeagent.config import AgentConfig, ModelProfile
            from localcodeagent.server import create_server, stop_state
            ws = Path(td)
            cfg = AgentConfig(profiles_onboarding_gate=False, 
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
                with urllib.request.urlopen(req, timeout=10) as r:
                    return json.loads(r.read())

            # Signals vocabulary is exposed so the UI can build the event
            # picker without hardcoding.
            with urllib.request.urlopen(
                    f"{base}/api/triggers", timeout=10) as r:
                data = json.loads(r.read())
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
            with urllib.request.urlopen(
                    f"{base}/api/triggers", timeout=10) as r:
                triggers = json.loads(r.read())["triggers"]
            trig = next(t for t in triggers if t["id"] == tid)
            self.assertFalse(trig["enabled"])

            out = post(f"/api/triggers/{tid}/delete", {})
            self.assertTrue(out["ok"])
            with urllib.request.urlopen(
                    f"{base}/api/triggers", timeout=10) as r:
                remaining = json.loads(r.read())["triggers"]
            self.assertFalse(any(t["id"] == tid for t in remaining))

    def test_schedule_create_validates_kind(self):
        with _RetriedTemporaryDirectory(ignore_cleanup_errors=True) as td:
            from localcodeagent.config import AgentConfig, ModelProfile
            from localcodeagent.server import create_server, stop_state
            ws = Path(td)
            cfg = AgentConfig(profiles_onboarding_gate=False, 
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
                try:
                    self.assertEqual(e.code, 400)
                finally:
                    e.close()

    def test_cancel_parked_task_closes_session_and_clears_approval(self):
        """Regression: /api/jobs/cancel on a waiting_approval task left the
        session in _sessions (leaked memory + resumable stale state) and
        kept pending_approval in the ledger row. A parked task has no live
        drive to notice the cancel, so the endpoint must clean up itself."""
        with _RetriedTemporaryDirectory(ignore_cleanup_errors=True) as td:
            from localcodeagent.config import AgentConfig, ModelProfile
            from localcodeagent.server import create_server, stop_state
            ws = Path(td)
            cfg = AgentConfig(profiles_onboarding_gate=False,
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

            task = state.tasks.create("parked work", mode="auto")
            from localcodeagent.agent.orchestrator import _AgentSession
            session = _AgentSession(
                task_id=task.id, user_text=task.prompt, mode=task.mode,
                messages=[], decision=None, profile=None, provider=None,
                pending_approval={"kind": "tool", "call": {"name": "x"}})
            state.agent._sessions[task.id] = session
            state.tasks.update(
                task.id, status="waiting_approval", phase="waiting",
                pending_approval=session.pending_approval)

            req = urllib.request.Request(
                f"{base}/api/jobs/cancel",
                data=json.dumps({"job_id": f"task-{task.id}"}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=10) as r:
                out = json.loads(r.read())
            self.assertTrue(out["ok"], out)

            self.assertNotIn(task.id, state.agent._sessions)
            row = state.tasks.get(task.id)
            self.assertEqual(row.status, "cancelled")
            self.assertIsNone(row.pending_approval)


class ChatAttachmentTests(unittest.TestCase):
    """Chat '+' attachments: text files inline into the model's user
    message; images persist locally and reach the image lane as source
    paths. Task titles/routing/memory see only the bare prompt."""

    def _state(self, td: str, endpoint: str):
        from localcodeagent.config import AgentConfig, ModelProfile
        from localcodeagent.server import AppState
        cfg = AgentConfig(profiles_onboarding_gate=False, 
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
        with _RetriedTemporaryDirectory(ignore_cleanup_errors=True) as td:
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
        with _RetriedTemporaryDirectory(ignore_cleanup_errors=True) as td:
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
        with _RetriedTemporaryDirectory(ignore_cleanup_errors=True) as td:
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
