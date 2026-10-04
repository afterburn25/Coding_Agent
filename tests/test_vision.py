import base64
import tempfile
import unittest
import types
from pathlib import Path

from localcodeagent.agent.orchestrator import AgentOrchestrator
from localcodeagent.config import AgentConfig, ModelProfile
from localcodeagent.models.openai_compat import _shrink_messages_for_context
from localcodeagent.models.router import ModelRouter
from localcodeagent.runtime.manager import RuntimeManager
from localcodeagent.runtime.setup import suggest_model_profiles


def _vision_profile(**kw):
    kw.setdefault("id", "qwen3-vl-4b")
    kw.setdefault("endpoint", "http://127.0.0.1:8086/v1")
    kw.setdefault("model", "Qwen3VL-4B-Instruct-Q4_K_M")
    kw.setdefault("roles", ["vision"])
    kw.setdefault("vision", True)
    return ModelProfile(**kw)


class SuggestionTests(unittest.TestCase):
    def test_mmproj_files_are_not_suggested_as_chat_models(self):
        sugs = suggest_model_profiles([
            {"path": "models/Qwen3VL-4B-Instruct-Q4_K_M.gguf",
             "name": "Qwen3VL-4B-Instruct-Q4_K_M.gguf", "size_gb": 2.4},
            {"path": "models/mmproj-Qwen3VL-4B-Instruct-Q8_0.gguf",
             "name": "mmproj-Qwen3VL-4B-Instruct-Q8_0.gguf", "size_gb": 0.4},
        ])
        self.assertEqual(len(sugs), 1)
        self.assertNotIn("mmproj", sugs[0]["model_path"].lower())
        self.assertTrue(sugs[0]["vision"])
        self.assertIn("vision", sugs[0]["roles"])


class LaunchCommandTests(unittest.TestCase):
    def _manager(self, root: Path) -> RuntimeManager:
        rm = RuntimeManager.__new__(RuntimeManager)
        rm.base_dir = root
        rm.config = AgentConfig()
        rm.tuner = types.SimpleNamespace(tuned_flags=lambda *a, **k: [])
        rm.discover_llama_server = lambda profile=None: "llama-server"
        rm._is_unified_llama = lambda exe: False
        return rm

    def test_mmproj_flag_emitted_when_configured(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "models").mkdir()
            (root / "models" / "m.gguf").write_bytes(b"x")
            (root / "models" / "mm.gguf").write_bytes(b"y")
            rm = self._manager(root)
            p = _vision_profile(
                runtime="llama_cpp",
                model_path="models/m.gguf",
                mmproj_path="models/mm.gguf",
            )
            cmd = rm._build_command(p, 8086, apply_tuning=False)
            self.assertIn("--mmproj", cmd)
            self.assertEqual(
                cmd[cmd.index("--mmproj") + 1],
                str(root / "models" / "mm.gguf"),
            )

    def test_missing_mmproj_fails_loudly(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "models").mkdir()
            (root / "models" / "m.gguf").write_bytes(b"x")
            rm = self._manager(root)
            p = _vision_profile(
                runtime="llama_cpp",
                model_path="models/m.gguf",
                mmproj_path="models/missing.gguf",
            )
            with self.assertRaises(RuntimeError):
                rm._build_command(p, 8086, apply_tuning=False)


class VisionRoutingTests(unittest.TestCase):
    def test_vision_role_override_selects_vision_profile(self):
        router = ModelRouter([
            ModelProfile(id="fast", endpoint="http://x", model="fast",
                         roles=["utility", "fast_coder"], priority=10),
            _vision_profile(),
        ])
        d = router.choose("what is in this photo?", override="vision")
        self.assertEqual(d.role, "vision")
        self.assertEqual(d.model_id, "qwen3-vl-4b")

    def test_vision_profile_detection(self):
        orch = AgentOrchestrator.__new__(AgentOrchestrator)
        orch.router = ModelRouter([
            ModelProfile(id="fast", endpoint="http://x", model="fast",
                         roles=["utility"], priority=10),
            _vision_profile(),
        ])
        self.assertIsNotNone(orch._vision_profile())
        orch.router = ModelRouter([
            ModelProfile(id="fast", endpoint="http://x", model="fast",
                         roles=["utility"], priority=10),
        ])
        self.assertIsNone(orch._vision_profile())

    def test_visual_reference_regex(self):
        self.assertTrue(AgentOrchestrator._VISUAL_REFERENCE_RE.search(
            "what is she wearing in the photo"))
        self.assertTrue(AgentOrchestrator._VISUAL_REFERENCE_RE.search(
            "describe it"))
        self.assertTrue(AgentOrchestrator._VISUAL_REFERENCE_RE.search(
            "look at her face"))
        self.assertFalse(AgentOrchestrator._VISUAL_REFERENCE_RE.search(
            "what time is it"))
        self.assertFalse(AgentOrchestrator._VISUAL_REFERENCE_RE.search(
            "write a python function"))


