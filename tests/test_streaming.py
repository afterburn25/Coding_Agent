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

    def test_main_ui_uses_agent_sse_endpoint(self):
        app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        server = (ROOT / "localcodeagent" / "server.py").read_text(encoding="utf-8")
        self.assertIn("/api/chat/stream", app)
        self.assertIn("text/event-stream", server)
        self.assertIn("event_callback=emit", server)
        self.assertIn("name==='token'", app)
        self.assertIn("name==='tool'", app)
        self.assertIn("name==='task'", app)
        self.assertIn("name==='heartbeat'", app)
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
        self.assertIn("/api/conversation-memory/exchange", server)
        self.assertIn('path == "/api/conversation-memory"', server)
        self.assertIn("can_answer_locally(message)", server)
        self.assertIn("chat-nexus-primary-prewarm", server)
        self.assertIn("Memory & training", (ROOT / "web" / "index.html").read_text(encoding="utf-8"))
        self.assertNotIn("stream ended before a final result was received", app)
        self.assertIn("queue.Queue", server)
        self.assertIn("chat-nexus-agent-stream", server)
        self.assertIn('self._sse_event("heartbeat"', server)
        self.assertIn('Cache-Control", "no-store, no-cache, must-revalidate, max-age=0"', server)


if __name__ == "__main__":
    unittest.main()
