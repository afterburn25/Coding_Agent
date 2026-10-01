from __future__ import annotations

import io
import json
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from localcodeagent.agent.classify import research_class, wants_long_form
from localcodeagent.agent.orchestrator import AgentOrchestrator
from localcodeagent.boot import MARKER_PREFIX, boot_report, reporter_from_env
from localcodeagent.config import AgentConfig, ModelProfile, default_config, load_config
from localcodeagent.models.provider import ProviderResponse
from localcodeagent.models.router import ModelRouter
from localcodeagent.runtime.catalog import CODING_MODEL_CATALOG
from localcodeagent.streaming import TokenCoalescer
from localcodeagent.tools.base import ToolRegistry
from localcodeagent.workflow.checkpoint import CheckpointManager
from localcodeagent.workflow.memory import ProjectMemory
from localcodeagent.workflow.repository import RepositoryIndex
from localcodeagent.workflow.tasks import TaskStore


ROOT = Path(__file__).resolve().parents[1]


class _FakeRuntime:
    def __init__(self):
        self.rewarmed = 0

    def refresh_hardware(self):
        pass

    def ensure_ready(self, profile):
        return "http://127.0.0.1:1/v1"

    def recover(self, profile):
        return "http://127.0.0.1:1/v1"

    def rewarm_keep_loaded(self):
        self.rewarmed += 1
        return []


class _CaptureProvider:
    def __init__(self, content: str = "done"):
        self.messages = []
        self.tools = "unset"
        self.max_tokens = None
        self._content = content

    def complete(self, *, messages, tools=None, max_tokens=None):
        self.messages = list(messages)
        self.tools = tools
        self.max_tokens = max_tokens
        return ProviderResponse(message={"role": "assistant", "content": self._content}, raw={})


def _agent(root: Path, *, roles=None, research=None, provider=None, config_overrides=None):
    profile = ModelProfile(
        id="local", endpoint="http://unused/v1", model="x",
        roles=roles or ["utility", "fast_coder", "primary_coder"], runtime="external",
    )
    config = AgentConfig(
        models=[profile], permissions={}, research_enabled=True,
        auto_verify_after_changes=False, review_after_changes=False,
        **(config_overrides or {}),
    )
    index = RepositoryIndex(root)
    agent = AgentOrchestrator(
        config, ModelRouter(config.models), ToolRegistry(config.permissions), _FakeRuntime(),
        tasks=TaskStore(root), checkpoints=CheckpointManager(root),
        memory=ProjectMemory(root), repository_index=index,
        research=research,
    )
    agent._provider_for = lambda _: provider
    return agent


class _StubResearch:
    def __init__(self):
        self.planned = 0
        self.researched = 0

    class _Plan:
        needed = False

    def plan(self, text, mode="auto"):
        self.planned += 1
        return self._Plan()

    def prepare_task(self, text, mode="auto"):
        return {"guidance": "preflight"}


class RequestClassificationTests(unittest.TestCase):
    def test_stable_questions(self):
        for text in (
            "What is a neural network?",
            "Why is the sky blue?",
            "Explain quantum computing simply",
            "What does RAM do?",
            "Can a neural network learn by itself?",
        ):
            self.assertEqual(research_class(text), "stable", text)

    def test_volatile_questions(self):
        for text in (
            "what is the latest version of python",
            "current weather in tokyo",
            "today's news",
            "recent linux kernel release",
            "stock price of nvidia",
        ):
            self.assertEqual(research_class(text), "volatile", text)

    def test_explicit_research(self):
        for text in ("search the web for it", "look this up please", "research this topic"):
            self.assertEqual(research_class(text), "explicit", text)

    def test_long_form_detection(self):
        self.assertTrue(wants_long_form("give me a detailed explanation of transformers"))
        self.assertTrue(wants_long_form("write a step-by-step tutorial"))
        self.assertFalse(wants_long_form("what is ram"))


