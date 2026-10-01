from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from localcodeagent.config import AgentConfig, ModelProfile
from localcodeagent.runtime.tuner import RuntimeTuner, recommended_context


def _profile(**kw):
    base = dict(
        id="qwen3-14b", endpoint="http://127.0.0.1:1/v1", model="Qwen3-14B-Q4_K_M",
        model_path="models/Qwen3-14B-Q4_K_M.gguf",
        roles=["primary_coder"], runtime="llama_cpp",
        context_window=32768, gpu_layers=99, threads=0,
    )
    base.update(kw)
    return ModelProfile(**base)


HELP_TEXT = """
  --flash-attn                 enable flash attention
  --cache-reuse N              reuse KV cache prefix
  --batch-size N               logical batch size
  --ubatch-size N              physical batch size
  --threads N                  cpu threads
  --gpu-layers N               layers on gpu
  --ctx-size N                 context window
  --cache-type-k TYPE          kv cache type
  --mlock                      lock memory
"""


class _Proc:
    def __init__(self, out):
        self.stdout = out
        self.stderr = ""
        self.returncode = 0


def _tuner(tmp: str, help_text: str = HELP_TEXT, config: AgentConfig | None = None) -> RuntimeTuner:
    cfg = config or AgentConfig(models=[_profile()], permissions={})
    tuner = RuntimeTuner(Path(tmp), cfg)
    tuner._find_llama = lambda: "llama-server"

    def fake_run(cmd, **kwargs):
        if "--help" in cmd:
            return _Proc(help_text)
        return _Proc("llama.cpp build 6100")

    with patch("localcodeagent.runtime.tuner.subprocess.run", side_effect=fake_run):
        tuner.capabilities()
    return tuner


class CapabilityTests(unittest.TestCase):
    def test_detects_supported_flags(self):
        with tempfile.TemporaryDirectory() as td:
            tuner = _tuner(td)
            self.assertTrue(tuner.supports("--flash-attn"))
            self.assertTrue(tuner.supports("--cache-reuse"))
            self.assertFalse(tuner.supports("--model-draft"))

    def test_missing_binary_marks_unavailable(self):
        with tempfile.TemporaryDirectory() as td:
            tuner = RuntimeTuner(Path(td), AgentConfig(models=[_profile()], permissions={}))
            tuner._find_llama = lambda: None
            caps = tuner.capabilities()
            self.assertEqual(caps["supported"], set())
            self.assertEqual(tuner.speculative_status(), "not_supported")

    def test_no_unsupported_flags_in_heuristics(self):
        with tempfile.TemporaryDirectory() as td:
            tuner = _tuner(td, help_text="--ctx-size N only")
            args = tuner.tuned_flags(_profile())
            self.assertNotIn("--flash-attn", args)
            self.assertNotIn("--cache-reuse", args)


class TuningResolutionTests(unittest.TestCase):
    def test_heuristic_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            tuner = _tuner(td)
            args = tuner.tuned_flags(_profile())
            self.assertIn("--flash-attn", args)
            self.assertIn("--cache-reuse", args)
            self.assertIn("--batch-size", args)
            self.assertIn("--ubatch-size", args)
            self.assertIn("--threads", args)

    def test_persisted_result_reused_then_invalidated(self):
        with tempfile.TemporaryDirectory() as td:
            profile = _profile()
            tuner = _tuner(td)
            tuner.record_result(profile, ["--batch-size", "999"], {"predicted_per_second": 42.0})
            self.assertEqual(tuner.tuned_flags(profile), ["--batch-size", "999"])
            changed = _profile(context_window=8192)
            self.assertNotEqual(tuner.fingerprint(profile), tuner.fingerprint(changed))
            self.assertIn("--batch-size", tuner.tuned_flags(changed))
            self.assertNotIn("999", tuner.tuned_flags(changed))

    def test_persistence_survives_reload(self):
        with tempfile.TemporaryDirectory() as td:
            profile = _profile()
            tuner = _tuner(td)
            tuner.record_result(profile, ["--ubatch-size", "128"], {"predicted_per_second": 30.0})
            tuner2 = _tuner(td)
            self.assertEqual(tuner2.tuned_flags(profile), ["--ubatch-size", "128"])

    def test_reset_restores_heuristics(self):
        with tempfile.TemporaryDirectory() as td:
            profile = _profile()
            tuner = _tuner(td)
            tuner.record_result(profile, ["--ubatch-size", "111"], {"predicted_per_second": 1.0})
            tuner.reset(profile.id)
            args = tuner.tuned_flags(profile)
            self.assertNotIn("111", args)
            self.assertIn("--batch-size", args)

    def test_quiet_and_max_modes(self):
        with tempfile.TemporaryDirectory() as td:
            tuner = _tuner(td)
            quiet = tuner.tuned_flags(_profile(), mode="quiet")
            self.assertEqual(quiet[quiet.index("--batch-size") + 1], "256")
            mx = tuner.tuned_flags(_profile(), mode="max")
            self.assertEqual(mx[mx.index("--batch-size") + 1], "1024")


