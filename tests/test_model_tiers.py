"""Model-tier catalog + hardware planner tests."""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

from localcodeagent.models.tiers import (
    EXECUTION_CPU_ONLY,
    EXECUTION_GPU_NATIVE,
    EXECUTION_HYBRID,
    HardwareProfile,
    canonical_role,
    classify_tier,
    ladder_role_for_tier,
    model_tiers,
    plan_model_stack,
    tier_by_id,
    tier_for_role,
    usable_ram_gb,
)


def profile(**kw) -> HardwareProfile:
    base = dict(dedicated_vram_gb=12.0, gpu_vram_gb_list=[12.0],
                total_ram_gb=64.0, available_ram_gb=50.0,
                cpu_logical_cores=16, disk_free_gb=500.0,
                gpu_name="Test GPU", gpu_vendor="nvidia",
                gpu_backend="cuda")
    base.update(kw)
    return HardwareProfile(**base)


class CatalogTests(unittest.TestCase):
    def test_catalog_loads_and_is_ordered(self):
        tiers = model_tiers()
        self.assertGreaterEqual(len(tiers), 4)
        self.assertEqual([t.tier for t in tiers], sorted(t.tier for t in tiers))
        for t in tiers:
            self.assertTrue(t.filename.endswith(".gguf"))
            self.assertTrue(t.url.startswith("https://"))
            self.assertEqual(len(t.sha256), 64)
            self.assertTrue(t.roles)

    def test_tier_ids_unique(self):
        ids = [t.id for t in model_tiers()]
        self.assertEqual(len(ids), len(set(ids)))

    def test_ladder_roles_present(self):
        roles = {t.ladder_role for t in model_tiers()}
        self.assertIn("utility", roles)
        self.assertIn("lightweight_reasoner", roles)
        self.assertIn("primary_coder", roles)
        self.assertIn("deep_reasoner", roles)


class ClassificationTests(unittest.TestCase):
    def test_12gb_64gb_hybrid_30b(self):
        """The documented target case: 12 GB VRAM + 64 GB RAM should
        qualify the 30B as hybrid (one tier above the 14B native)."""
        p = profile()
        plan = plan_model_stack(p)
        by_id = {d.tier.id: d for d in plan.decisions}
        self.assertEqual(by_id["qwen3-14b-q4-k-m"].execution, EXECUTION_GPU_NATIVE)
        t30 = by_id["qwen3-coder-30b-a3b-q4-k-m"]
        self.assertEqual(t30.execution, EXECUTION_HYBRID)
        self.assertTrue(t30.supported)

    def test_low_ram_blocks_hybrid(self):
        p = profile(total_ram_gb=32.0, available_ram_gb=20.0)
        plan = plan_model_stack(p)
        t30 = next(d for d in plan.decisions if d.tier.tier == 4)
        self.assertEqual(t30.execution, "unsupported")

    def test_no_gpu_cpu_only(self):
        p = profile(dedicated_vram_gb=0.0, gpu_vram_gb_list=[],
                    gpu_vendor="", gpu_name="", gpu_backend="cpu")
        plan = plan_model_stack(p)
        execs = {d.tier.tier: d.execution for d in plan.decisions}
        for tier_no in execs:
            self.assertIn(execs[tier_no],
                          {EXECUTION_CPU_ONLY, "unsupported"})

    def test_hybrid_distance_cap(self):
        """Hybrid can exceed native tier by at most max_tier_distance."""
        p = profile(dedicated_vram_gb=4.0, gpu_vram_gb_list=[4.0])
        plan = plan_model_stack(p)
        # 4 GB VRAM: tier 1 native. Tier 3+4 must not hybrid-climb.
        by_tier = {d.tier.tier: d.execution for d in plan.decisions}
        self.assertEqual(by_tier[1], EXECUTION_GPU_NATIVE)
        self.assertEqual(by_tier[4], "unsupported")

    def test_shared_memory_never_counts(self):
        p = profile(dedicated_vram_gb=2.0, gpu_vram_gb_list=[2.0],
                    shared_gpu_memory_gb=16.0)
        tiers = model_tiers()
        t2 = next(t for t in tiers if t.tier == 2)
        execution, _, _ = classify_tier(t2, p)
        self.assertNotEqual(execution, EXECUTION_GPU_NATIVE)

    def test_disk_pressure_blocks_selection(self):
        p = profile(disk_free_gb=5.0)
        plan = plan_model_stack(p)
        self.assertFalse(plan.disk_ok)
        self.assertFalse(plan.selected)

    def test_usable_ram_reserve(self):
        p = profile(total_ram_gb=64.0)
        # reserve = max(10, 64*0.15)=10 → usable ~54
        self.assertAlmostEqual(usable_ram_gb(p), 64.0 - 10.0, places=1)
        small = profile(total_ram_gb=16.0)
        self.assertAlmostEqual(usable_ram_gb(small), 16.0 - 10.0, places=1)


class RoleTests(unittest.TestCase):
    def test_canonical_aliases(self):
        self.assertEqual(canonical_role("light_coder"), "lightweight_reasoner")
        self.assertEqual(canonical_role("fast_coder"), "primary_coder")
        self.assertEqual(canonical_role("deep_coder"), "deep_reasoner")
        self.assertEqual(canonical_role("utility"), "utility")
        self.assertEqual(canonical_role("unknown_xyz"), "unknown_xyz")

    def test_tier_for_role(self):
        self.assertEqual(tier_for_role("light_debugger"), 2)
        self.assertEqual(tier_for_role("primary_reasoner"), 3)
        self.assertEqual(tier_for_role("intent_classifier"), 1)
        self.assertEqual(tier_for_role("bogus"), 3)  # unknown → workhorse

    def test_ladder_role_for_tier(self):
        self.assertEqual(ladder_role_for_tier(2), "lightweight_reasoner")
        self.assertEqual(ladder_role_for_tier(99), "utility")

    def test_tier_by_id(self):
        self.assertIsNotNone(tier_by_id("qwen3-14b-q4-k-m"))
        self.assertIsNone(tier_by_id("nope"))


class InstallerSyncTests(unittest.TestCase):
    def test_installer_defines_match_catalog(self):
        root = Path(__file__).resolve().parents[1]
        r = subprocess.run(
            [sys.executable, "scripts/sync_model_catalog.py", "--check"],
            cwd=root, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0,
                         f"catalog↔installer drift: {r.stdout}{r.stderr}")


if __name__ == "__main__":
    unittest.main()