class FastLaneRoutingTests(unittest.TestCase):
    def test_stable_question_routes_utility_and_skips_research_preflight(self):
        with tempfile.TemporaryDirectory() as td:
            research = _StubResearch()
            provider = _CaptureProvider("a neural network is a model")
            agent = _agent(Path(td), research=research, provider=provider)
            result = agent.run("What is a neural network?")
            self.assertEqual(result.routing.role, "utility")
            self.assertIsNone(provider.tools)
            self.assertEqual(research.planned, 0)

    def test_utility_has_no_tool_schema(self):
        with tempfile.TemporaryDirectory() as td:
            provider = _CaptureProvider()
            agent = _agent(Path(td), provider=provider)
            agent.run("what does ram do")
            self.assertIsNone(provider.tools)

    def test_utility_skips_repository_index(self):
        with tempfile.TemporaryDirectory() as td:
            provider = _CaptureProvider()
            agent = _agent(Path(td), provider=provider)
            calls = {"ensure": 0}
            original = agent.repository_index.ensure
            agent.repository_index.ensure = lambda *a, **k: calls.__setitem__("ensure", calls["ensure"] + 1) or original(*a, **k)
            agent.run("why is the sky blue")
            self.assertEqual(calls["ensure"], 0)

    def test_explicit_search_still_researches(self):
        with tempfile.TemporaryDirectory() as td:
            research = _StubResearch()
            provider = _CaptureProvider()
            agent = _agent(Path(td), research=research, provider=provider)
            agent.run("search the web for mars facts")
            self.assertGreater(research.planned, 0)

    def test_fast_general_output_cap_and_long_form_escalation(self):
        with tempfile.TemporaryDirectory() as td:
            provider = _CaptureProvider()
            agent = _agent(Path(td), provider=provider)
            agent.run("what is ram")
            self.assertEqual(provider.max_tokens, agent.config.fast_general_output_tokens)
            agent.run("give me a detailed step-by-step tutorial on virtual memory")
            self.assertEqual(provider.max_tokens, agent.config.fast_general_long_output_tokens)

    def test_fast_general_history_is_bounded(self):
        with tempfile.TemporaryDirectory() as td:
            provider = _CaptureProvider()
            agent = _agent(Path(td), provider=provider)
            history = [
                {"role": "user" if i % 2 == 0 else "assistant", "content": f"turn {i}"}
                for i in range(30)
            ]
            agent.run("continue our chat about plants", history=history)
            history_msgs = [m for m in provider.messages if m.get("role") in {"user", "assistant"}]
            # bounded window + the current user turn
            self.assertLessEqual(len(history_msgs), agent.config.fast_general_history_turns + 1)
            self.assertEqual(history_msgs[-1]["content"], "continue our chat about plants")


class UtilityModelConfigTests(unittest.TestCase):
    def test_catalog_entry_is_audited(self):
        asset = next(a for a in CODING_MODEL_CATALOG if a.id == "qwen3-4b-instruct-q4-k-m")
        self.assertEqual(asset.roles, ("utility",))
        self.assertTrue(asset.url.startswith("https://huggingface.co/"))
        self.assertRegex(asset.sha256, r"^[0-9a-f]{64}$")
        self.assertGreater(asset.size_bytes, 1_000_000_000)
        self.assertTrue(asset.license)

    def test_default_config_uses_dedicated_utility_model(self):
        cfg = default_config()
        utility = next(m for m in cfg.models if m.id == "qwen3-4b-instruct")
        self.assertEqual(utility.roles, ["utility"])
        self.assertTrue(utility.keep_loaded)
        self.assertEqual(utility.context_window, 8192)
        q14 = next(m for m in cfg.models if m.id == "qwen3-14b")
        self.assertNotIn("utility", q14.roles)
        self.assertIn("primary_coder", q14.roles)

    def test_existing_config_migration_grafts_utility_profile(self):
        with tempfile.TemporaryDirectory() as td:
            cfg_path = Path(td) / "config.json"
            cfg_path.write_text(json.dumps({
                "models": [{
                    "id": "qwen3-14b", "endpoint": "http://127.0.0.1:8081/v1",
                    "model": "Qwen3-14B-Q4_K_M",
                    "model_path": "models/Qwen3-14B-Q4_K_M.gguf",
                    "roles": ["utility", "fast_coder", "primary_coder"],
                }],
            }))
            cfg = load_config(cfg_path)
            ids = [m.id for m in cfg.models]
            self.assertIn("qwen3-4b-instruct", ids)
            q14 = next(m for m in cfg.models if m.id == "qwen3-14b")
            self.assertNotIn("utility", q14.roles)

    def test_setup_planner_assigns_utility_to_4b(self):
        from localcodeagent.runtime.setup import suggest_model_profiles
        suggestions = suggest_model_profiles([
            {"name": "Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf", "path": "/models/q4.gguf", "size_gb": 2.4},
            {"name": "Qwen3-14B-Q4_K_M.gguf", "path": "/models/q14.gguf", "size_gb": 9.0},
            {"name": "Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf", "path": "/models/q30.gguf", "size_gb": 18.6},
        ])
        self.assertEqual([s["id"] for s in suggestions], ["qwen3-4b-instruct", "qwen3-14b", "qwen3-coder-30b"])
        self.assertEqual(suggestions[0]["roles"], ["utility"])
        self.assertTrue(suggestions[0]["keep_loaded"])
        self.assertEqual(suggestions[0]["context_window"], 8192)
        self.assertNotIn("utility", suggestions[1]["roles"])

    def test_router_falls_back_to_14b_when_utility_missing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            utility = ModelProfile(
                id="qwen3-4b-instruct", endpoint="http://127.0.0.1:8080/v1",
                model="x4b", model_path="models/missing-4b.gguf",
                roles=["utility"], runtime="llama_cpp", keep_loaded=True,
            )
            q14 = ModelProfile(
                id="qwen3-14b", endpoint="http://127.0.0.1:8081/v1",
                model="x14b", model_path="models/q14.gguf",
                roles=["fast_coder", "primary_coder"], runtime="llama_cpp",
            )
            config = AgentConfig(models=[utility, q14], permissions={}, research_enabled=False)

            def fits(profile):
                # 4B file does not exist -> not runnable; 14B is runnable.
                return (False, -200, "missing") if profile.id == "qwen3-4b-instruct" else (True, 0, "")

            router = ModelRouter(config.models, resource_advisor=fits)
            decision = router.choose("what is a neural network?")
            self.assertEqual(decision.role, "utility")
            self.assertEqual(decision.model_id, "qwen3-14b")


