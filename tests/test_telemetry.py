import json
import tempfile
import unittest
from pathlib import Path

from localcodeagent.config import ModelProfile
from localcodeagent.models.router import ModelRouter
from localcodeagent.models.telemetry import ModelPerformanceTelemetry


def profile(model_id: str) -> ModelProfile:
    return ModelProfile(
        id=model_id,
        endpoint="http://unused/v1",
        model=model_id,
        roles=["primary_coder"],
        priority=10,
    )


class ModelTelemetryTests(unittest.TestCase):
    def test_records_content_free_metrics_and_persists(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            telemetry = ModelPerformanceTelemetry(root, min_samples=1)
            telemetry.record(
                model_id="b",
                role="primary_coder",
                complexity=3,
                status="completed",
                verification_passed=True,
                review_passed=True,
                steps=4,
                elapsed_seconds=12.5,
                research_used=True,
            )
            summary = telemetry.summary()
            self.assertEqual(summary["event_count"], 1)
            self.assertEqual(summary["groups"][0]["clean_completions"], 1)
            raw = json.loads((root / ".agent" / "model_performance.json").read_text(encoding="utf-8"))
            self.assertNotIn("prompt", raw["events"][0])
            self.assertNotIn("content", raw["events"][0])

            reloaded = ModelPerformanceTelemetry(root, min_samples=1)
            score, reason = reloaded.score(profile("b"), "primary_coder", 3)
            self.assertGreater(score, 0)
            self.assertIn("1 samples", reason)

    def test_router_prefers_better_history_after_minimum_samples(self):
        with tempfile.TemporaryDirectory() as td:
            telemetry = ModelPerformanceTelemetry(Path(td), min_samples=2, weight=20)
            for _ in range(2):
                telemetry.record(model_id="a", role="primary_coder", complexity=3, status="error")
                telemetry.record(
                    model_id="b", role="primary_coder", complexity=3, status="completed",
                    verification_passed=True, review_passed=True,
                )
            router = ModelRouter([profile("a"), profile("b")], performance_advisor=telemetry.score)
            decision = router.choose("Update the backend and frontend database flow")
            self.assertEqual(decision.role, "primary_coder")
            self.assertEqual(decision.model_id, "b")
            self.assertTrue(any("learned" in reason for reason in decision.reasons))

    def test_sparse_history_keeps_deterministic_ordering(self):
        with tempfile.TemporaryDirectory() as td:
            telemetry = ModelPerformanceTelemetry(Path(td), min_samples=3, weight=20)
            telemetry.record(model_id="b", role="primary_coder", complexity=3, status="completed")
            router = ModelRouter([profile("a"), profile("b")], performance_advisor=telemetry.score)
            decision = router.choose("Update the backend and frontend database flow")
            self.assertEqual(decision.model_id, "a")

    def test_resource_fit_still_wins_over_history(self):
        with tempfile.TemporaryDirectory() as td:
            telemetry = ModelPerformanceTelemetry(Path(td), min_samples=2, weight=100)
            for _ in range(2):
                telemetry.record(model_id="a", role="primary_coder", complexity=3, status="error")
                telemetry.record(model_id="b", role="primary_coder", complexity=3, status="completed")

            def resources(model):
                if model.id == "a":
                    return True, 1, "fits current hardware"
                return False, 100, "too large for current hardware"

            router = ModelRouter(
                [profile("a"), profile("b")],
                resource_advisor=resources,
                performance_advisor=telemetry.score,
            )
            decision = router.choose("Update the backend and frontend database flow")
            self.assertEqual(decision.model_id, "a")

    def test_generation_stats_aggregate_per_model(self):
        with tempfile.TemporaryDirectory() as td:
            telemetry = ModelPerformanceTelemetry(Path(td))
            telemetry.record_generation(
                model_id="qwen3-14b", role="primary_coder",
                prompt_tokens=512, completion_tokens=128,
                elapsed_seconds=4.0, predicted_per_second=32.0,
                prompt_per_second=640.0, time_to_first_token_ms=210.5,
            )
            telemetry.record_generation(
                model_id="qwen3-14b", role="primary_coder",
                prompt_tokens=256, completion_tokens=64,
                elapsed_seconds=2.0, predicted_per_second=30.0,
            )
            telemetry.record_generation(
                model_id="coder-7b", completion_tokens=32,
                elapsed_seconds=1.0, predicted_per_second=55.0,
            )
            summary = telemetry.generation_summary()
            self.assertEqual(summary["generation_count"], 3)
            rows = {r["model_id"]: r for r in summary["models"]}
            qwen = rows["qwen3-14b"]
            self.assertEqual(qwen["samples"], 2)
            self.assertAlmostEqual(qwen["avg_predicted_per_second"], 31.0)
            self.assertAlmostEqual(qwen["avg_time_to_first_token_ms"], 210.5)
            self.assertEqual(qwen["last_completion_tokens"], 64)
            self.assertEqual(rows["coder-7b"]["avg_time_to_first_token_ms"], None)

            reloaded = ModelPerformanceTelemetry(Path(td))
            self.assertEqual(reloaded.generation_summary()["generation_count"], 3)


if __name__ == "__main__":
    unittest.main()
