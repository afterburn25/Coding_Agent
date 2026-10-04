"""Phase 4 — shared evidence board and contradiction handling."""
import tempfile
import unittest
from pathlib import Path


class EvidenceBoardTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        from localcodeagent.evidence import EvidenceBoard
        self.board = EvidenceBoard(Path(self.td.name) / "ev.json")

    def test_post_search_and_persist(self):
        a = self.board.post("backend responds on 8080",
                            kind="test_result", source="worker-a",
                            confidence=0.9, tags=["startup"])
        self.board.post("port 9090 is free", kind="telemetry",
                        source="worker-b")
        hits = self.board.search("backend port startup")
        self.assertTrue(any(h["id"] == a["id"] for h in hits))
        hits2 = self.board.search("startup", kinds=["telemetry"])
        self.assertFalse(any(h["id"] == a["id"] for h in hits2))
        from localcodeagent.evidence import EvidenceBoard
        again = EvidenceBoard(self.board.path)
        self.assertEqual(len(again.list()), 2)

    def test_contradiction_marks_both_and_surfaces(self):
        a = self.board.post("port is free", kind="test_result",
                            source="worker-a")
        b = self.board.post("port is held", kind="log",
                            source="worker-b", contradicts=a["id"])
        pa, pb = self.board.get(a["id"]), self.board.get(b["id"])
        self.assertEqual(pa["status"], "contradicted")
        self.assertEqual(pb["status"], "contradicted")
        self.assertEqual(len(self.board.contradictions()), 2)
        self.assertEqual(pb["contradictions"][0]["with"], a["id"])

    def test_resolve_contradiction_confirms_winner(self):
        a = self.board.post("tests pass", source="w1")
        b = self.board.post("tests fail", source="w2")
        self.board.contradict(a["id"], b["id"], note="disagree")
        # resolve the loser with the winner named — loser → resolved,
        # winner → confirmed
        out = self.board.resolve(a["id"], resolution="re-ran: b correct",
                                 winner_id=b["id"], resolver="reviewer")
        self.assertEqual(out["status"], "resolved")
        self.assertEqual(self.board.get(b["id"])["status"], "confirmed")
        self.assertEqual(self.board.contradictions(), [])
        self.assertEqual(self.board.get(b["id"])["resolution"],
                         "re-ran: b correct")

    def test_questions_and_filters(self):
        self.board.post("why does it hang?", kind="question",
                        source="w1")
        self.board.post("looks fine", kind="finding", source="w2",
                        mission_id="m1")
        self.assertEqual(len(self.board.questions()), 1)
        self.assertEqual(len(self.board.list(mission_id="m1")), 1)
        self.assertEqual(self.board.summary()["open_questions"], 1)
        # withdraw clears it from open questions
        q = self.board.questions()[0]
        self.board.withdraw(q["id"], reason="answered")
        self.assertEqual(self.board.questions(), [])

    def test_self_contradiction_and_unknown_refused(self):
        a = self.board.post("x", source="w")
        self.assertIsNone(self.board.contradict(a["id"], a["id"]))
        self.assertIsNone(self.board.contradict(a["id"], "ev-nope"))
        self.assertIsNone(self.board.resolve("ev-nope", resolution="r"))


class EvidenceHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import threading
        from localcodeagent.config import AgentConfig, ModelProfile
        from localcodeagent.server import create_server, stop_state
        cls._td = tempfile.TemporaryDirectory(
            ignore_cleanup_errors=True)
        ws = Path(cls._td.name)
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

    def test_board_over_http(self):
        code, out = self._post("/api/evidence", {
            "claim": "startup healthy", "kind": "test_result",
            "source": "worker-1", "confidence": 0.9,
            "mission_id": "m-ev"})
        self.assertEqual(code, 200, out)
        a = out["entry"]["id"]
        code, out = self._post("/api/evidence", {
            "claim": "startup hung", "kind": "log",
            "source": "worker-2", "contradicts": a})
        self.assertEqual(code, 200)
        b = out["entry"]["id"]
        data = self._get("/api/evidence?mission_id=m-ev")
        self.assertEqual(len(data["contradictions"]), 2)
        code, out = self._post("/api/evidence/resolve", {
            "id": b, "resolution": "worker-2 log was stale",
            "winner_id": a, "resolver": "reviewer"})
        self.assertEqual(code, 200)
        data = self._get("/api/evidence?mission_id=m-ev")
        self.assertEqual(data["contradictions"], [])
        # search-scoped retrieval
        hits = self._get("/api/evidence?q=startup&kinds=test_result")
        self.assertTrue(any(e["id"] == a for e in hits["entries"]))
        code, _ = self._post("/api/evidence", {"claim": ""})
        self.assertEqual(code, 400)


if __name__ == "__main__":
    unittest.main()