class CoalescerTests(unittest.TestCase):
    def test_character_deltas_merge(self):
        c = TokenCoalescer()
        emitted = []
        for ch in "Hello there":
            chunk = c.feed(ch)
            if chunk:
                emitted.append(chunk)
        emitted.append(c.flush())
        self.assertEqual("".join(emitted), "Hello there")
        self.assertLess(len(emitted), 5)

    def test_order_is_preserved(self):
        c = TokenCoalescer()
        text = "The quick brown fox jumps over the lazy dog. " * 4
        emitted = []
        for piece in re.findall(r"..?", text):
            chunk = c.feed(piece)
            if chunk:
                emitted.append(chunk)
        emitted.append(c.flush())
        self.assertEqual("".join(emitted), text)

    def test_boundary_flush_after_minimum(self):
        c = TokenCoalescer(min_chars=8, max_chars=64)
        self.assertIsNone(c.feed("small "))
        # under min? no — len is 6 < 8; add more
        self.assertIsNone(c.feed("x"))
        out = c.feed("y ")
        self.assertIsNotNone(out)

    def test_size_cap_forces_flush(self):
        c = TokenCoalescer(min_chars=10, max_chars=20)
        out = c.feed("a" * 25)
        self.assertEqual(out, "a" * 25)

    def test_interval_flush(self):
        now = [0.0]
        c = TokenCoalescer(min_chars=8, flush_interval=0.03, clock=lambda: now[0])
        c.feed("abc")
        now[0] += 0.05
        # still under min_chars, so interval alone doesn't flush
        self.assertIsNone(c.feed("def"))
        out = c.feed("ghijk")
        self.assertIsNotNone(out)


class BootMarkerTests(unittest.TestCase):
    def test_report_format(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            boot_report(42, "VERIFYING · AI MODELS", "Checking Qwen3 4B")
        line = buf.getvalue().strip()
        self.assertTrue(line.startswith(MARKER_PREFIX))
        payload = json.loads(line[len(MARKER_PREFIX):])
        self.assertEqual(payload["pct"], 42.0)
        self.assertEqual(payload["primary"], "VERIFYING · AI MODELS")
        # ASCII-safe for the OEM-decoded stdout pipe
        self.assertTrue(all(ord(c) < 128 for c in line))

    def test_env_gate(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(reporter_from_env())
        with patch.dict("os.environ", {"NEXUS_BOOT_MARKERS": "1"}):
            self.assertIs(reporter_from_env(), boot_report)

    def test_host_parses_markers(self):
        src = (ROOT / "desktop" / "ChatNexus.Desktop" / "Program.cs").read_text(encoding="utf-8")
        self.assertIn("NEXUS_BOOT_MARKERS", src)
        self.assertIn("TryParseBootMarker", src)
        self.assertIn("BootPhase", src)


class FrontendBufferTests(unittest.TestCase):
    def setUp(self):
        self.src = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    def test_token_handler_uses_render_buffer(self):
        self.assertIn("pendingText", self.src)
        self.assertIn("requestAnimationFrame", self.src)
        # no synchronous per-event DOM append for token deltas
        token_block = self.src.split("if(name==='token')", 1)[1].split("return;}", 1)[0]
        self.assertNotIn("state.text.textContent+=", token_block)

    def test_result_clears_pending_buffer(self):
        result_block = self.src.split("if(name==='result')", 1)[1][:400]
        self.assertIn("pendingText=''", result_block)

    def test_refusal_retry_clears_pending_buffer(self):
        retry_block = self.src.split("generic_refusal_retry", 1)[1][:400]
        self.assertIn("pendingText=''", retry_block)


if __name__ == "__main__":
    unittest.main()
