from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from localcodeagent.config import ModelProfile
from localcodeagent.models.openai_compat import OpenAICompatibleProvider


ROOT = Path(__file__).resolve().parents[1]


class _Response:
    def __init__(self, *, lines=None, payload: bytes = b"", content_type: str = "text/event-stream"):
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


def _line(payload: dict) -> bytes:
    return ("data: " + json.dumps(payload) + "\n").encode("utf-8")


class ModelStreamingTests(unittest.TestCase):
    def _provider(self) -> OpenAICompatibleProvider:
        profile = ModelProfile(
            id="local",
            endpoint="http://127.0.0.1:9999/v1",
            model="test-model",
            roles=["primary_coder"],
        )
        return OpenAICompatibleProvider(profile)

    def test_streams_content_deltas_and_reconstructs_message(self):
        response = _Response(lines=[
            _line({"choices": [{"delta": {"role": "assistant", "content": "Hello "}}]}),
            _line({"choices": [{"delta": {"content": "world"}, "finish_reason": "stop"}]}),
            b"data: [DONE]\n",
        ])
        deltas = []
        with patch("localcodeagent.models.openai_compat.urllib.request.urlopen", return_value=response):
            result = self._provider().complete_stream(
                messages=[{"role": "user", "content": "hi"}],
                on_delta=deltas.append,
            )
        self.assertEqual(deltas, ["Hello ", "world"])
        self.assertEqual(result.message["content"], "Hello world")
        self.assertEqual(result.raw["finish_reason"], "stop")

    def test_provider_sends_configured_output_token_cap(self):
        profile = ModelProfile(
            id="local",
            endpoint="http://127.0.0.1:9999/v1",
            model="test-model",
            roles=["primary_coder"],
            max_output_tokens=777,
        )
        provider = OpenAICompatibleProvider(profile)
        response = _Response(lines=[
            _line({"choices": [{"delta": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]}),
            b"data: [DONE]\n",
        ])
        captured = {}

        def fake_urlopen(req, timeout):
            captured["payload"] = json.loads(req.data.decode("utf-8"))
            return response

        with patch("localcodeagent.models.openai_compat.urllib.request.urlopen", side_effect=fake_urlopen):
            provider.complete_stream(messages=[{"role": "user", "content": "hi"}])

        self.assertEqual(captured["payload"]["max_tokens"], 777)
        self.assertEqual(captured["payload"]["temperature"], 0.2)

    def test_provider_uses_profile_temperature(self):
        profile = ModelProfile(
            id="local",
            endpoint="http://127.0.0.1:9999/v1",
            model="test-model",
            roles=["primary_coder"],
            temperature=0.65,
        )
        provider = OpenAICompatibleProvider(profile)
        response = _Response(lines=[
            _line({"choices": [{"delta": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]}),
            b"data: [DONE]\n",
        ])
        captured = {}

        def fake_urlopen(req, timeout):
            captured["payload"] = json.loads(req.data.decode("utf-8"))
            return response

        with patch("localcodeagent.models.openai_compat.urllib.request.urlopen", side_effect=fake_urlopen):
            provider.complete_stream(messages=[{"role": "user", "content": "hello"}])

        self.assertEqual(captured["payload"]["temperature"], 0.65)

    def test_streamed_tool_call_fragments_are_reassembled(self):
        response = _Response(lines=[
            _line({"choices": [{"delta": {"role": "assistant", "tool_calls": [{
                "index": 0,
                "id": "call-1",
                "type": "function",
                "function": {"name": "write_file", "arguments": "{\"path\":\"a"},
            }]}}]}),
            _line({"choices": [{"delta": {"tool_calls": [{
                "index": 0,
                "function": {"arguments": ".txt\",\"content\":\"ok\"}"},
            }]}, "finish_reason": "tool_calls"}]}),
            b"data: [DONE]\n",
        ])
        with patch("localcodeagent.models.openai_compat.urllib.request.urlopen", return_value=response):
            result = self._provider().complete_stream(
                messages=[{"role": "user", "content": "write"}],
                tools=[{"type": "function", "function": {"name": "write_file", "parameters": {}}}],
            )
        call = result.message["tool_calls"][0]
        self.assertEqual(call["id"], "call-1")
        self.assertEqual(call["function"]["name"], "write_file")
        self.assertEqual(
            json.loads(call["function"]["arguments"]),
            {"path": "a.txt", "content": "ok"},
        )

    def test_non_sse_endpoint_falls_back_to_normal_message(self):
        payload = json.dumps({
            "choices": [{"message": {"role": "assistant", "content": "fallback works"}}]
        }).encode("utf-8")
        response = _Response(payload=payload, content_type="application/json")
        deltas = []
        with patch("localcodeagent.models.openai_compat.urllib.request.urlopen", return_value=response):
            result = self._provider().complete_stream(
                messages=[{"role": "user", "content": "hi"}],
                on_delta=deltas.append,
            )
        self.assertEqual(result.message["content"], "fallback works")
        self.assertEqual(deltas, ["fallback works"])

    def test_chat_preflight_uses_no_coding_model_guard_for_direct_images(self):
        server = (ROOT / "localcodeagent" / "server.py").read_text(encoding="utf-8")
        self.assertEqual(server.count("can_run_without_coding_model(message)"), 2)
        self.assertNotIn("and self.state.agent.can_answer_locally(message)", server)

    def test_main_ui_uses_agent_sse_endpoint(self):
        app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        server = (ROOT / "localcodeagent" / "server.py").read_text(encoding="utf-8")
        self.assertIn("/api/chat/stream", app)
        self.assertIn("text/event-stream", server)
        self.assertIn("event_callback=emit", server)
        self.assertIn("name==='token'", app)
        self.assertIn("name==='tool'", app)
        self.assertIn("name==='image_job'", app)
        self.assertIn("Generating image…", app)
        self.assertIn("image-generation-placeholder", app)
        self.assertIn("pollImageJob", app)
        self.assertIn("name==='task'", app)
        self.assertIn("name==='heartbeat'", app)
        self.assertIn("NEXUS CORE // ACTIVE", app)
        self.assertIn("nexusThinkingStep", app)
        self.assertIn("nexusThinkingPhase", app)
        self.assertIn("Sensor sweep", app)
        self.assertIn("Engineering operation", app)
        styles = (ROOT / "web" / "styles.css").read_text(encoding="utf-8")
        self.assertIn(".nexus-thinking-hud", styles)
        self.assertIn("@keyframes nexus-orbit", styles)
        self.assertIn("prefers-reduced-motion", styles)
        self.assertIn("elapsed_seconds", app)
        self.assertIn("state.error=String(data.error", app)
        self.assertIn("fetch('/api/tasks')", app)
        self.assertIn("stream_result_recovered", app)
        self.assertIn("terminal.includes(task.status)", app)
        self.assertIn("final result recovered from durable task ledger after stream closed", app)
        self.assertIn("task.final_content||task.summary", app)
        self.assertIn("String(current.prompt||'')===state.requestMessage", app)
        self.assertIn("currentUpdated>=lastUpdated", app)
        self.assertIn("I don't have a human age", app)
        self.assertIn("/api/conversation-memory", app)
        self.assertIn("renderConversationMemory", app)
        self.assertIn("recordBuiltinExchange", app)
        self.assertIn("loadConversations", app)
        self.assertIn("/api/conversations/select", app)
        self.assertIn('"history": list(row.get("messages", []))[-32:]', server)
        self.assertIn("data-message-id", app)
        self.assertIn("conversationSearch", app)
        self.assertIn("data-feedback", app)
        self.assertIn("learn across conversations through Nexus Brain", app)
        self.assertIn("can you learn and adapt", app)
        self.assertNotIn("if self.state.nexus_brain.unlocked:\n                    self.state.sync_nexus_brain()", server)
        self.assertIn("/api/conversation-memory/exchange", server)
        self.assertIn("/api/conversation-memory/update", server)
        self.assertIn("/api/conversation-memory/forget", server)
        self.assertIn('path == "/api/time"', server)
        self.assertIn('"clock": self.state.agent.current_time_snapshot()', server)
        self.assertIn('path == "/api/conversation-memory"', server)
        self.assertIn('path == "/api/nexus-brain"', server)
        self.assertIn("/api/nexus-brain/initialize", server)
        self.assertIn("/api/nexus-brain/unlock", server)
        self.assertIn("/api/nexus-brain/subroutines", server)
        self.assertIn("/api/nexus-brain/emotions", server)
        self.assertIn("/api/nexus-brain/self-model", server)
        self.assertIn("creator_token", server)
        self.assertIn('"nexus_brain": self.state.nexus_brain.summary()', server)
        self.assertIn('runtime_root / "data" / "nexus_brain.json"', server)
        self.assertIn("mutable config cannot disable", server)
        self.assertIn('config.nexus_brain_path = "data/nexus_brain.json"', server)
        self.assertIn('self.images.adult_content_allowed = lambda: self.brain_allows("adult_content", True)', server)
        trainer_html = (ROOT / "web" / "trainer.html").read_text(encoding="utf-8")
        trainer_js = (ROOT / "web" / "trainer.js").read_text(encoding="utf-8")
        self.assertIn("Nexus Brain", trainer_html)
        self.assertIn("Signed subroutines", trainer_html)
        self.assertIn("Emotional profile", trainer_html)
        self.assertIn("Human-like conversational behavior", trainer_html)
        self.assertIn("/api/nexus-brain/initialize", trainer_js)
        self.assertIn("/api/nexus-brain/export", trainer_js)
        self.assertIn("brainCreatorToken", trainer_js)
        self.assertIn('path == "/api/conversations"', server)
        self.assertIn('path == "/api/knowledge-memory"', server)
        self.assertIn('path == "/api/model-growth"', server)
        self.assertIn("/api/model-growth/export", server)
        self.assertIn("/api/model-growth/job", server)
        self.assertIn("/api/model-growth/job/start", server)
        self.assertIn("/api/model-growth/evaluate", server)
        self.assertIn("activate_growth_candidate", server)
        self.assertIn('"--lora", str(artifact)', server)
        self.assertIn("activation_backup.json", server)
        self.assertIn("restore_growth_activation", server)
        self.assertIn("can_run_without_coding_model(message)", server)
        self.assertIn("Trainer / Model Growth", (ROOT / "web" / "trainer.html").read_text(encoding="utf-8"))
        trainer_js = (ROOT / "web" / "trainer.js").read_text(encoding="utf-8")
        self.assertIn("/api/model-growth/review", trainer_js)
        self.assertIn("/api/model-growth/job/start", trainer_js)
        self.assertIn("/api/model-growth/evaluate", trainer_js)
        self.assertIn("/api/conversation-memory/update", trainer_js)
        self.assertIn("registerCandidate", trainer_js)
        self.assertIn("chat-nexus-primary-prewarm", server)
        self.assertIn("Memory & training", (ROOT / "web" / "index.html").read_text(encoding="utf-8"))
        self.assertNotIn("stream ended before a final result was received", app)
        self.assertIn("queue.Queue", server)
        self.assertIn("chat-nexus-agent-stream", server)
        self.assertIn('self._sse_event("heartbeat"', server)
        self.assertIn('Cache-Control", "no-store, no-cache, must-revalidate, max-age=0"', server)

    def test_chat_events_mirrored_to_bus_and_ui_reconnects(self):
        app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        server = (ROOT / "localcodeagent" / "server.py").read_text(encoding="utf-8")
        # Agent events are republished on the shared bus with task attribution.
        self.assertIn('payload.setdefault("task_id", current.id)', server)
        self.assertIn('self.events.publish(etype or "task", payload)', server)
        self.assertIn('if etype in {"token", "result"}:', server)
        self.assertIn('self.state._bus_emit(event)', server)
        # The chat page subscribes to the bus so a reload keeps terminal/task
        # visibility, and ignores duplicates while its own stream is active.
        self.assertIn("connectAgentEvents()", app)
        self.assertIn("new EventSource('/api/events')", app)
        self.assertIn("agentStreamActive", app)
        self.assertIn("data.event==='queued'", app)


if __name__ == "__main__":
    unittest.main()
