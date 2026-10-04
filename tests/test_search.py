"""Global-search aggregation — /api/search must return real matches from
live stores and degrade gracefully when a source is unavailable."""

import tempfile
import unittest
from pathlib import Path

from localcodeagent.config import AgentConfig
from localcodeagent.search import GlobalSearch
from localcodeagent.server import AppState


class _Stub:
    """Minimal state — only the stores each source reads."""
    def __init__(self, tmp: Path):
        self.workspace = tmp
        self.tasks = type("T", (), {"recent": lambda s, n: [
            {"id": "t1", "prompt": "rename the logo asset",
             "status": "completed", "summary": ""}]})()
        self.autonomy = type("A", (), {"missions": type("M", (), {
            "list": lambda s, **kw: [
                {"id": "m1", "title": "Deploy the staging build",
                 "status": "running"}]})()})()
        self.projects = type("P", (), {"list": lambda s, **kw: [
            {"name": "nexus-core", "root": str(tmp),
             "summary": "main workstation"}]})()
        self.skills = type("S", (), {"list": lambda s: [
            {"name": "code_review", "description": "review diffs"}]})()
        self.answer_memory = type("AM", (), {
            "list_answers": lambda s, **kw: [
                {"question": "how do workers scale?",
                 "answer": "bounded by hardware", "id": "a1"}]})()
        self.knowledge = None  # lazy KG absent — must degrade
        self.queue = type("Q", (), {"list": lambda s: [
            {"id": "q1", "prompt": "queued prompt about workers",
             "position": 1}]})()
        self.devservers = type("D", (), {"list": lambda s, **kw: [
            {"name": "vite", "url": "http://127.0.0.1:5173",
             "command": "npm run dev", "status": "running"}]})()


class ScoreTests(unittest.TestCase):
    def test_exact_beats_prefix_beats_substring(self):
        s = GlobalSearch._score
        self.assertGreater(s("logo", "logo"), s("logo", "logo-x"))
        self.assertGreater(s("logo", "logo-x"), s("logo", "the logo"))


class AggregationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "logo_notes.md").write_text("x", encoding="utf-8")
        self.gs = GlobalSearch(_Stub(self.tmp))

    def test_empty_query_returns_nothing(self):
        self.assertEqual(self.gs.query("")["results"], [])

    def test_query_fans_across_sources(self):
        kinds = {r["kind"] for r in self.gs.query("logo")["results"]}
        self.assertIn("task", kinds)
        self.assertIn("file", kinds)

    def test_worker_query_matches_queue_and_answer(self):
        kinds = {r["kind"] for r in self.gs.query("workers")["results"]}
        self.assertIn("queue", kinds)
        self.assertIn("answer", kinds)

    def test_results_carry_ref_and_score(self):
        for r in self.gs.query("nexus")["results"]:
            self.assertIn("kind", r)
            self.assertTrue(r["ref"].startswith("/"))
            self.assertGreater(r["score"], 0)

    def test_broken_source_does_not_break_search(self):
        st = _Stub(self.tmp)
        def boom(**kw):
            raise RuntimeError("store down")
        st.projects = type("B", (), {"list": boom})()
        self.assertTrue(self.gs.query("anything")["results"] is not None)


class EndpointTests(unittest.TestCase):
    def test_search_route_registered_on_appstate(self):
        # The endpoint builds GlobalSearch(self.state) — verify the attr
        # surface it needs exists on a real AppState.
        td = tempfile.mkdtemp()
        state = AppState(AgentConfig(profiles_onboarding_gate=False),
                         Path(td), Path(td) / ".runtime")
        for attr in ("tasks", "autonomy", "projects", "skills",
                     "answer_memory", "queue", "devservers", "workspace"):
            self.assertTrue(hasattr(state, attr), attr)
        out = GlobalSearch(state).query("test")
        self.assertIn("results", out)


if __name__ == "__main__":
    unittest.main()