class StoredAttachmentTests(unittest.TestCase):
    def test_latest_stored_image_paths_from_history(self):
        with tempfile.TemporaryDirectory() as td:
            img = Path(td) / "abc123.png"
            img.write_bytes(b"png")
            orch = AgentOrchestrator.__new__(AgentOrchestrator)
            orch.conversation_manager = types.SimpleNamespace(
                active=lambda: {"messages": [
                    {"role": "user", "content": "look at this",
                     "attachments": [{"kind": "image", "name": "x.png",
                                      "path": str(img)}]},
                    {"role": "assistant", "content": "nice"},
                    {"role": "user", "content": "and this?"},
                ]})
            self.assertEqual(orch._latest_stored_image_paths(), [str(img)])

    def test_no_attachments_returns_empty(self):
        orch = AgentOrchestrator.__new__(AgentOrchestrator)
        orch.conversation_manager = types.SimpleNamespace(
            active=lambda: {"messages": [
                {"role": "user", "content": "hello"},
            ]})
        self.assertEqual(orch._latest_stored_image_paths(), [])

    def test_missing_files_are_skipped(self):
        orch = AgentOrchestrator.__new__(AgentOrchestrator)
        orch.conversation_manager = types.SimpleNamespace(
            active=lambda: {"messages": [
                {"role": "user", "content": "see this",
                 "attachments": [{"kind": "image", "name": "gone.png",
                                  "path": "C:/does/not/exist.png"}]},
            ]})
        self.assertEqual(orch._latest_stored_image_paths(), [])


class VisionContentTests(unittest.TestCase):
    def test_parts_built_for_vision_profile(self):
        with tempfile.TemporaryDirectory() as td:
            img = Path(td) / "i.png"
            img.write_bytes(b"PNGDATA")
            orch = AgentOrchestrator.__new__(AgentOrchestrator)
            content = orch._vision_user_content(
                "what is this", [str(img)], _vision_profile())
            self.assertIsInstance(content, list)
            self.assertEqual(content[0]["type"], "image_url")
            self.assertTrue(content[0]["image_url"]["url"].startswith(
                "data:image/png;base64,"))
            decoded = base64.b64decode(
                content[0]["image_url"]["url"].split(",", 1)[1])
            self.assertEqual(decoded, b"PNGDATA")
            self.assertEqual(content[-1], {"type": "text", "text": "what is this"})

    def test_plain_text_for_non_vision_profile(self):
        orch = AgentOrchestrator.__new__(AgentOrchestrator)
        p = ModelProfile(id="t", endpoint="", model="m", roles=[], vision=False)
        content = orch._vision_user_content("hi", ["x.png"], p)
        self.assertEqual(content, "hi")

    def test_unreadable_files_fall_back_to_text(self):
        orch = AgentOrchestrator.__new__(AgentOrchestrator)
        content = orch._vision_user_content(
            "hi", ["C:/does/not/exist.png"], _vision_profile())
        self.assertEqual(content, "hi")


class ShrinkGuardTests(unittest.TestCase):
    def test_multimodal_content_never_stringified(self):
        parts = [{"type": "image_url",
                  "image_url": {"url": "data:image/png;base64,QUJD"}},
                 {"type": "text", "text": "look"}]
        msgs = [
            {"role": "system", "content": "s" * 2000},
            {"role": "user", "content": parts},
            {"role": "user", "content": "x" * 1000},
        ]
        out = _shrink_messages_for_context(msgs, 500)
        for m in out:
            self.assertFalse(str(m.get("content", "")).startswith("[{'type'"))
            if isinstance(m.get("content"), list):
                self.assertEqual(m["content"], parts)


if __name__ == "__main__":
    unittest.main()