class ContextHintTests(unittest.TestCase):
    def test_role_windows(self):
        self.assertEqual(recommended_context(_profile(roles=["utility"], context_window=32768)), 8192)
        self.assertEqual(recommended_context(_profile(roles=["primary_coder"], context_window=32768)), 16384)
        self.assertEqual(recommended_context(_profile(roles=["deep_reasoner"], context_window=65536)), 32768)

    def test_never_exceeds_profile_window(self):
        self.assertEqual(recommended_context(_profile(roles=["utility"], context_window=4096)), 4096)


class BenchmarkTests(unittest.TestCase):
    class _Probe:
        def __init__(self, profile, port, args):
            self.endpoint = "http://127.0.0.1:9/v1"
            self.stopped = False
            self.args = args

        def stop(self):
            self.stopped = True

    def test_benchmark_picks_fastest_and_persists(self):
        with tempfile.TemporaryDirectory() as td:
            tuner = _tuner(td)
            profile = _profile()
            metrics = {
                "fast": {"predicted_per_second": 50.0, "ttft_ms": 100.0},
                "slow": {"predicted_per_second": 20.0, "ttft_ms": 400.0},
            }
            calls = []

            def launch(p, port, args):
                calls.append(list(args))
                return BenchmarkTests._Probe(p, port, args)

            def measure(endpoint, *_):
                return metrics["fast"] if "--flash-attn" in calls[-1] else metrics["slow"]

            with patch.object(tuner, "_measure", side_effect=measure):
                result = tuner.benchmark(profile, launch=launch, candidates=[["--batch-size", "512"], ["--flash-attn", "auto"]])
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["best"]["args"], ["--flash-attn", "auto"])
            self.assertEqual(tuner.tuned_flags(profile), ["--flash-attn", "auto"])

    def test_benchmark_all_failed_rolls_back(self):
        with tempfile.TemporaryDirectory() as td:
            tuner = _tuner(td)
            profile = _profile()
            tuner.record_result(profile, ["--batch-size", "321"], {"predicted_per_second": 33.0})

            def launch(p, port, args):
                raise RuntimeError("probe crashed")

            result = tuner.benchmark(profile, launch=launch, candidates=[["--batch-size", "2048"]])
            self.assertEqual(result["status"], "all_failed")
            self.assertEqual(tuner.tuned_flags(profile), ["--batch-size", "321"])

    def test_benchmark_unavailable_without_launch(self):
        with tempfile.TemporaryDirectory() as td:
            tuner = _tuner(td)
            result = tuner.benchmark(_profile(), launch=None)
            self.assertEqual(result["status"], "unavailable")


class SpeculativeStatusTests(unittest.TestCase):
    def test_no_draft_model(self):
        with tempfile.TemporaryDirectory() as td:
            tuner = _tuner(td)
            self.assertEqual(tuner.speculative_status(), "not_supported")


class ResidencyEventTests(unittest.TestCase):
    def test_hook_receives_eviction(self):
        from localcodeagent.runtime.manager import RuntimeManager
        mgr = RuntimeManager.__new__(RuntimeManager)
        captured = []
        mgr.on_residency_event = captured.append
        mgr._emit_residency("evict", "qwen3-4b", "memory pressure")
        self.assertEqual(captured[0]["action"], "evict")
        self.assertEqual(captured[0]["model_id"], "qwen3-4b")

    def test_hook_failure_is_safe(self):
        from localcodeagent.runtime.manager import RuntimeManager
        mgr = RuntimeManager.__new__(RuntimeManager)
        mgr.on_residency_event = lambda e: (_ for _ in ()).throw(RuntimeError("x"))
        mgr._emit_residency("rewarm", "m")  # must not raise


class ConfigTests(unittest.TestCase):
    def test_performance_mode_parse(self):
        from localcodeagent.config import load_config
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "config.json"
            p.write_text(json.dumps({"performance_mode": "max"}), encoding="utf-8")
            cfg = load_config(p)
            self.assertEqual(cfg.performance_mode, "max")
            p.write_text(json.dumps({"performance_mode": "bogus"}), encoding="utf-8")
            self.assertEqual(load_config(p).performance_mode, "auto")
            self.assertTrue(cfg.runtime_dynamic_context)


if __name__ == "__main__":
    unittest.main()
