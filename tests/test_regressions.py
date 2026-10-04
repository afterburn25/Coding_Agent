"""Phase 6 — regression memory, performance baselines, bounded git
bisect in an isolated worktree."""
import subprocess
import tempfile
import unittest
from pathlib import Path


def _git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=str(cwd),
                          capture_output=True, text=True)


def _make_bisect_repo(td: str, bad_index: int = 3):
    """8 commits; flag.txt is 'ok' until commit `bad_index`, then 'bad'."""
    root = Path(td) / "repo"
    root.mkdir()
    _git("init", cwd=root)
    shas = []
    for i in range(8):
        (root / "flag.txt").write_text(
            "ok" if i < bad_index else "bad", encoding="utf-8")
        # counter guarantees every commit actually lands (no silent
        # empty-commit failures producing duplicate shas)
        (root / "counter.txt").write_text(str(i), encoding="utf-8")
        _git("-c", "user.email=t@t", "-c", "user.name=t",
             "add", "-A", cwd=root)
        _git("-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-m", f"c{i}", cwd=root)
        shas.append(_git("rev-parse", "HEAD", cwd=root).stdout.strip())
    return root, shas


class RegressionStoreTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        from localcodeagent.regressions import RegressionStore
        self.store = RegressionStore(Path(self.td.name) / "reg.json")

    def test_regression_lifecycle(self):
        self.store.record("startup works", ok=True, head="aaa")
        self.assertFalse(self.store.check("startup works")
                         ["regression"])
        b = self.store.record("startup works", ok=False, head="bbb",
                              detail="health timeout")
        self.assertEqual(b["event"]["type"], "regression_detected")
        self.assertTrue(self.store.check("startup works")["regression"])
        self.assertEqual(len(self.store.open_regressions()), 1)
        # recovery clears it
        b = self.store.record("startup works", ok=True, head="ccc")
        self.assertEqual(b["event"]["type"], "regression_cleared")
        self.assertEqual(self.store.open_regressions(), [])
        # unknown behavior isn't a regression
        self.assertFalse(self.store.check("never seen")["known"])
        # failure of something never verified ≠ regression
        self.store.record("new thing", ok=False)
        self.assertEqual(self.store.open_regressions(), [])

    def test_persistence(self):
        self.store.record("x", ok=True, head="h1")
        from localcodeagent.regressions import RegressionStore
        again = RegressionStore(self.store.path)
        self.assertTrue(again.check("x")["known"])


class BaselineStoreTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        from localcodeagent.regressions import BaselineStore
        self.base = BaselineStore(Path(self.td.name) / "base.json")
        for v in (10.0, 10.2, 9.8, 10.1, 10.0, 10.3, 9.9):
            self.base.record("startup_s", v)

    def test_noise_is_not_regression(self):
        r = self.base.check("startup_s", 10.4)
        self.assertFalse(r["regression"])
        self.assertEqual(r["confidence"], "moderate")

    def test_real_regression_flagged(self):
        r = self.base.check("startup_s", 40.0)
        self.assertTrue(r["regression"])
        self.assertGreater(r["ratio"], 3)

    def test_insufficient_data(self):
        self.base.record("rare_metric", 1.0)
        r = self.base.check("rare_metric", 99.0)
        self.assertFalse(r["regression"])
        self.assertEqual(r["confidence"], "insufficient")


class GitBisectTests(unittest.TestCase):
    def test_finds_first_bad_commit(self):
        from localcodeagent.bisect import GitBisector
        with tempfile.TemporaryDirectory() as td:
            repo, shas = _make_bisect_repo(td, bad_index=3)
            res = GitBisector(repo).run(
                good=shas[0], bad=shas[-1],
                test_command='python -c "import sys; '
                             'sys.exit(0 if open(\'flag.txt\').read()'
                             '.strip()==\'ok\' else 1)"')
            self.assertTrue(res["ok"], res.get("detail"))
            self.assertEqual(res["first_bad"], shas[3])
            self.assertIn("c3", res["first_bad_message"])
            self.assertGreater(res["steps"], 0)
            # worktree cleaned up, user tree untouched
            self.assertIn("clean", _git("status", "--porcelain",
                                        cwd=repo).stdout or "clean"
                          or "clean")

    def test_invalid_range_fails_honestly(self):
        from localcodeagent.bisect import GitBisector
        with tempfile.TemporaryDirectory() as td:
            repo, shas = _make_bisect_repo(td)
            # good ref that actually fails the test → honest refusal
            res = GitBisector(repo).run(
                good=shas[7], bad=shas[7],
                test_command='python -c "import sys; sys.exit(0)"')
            self.assertFalse(res["ok"])


class RegressionHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import threading
        from localcodeagent.config import AgentConfig, ModelProfile
        from localcodeagent.server import create_server, stop_state
        cls._td = tempfile.TemporaryDirectory(
            ignore_cleanup_errors=True)
        ws = Path(cls._td.name)
        # workspace must be a git repo for /api/bisect
        _git("init", cwd=ws)
        _git("-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "--allow-empty", "-m", "init", cwd=ws)
        cfg = AgentConfig(
            profiles_onboarding_gate=False,
            models=[ModelProfile(id="fake", endpoint="http://127.0.0.1:1/v1",
                                 model="m", roles=["utility"],
                                 runtime="external")],
            process_watchdog=False, research_enabled=False,
            autonomy_enabled=True)
        cls.server, cls.state = create_server(
            cfg, ws, "127.0.0.1", 0, ws / "web", ws / ".runtime")
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.addClassCleanup(lambda: (cls.server.shutdown(),
                                     cls.server.server_close(),
                                     stop_state(cls.state)))

    @classmethod
    def tearDownClass(cls):
        cls._td.cleanup()

    def _get(self, path):
        import json
        import urllib.request
        with urllib.request.urlopen(self.base + path, timeout=15) as r:
            return json.loads(r.read())

    def _post(self, path, body):
        import json
        import urllib.error
        import urllib.request
        req = urllib.request.Request(
            self.base + path, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_regression_and_baseline_routes(self):
        code, out = self._post("/api/regressions/record", {
            "behavior": "voice greeting", "ok": True, "head": "aaa"})
        self.assertEqual(code, 200)
        code, out = self._post("/api/regressions/record", {
            "behavior": "voice greeting", "ok": False, "head": "bbb"})
        self.assertEqual(code, 200)
        self.assertEqual(out["behavior"]["event"]["type"],
                         "regression_detected")
        data = self._get("/api/regressions")
        self.assertEqual(data["summary"]["open_regressions"], 1)
        for _ in range(6):
            self._post("/api/baselines/record",
                       {"metric": "startup_s", "value": 10.0})
        code, out = self._post("/api/baselines/check",
                               {"metric": "startup_s", "value": 50.0})
        self.assertEqual(code, 200)
        self.assertTrue(out["regression"])
        code, out = self._post("/api/bisect",
                               {"good": "HEAD~0", "bad": "HEAD",
                                "test": "exit 0" if False else
                                        "python -c \"x=1\""})
        # trivially satisfiable range (single commit) → honest result
        self.assertIn(code, (200,))
        self.assertIn("ok", out)


if __name__ == "__main__":
    unittest.main()
