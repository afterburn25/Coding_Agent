"""Adaptive Worker Manager tests — admission, reservations, queue reasons,
priority aging, fit-aware drain, restart reconciliation, failure backoff,
cost learning, heartbeat reaping, worktree helpers, projects store."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from localcodeagent.projects import ProjectStore
from localcodeagent.workers import (AdaptiveWorkerManager, ResourceMonitor,
                                    Reserves, classify_role, estimate_for)
from localcodeagent.workers.capacity import CapacitySnapshot
from localcodeagent.workers.roles import ResourceEstimate
from localcodeagent.workers.worktree import detect_scope_overlap


def snap(cpu=16, util=0.1, ram_free=48000, vram_free=11000, gpu=0.0,
         disk=400, hwid="testhw"):
    return CapacitySnapshot(ts=0, cpu_logical=cpu, cpu_util=util,
                            ram_total_mb=65536, ram_free_mb=ram_free,
                            vram_total_mb=12288, vram_free_mb=vram_free,
                            gpu_util=gpu, disk_free_gb=disk,
                            hardware_id=hwid)


def mgr(tmp, *, s=None, max_workers=8, reserves=None, **kw):
    s = s or snap()
    mon = ResourceMonitor(tmp, sampler=lambda: s, sample_ttl=0,
                          reserves=reserves)
    return AdaptiveWorkerManager(tmp, monitor=mon,
                                 max_workers=max_workers, **kw)


class TestAdmission(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_small_task_admits(self):
        m = mgr(self.tmp)
        out = m.submit("quick fix", role="tool")
        self.assertEqual(out["status"], "admitted")
        self.assertTrue(out["worker"]["id"].startswith("w-"))

    def test_vram_exhaustion_queues_with_reason(self):
        m = mgr(self.tmp, s=snap(vram_free=10000))
        # coding takes ~6GB; image takes 8GB — together they do not fit.
        a = m.submit("code task", role="coding")
        self.assertEqual(a["status"], "admitted")
        b = m.submit("render image", role="image")
        self.assertEqual(b["status"], "queued")
        self.assertEqual(b["reason"], "waiting_for_vram")
        self.assertEqual(b["message"], "Waiting for GPU memory")

    def test_ram_exhaustion_queues(self):
        m = mgr(self.tmp, s=snap(ram_free=4000))
        out = m.submit("big coding job", role="coding")
        self.assertEqual(out["status"], "queued")
        self.assertEqual(out["reason"], "waiting_for_ram")

    def test_cpu_pressure_queues(self):
        m = mgr(self.tmp, s=snap(util=0.95))
        out = m.submit("build", role="build_test")
        self.assertEqual(out["status"], "queued")
        self.assertEqual(out["reason"], "waiting_for_cpu")

    def test_model_slot_exhaustion_queues(self):
        m = mgr(self.tmp)
        a = m.submit("14B coding", role="coding")       # 14B slot 1/1
        self.assertEqual(a["status"], "admitted")
        b = m.submit("14B reasoning", role="reasoning")
        self.assertEqual(b["status"], "queued")
        self.assertEqual(b["reason"], "waiting_for_model")

    def test_admit_node_fires_shortfall_hook_on_vram(self):
        """A node gated on memory notifies the caller so idle managed
        runtimes can be evicted on demand rather than waiting for the
        idle timer."""
        m = mgr(self.tmp, s=snap(vram_free=2000, ram_free=48000))
        fired = []
        worker, reason, detail = m.admit_node(
            "t-1", "code task", role="coding",
            on_shortfall=lambda est, r: fired.append((est, r)))
        self.assertIsNone(worker)
        self.assertEqual(reason, "waiting_for_vram")
        self.assertEqual(len(fired), 1)
        self.assertGreater(fired[0][0].vram_mb, 0)

    def test_admit_node_shortfall_hook_ignores_non_memory(self):
        """CPU/slot/ceiling shortfalls can't be fixed by evicting a
        model — the hook must not fire for them."""
        m = mgr(self.tmp, s=snap(util=0.99, vram_free=11000))
        fired = []
        worker, reason, _ = m.admit_node(
            "t-1", "build", role="build_test",
            on_shortfall=lambda est, r: fired.append(r))
        self.assertIsNone(worker)
        self.assertEqual(reason, "waiting_for_cpu")
        self.assertEqual(fired, [])

    def test_reservation_releases_on_completion(self):
        m = mgr(self.tmp, s=snap(vram_free=10000))
        a = m.submit("code", role="coding")
        b = m.submit("image", role="image")
        self.assertEqual(b["status"], "queued")
        m.release(a["worker"]["id"], outcome="completed",
                  observed={"vram_mb": 6000, "ram_mb": 5000})
        started = m.tick()
        self.assertEqual([w.id for w in started], [b["entry"]["id"]])
        self.assertEqual(len(m.status()["queue"]), 0)

    def test_reservation_releases_on_failure_and_cancel(self):
        m = mgr(self.tmp, s=snap(vram_free=10000))
        a = m.submit("code", role="coding")
        m.release(a["worker"]["id"], outcome="failed",
                  result={"error": "crashed"})
        b = m.submit("image", role="image")
        self.assertEqual(b["status"], "admitted")
        m.cancel(b["worker"]["id"])
        c = m.submit("another image", role="image")
        self.assertEqual(c["status"], "admitted")

    def test_queue_is_fit_aware_not_head_of_line(self):
        m = mgr(self.tmp, s=snap(vram_free=6000))
        big = m.submit("huge image", role="image")       # needs 8GB — won't fit
        self.assertEqual(big["status"], "queued")
        small = m.submit("tiny tool job", role="tool")   # no VRAM — must pass
        self.assertEqual(small["status"], "admitted")

    def test_ceiling_is_dynamic_not_fixed(self):
        m = mgr(self.tmp, max_workers=3)
        for i in range(3):
            out = m.submit(f"tool {i}", role="tool")
            self.assertEqual(out["status"], "admitted")
        over = m.submit("one more", role="tool")
        self.assertEqual(over["status"], "queued")
        self.assertEqual(over["reason"], "ceiling")

    def test_interactive_reserve_tightens_background(self):
        # While the user is active, background admissions see less capacity.
        m = mgr(self.tmp, s=snap(ram_free=8000),
                interactive_probe=lambda: True)
        out = m.submit("bg coding", role="coding")  # needs 6GB RAM
        self.assertEqual(out["status"], "queued")
        self.assertEqual(out["reason"], "waiting_for_ram")
        # A user-initiated task bypasses the interactive reserve.
        u = m.submit("user coding", role="tool", user_initiated=True)
        self.assertEqual(u["status"], "admitted")

    def test_user_event_and_announce_flow(self):
        events = []
        m = mgr(self.tmp, s=snap(vram_free=1000),
                on_queue_event=lambda t, p: events.append((t, p)))
        out = m.submit("user image", role="image", user_initiated=True)
        self.assertEqual(out["status"], "queued")
        kinds = [e[0] for e in events]
        self.assertIn("task_queued", kinds)
        self.assertTrue(events[-1][1]["user_initiated"])
        self.assertIn("queue_position", events[-1][1])

    def test_explain_reports_reason(self):
        m = mgr(self.tmp, s=snap(vram_free=1000))
        out = m.submit("image", role="image")
        exp = m.explain(out["entry"]["id"])
        self.assertEqual(exp["reason"], "waiting_for_vram")
        self.assertIn("schedulable", exp)
        self.assertEqual(m.explain("nope")["reason"], "not_queued")

    def test_dependency_gate(self):
        m = mgr(self.tmp)
        a = m.submit("first", role="tool")
        b = m.submit("dependent", role="tool", deps=[a["worker"]["task_id"]]
                     if False else [])
        # deps reference task_ids — set one up explicitly
        dep = m.submit("dep work", role="tool", task_id="task-A")
        follower = m.submit("follower", role="tool", deps=["task-A"])
        self.assertEqual(follower["status"], "queued")
        self.assertEqual(follower["reason"], "waiting_for_dependency")
        m.release(dep["worker"]["id"], outcome="completed")
        started = m.tick()
        self.assertEqual([w.id for w in started], [follower["entry"]["id"]])


class TestBackoffAndLearning(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_oom_reduces_ceiling(self):
        m = mgr(self.tmp, max_workers=4)
        self.assertEqual(m.status()["capacity"]["ceiling"], 4)
        w = m.submit("x", role="tool")["worker"]
        m.worker_started(w["id"])
        m.release(w["id"], outcome="failed",
                  result={"error": "CUDA out of memory"})
        self.assertEqual(m.status()["capacity"]["ceiling"], 3)

    def test_ceiling_recovers_slowly(self):
        m = mgr(self.tmp, max_workers=4)
        m.record_resource_failure()
        self.assertEqual(m.status()["capacity"]["ceiling"], 3)
        for _ in range(5):
            w = m.submit("x", role="tool")["worker"]
            m.worker_started(w["id"])
            m.release(w["id"], outcome="completed")
        self.assertEqual(m.status()["capacity"]["ceiling"], 4)

    def test_cost_learning_converges(self):
        m = mgr(self.tmp)
        # teach it image jobs really only use ~3GB VRAM on this hardware
        for _ in range(3):
            w = m.submit("img", role="image")["worker"]
            m.worker_started(w["id"])
            m.release(w["id"], outcome="completed",
                      observed={"vram_mb": 3000, "ram_mb": 2000})
        m2 = mgr(self.tmp)  # fresh manager, learned costs persist on disk
        est = m2._estimate("image", None)
        self.assertLess(est.vram_mb, 5000)
        self.assertGreater(est.vram_mb, 1500)

    def test_never_started_release_skips_learning(self):
        m = mgr(self.tmp)
        w = m.submit("x", role="tool")["worker"]
        m.release(w["id"], outcome="cancelled")  # no worker_started
        self.assertEqual(m.status()["capacity"]["ceiling"],
                         m.max_workers)

    def test_heartbeat_reap_releases_reservation(self):
        import time
        m = mgr(self.tmp, s=snap(vram_free=10000))
        a = m.submit("code", role="coding")["worker"]
        m.worker_started(a["id"])
        b = m.submit("img", role="image")
        self.assertEqual(b["status"], "queued")
        # simulate a dead worker
        m._workers[a["id"]].heartbeat_at = time.time() - 10000
        m.reconcile()
        self.assertEqual(m.tick()[0].id, b["entry"]["id"])


class TestDurability(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_queue_survives_restart(self):
        m = mgr(self.tmp, s=snap(vram_free=1000))
        out = m.submit("queued work", role="image")
        self.assertEqual(out["status"], "queued")
        # new process → same queue file
        m2 = mgr(self.tmp, s=snap(vram_free=12000))
        self.assertEqual(len(m2.status()["queue"]), 1)
        started = m2.tick()
        self.assertEqual(len(started), 1)

    def test_queue_cap(self):
        from localcodeagent.workers.manager import MAX_QUEUE
        m = mgr(self.tmp, s=snap(vram_free=100))
        for _ in range(MAX_QUEUE):
            m.submit("img", role="image")
        out = m.submit("overflow", role="image")
        self.assertEqual(out["status"], "rejected")


class TestClassification(unittest.TestCase):
    def test_keywords(self):
        self.assertEqual(classify_role("run the test suite"), "build_test")
        self.assertEqual(classify_role("generate an image"), "image")
        self.assertEqual(classify_role("implement a parser"), "coding")
        self.assertEqual(classify_role("audit this diff"), "reviewer")
        self.assertEqual(classify_role("random words", kind="verify"),
                         "build_test")

    def test_hint_wins(self):
        self.assertEqual(classify_role("do stuff", hinted="research"),
                         "research")

    def test_estimate_overrides(self):
        e = estimate_for("tool", overrides={"ram_mb": 42})
        self.assertEqual(e.ram_mb, 42)


class TestWorktreeHelpers(unittest.TestCase):
    def test_scope_overlap(self):
        out = detect_scope_overlap({
            "w1": ["web/*", "VERSION"],
            "w2": ["tests/*", "VERSION"],
            "w3": ["localcodeagent/*"]})
        self.assertEqual(out, {"VERSION": ["w1", "w2"]})


class TestProjects(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = ProjectStore(self.tmp)

    def test_create_get_list(self):
        p = self.store.create("Nexus UI", profile_id="A")
        self.assertEqual(self.store.get(p["id"])["name"], "Nexus UI")
        self.assertEqual(len(self.store.list()), 1)

    def test_profile_isolation(self):
        p = self.store.create("secret", profile_id="A")
        self.assertIsNone(self.store.get(p["id"], profile_id="B"))
        self.assertEqual(self.store.get(p["id"], profile_id="A")["id"],
                         p["id"])

    def test_goals_persist(self):
        p = self.store.create("g", profile_id="A")
        g = self.store.add_goal(p["id"], "green tests")
        store2 = ProjectStore(self.tmp)
        self.assertTrue(store2.complete_goal(p["id"], g["id"]))
        self.assertEqual(store2.get(p["id"])["goals"][0]["status"],
                         "completed")

    def test_memory_summary_buckets(self):
        p = self.store.create("m", profile_id="A")
        self.store.remember(p["id"], "decision", "use worktrees")
        self.store.remember(p["id"], "known_bug", "avatar race")
        self.store.remember(p["id"], "fix", "fixed race in app.js")
        s = self.store.memory_summary(p["id"])
        self.assertIn("use worktrees", s["decisions"])
        self.assertIn("avatar race", s["known_issues"])
        self.assertIn("fixed race in app.js", s["completed_work"])


if __name__ == "__main__":
    unittest.main()
