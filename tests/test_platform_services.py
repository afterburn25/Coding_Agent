"""Connectors, knowledge graph, skills, simulation, eval lab, repo RAG."""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from localcodeagent.connectors import Connector, ConnectorRegistry
from localcodeagent.knowledge import KnowledgeGraph
from localcodeagent.skills import SkillRegistry
from localcodeagent.simulate import simulate_plan
from localcodeagent.eval import EvalLab, ExperimentStore
from localcodeagent.rag import RepoIndex


class FakeConnector(Connector):
    name = "fake-svc"
    capabilities = ("fetch", "write")
    permission = "network.read"
    rate_limit_per_min = 3

    def health(self):
        return {"ok": True, "latency_ms": 5}

    def call(self, capability, **params):
        if capability == "fetch":
            return {"ok": True, "data": params.get("url", "")}
        if capability == "write":
            raise RuntimeError("boom")
        return {"ok": False}


class ConnectorTests(unittest.TestCase):
    def _reg(self, perm="allow"):
        return ConnectorRegistry(permission_check=lambda p: perm)

    def test_call_and_audit(self):
        reg = self._reg()
        reg.register(FakeConnector())
        out = reg.call("fake-svc", "fetch", url="http://x")
        self.assertTrue(out["ok"])
        self.assertTrue(any(a["event"] == "call" for a in reg.audit))

    def test_permission_denied(self):
        reg = self._reg(perm="deny")
        reg.register(FakeConnector())
        out = reg.call("fake-svc", "fetch")
        self.assertFalse(out["ok"])
        self.assertIn("permission", out["error"])

    def test_unknown_capability(self):
        reg = self._reg()
        reg.register(FakeConnector())
        self.assertFalse(reg.call("fake-svc", "delete")["ok"])

    def test_rate_limit(self):
        reg = self._reg()
        reg.register(FakeConnector())
        for _ in range(3):
            self.assertTrue(reg.call("fake-svc", "fetch")["ok"])
        self.assertFalse(reg.call("fake-svc", "fetch")["ok"])  # 4th denied

    def test_error_tracked_and_recovers(self):
        reg = self._reg()
        reg.register(FakeConnector())
        self.assertFalse(reg.call("fake-svc", "write")["ok"])
        rec = reg.connectors["fake-svc"]
        self.assertEqual(rec["consecutive_errors"], 1)
        reg.call("fake-svc", "fetch")
        self.assertEqual(rec["consecutive_errors"], 0)

    def test_disabled(self):
        reg = self._reg()
        reg.register(FakeConnector())
        reg.set_enabled("fake-svc", False)
        self.assertFalse(reg.call("fake-svc", "fetch")["ok"])


class KnowledgeGraphTests(unittest.TestCase):
    def test_entities_edges_neighbors(self):
        with tempfile.TemporaryDirectory() as td:
            kg = KnowledgeGraph(Path(td) / "kg.db")
            self.addCleanup(kg.close)
            kg.add_entity("project", "nexus", attrs={"lang": "py"})
            kg.add_entity("file", "server.py")
            kg.add_entity("tool", "shell")
            kg.link("project:nexus", "file:server.py", "contains")
            kg.link("file:server.py", "tool:shell", "uses")
            sub = kg.neighbors("project:nexus", depth=2)
            self.assertEqual(len(sub["nodes"]), 3)
            self.assertEqual(len(sub["edges"]), 2)
            kg.close()

    def test_upsert_and_find(self):
        with tempfile.TemporaryDirectory() as td:
            kg = KnowledgeGraph(Path(td) / "kg.db")
            self.addCleanup(kg.close)
            kg.add_entity("model", "qwen", attrs={"size": 14})
            kg.add_entity("model", "qwen", attrs={"size": 30})
            ent = kg.get_entity("model:qwen")
            self.assertEqual(ent["attrs"]["size"], 30)
            self.assertEqual(len(kg.find_entities(kind="model")), 1)
            kg.close()

    def test_context_for(self):
        with tempfile.TemporaryDirectory() as td:
            kg = KnowledgeGraph(Path(td) / "kg.db")
            self.addCleanup(kg.close)
            kg.add_entity("repo", "agent")
            kg.add_entity("decision", "use-sqlite")
            kg.link("repo:agent", "decision:use-sqlite", "decided")
            ctx = kg.context_for("agent")
            self.assertIn("decided", ctx)
            kg.close()

    def test_link_resolves_names_to_entity_ids(self):
        # link() must canonicalize endpoints — a bare name previously stored
        # as the raw string never joined neighbors()/context_for().
        with tempfile.TemporaryDirectory() as td:
            kg = KnowledgeGraph(Path(td) / "kg.db")
            self.addCleanup(kg.close)
            kg.add_entity("service", "Gateway")
            e = kg.link("Gateway", "Stripe", "depends_on")
            self.assertEqual(e["src"], "service:Gateway")
            self.assertEqual(e["dst"], "entity:Stripe")
            ctx = kg.context_for("Gateway")
            self.assertIn("depends_on", ctx)
            kg.close()

    def test_neighbors_matches_legacy_name_stored_edges(self):
        # Edges written before canonicalization kept raw names — neighbors
        # must still find them via the entity's name.
        with tempfile.TemporaryDirectory() as td:
            kg = KnowledgeGraph(Path(td) / "kg.db")
            self.addCleanup(kg.close)
            kg.add_entity("service", "Gateway")
            import sqlite3 as _sq
            raw = _sq.connect(str(kg.path))
            try:
                raw.execute(
                    "INSERT INTO edges(id,src,dst,rel,attrs,created_at)"
                    " VALUES('e-legacy','Gateway','Stripe','depends_on','{}',0)")
                raw.commit()
            finally:
                raw.close()
            sub = kg.neighbors("service:Gateway", depth=1)
            self.assertEqual(len(sub["edges"]), 1)
            kg.close()

    def test_persists_across_instances(self):
        with tempfile.TemporaryDirectory() as td:
            kg = KnowledgeGraph(Path(td) / "kg.db")
            self.addCleanup(kg.close)
            kg.add_entity("service", "comfyui")
            kg.close()
            kg2 = KnowledgeGraph(Path(td) / "kg.db")
            self.addCleanup(kg2.close)
            self.assertEqual(kg2.stats()["entities"], 1)
            kg2.close()


def _mk_skill(d: Path, name="demo", **extra):
    d.mkdir(parents=True, exist_ok=True)
    (d / "skill.json").write_text(json.dumps({
        "name": name, "version": "1.0.0", "description": "demo skill",
        "capabilities": ["research"], "tools": ["web_search"],
        "permissions": ["network.read"], "instructions": "do the thing",
        **extra}))


class SkillTests(unittest.TestCase):
    def test_scan_list_enable(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            _mk_skill(ws / "skills" / "demo")
            reg = SkillRegistry(ws)
            rows = reg.list()
            self.assertEqual(rows[0]["name"], "demo")
            self.assertTrue(rows[0]["enabled"])
            reg.set_enabled("demo", False)
            self.assertFalse(reg.list()[0]["enabled"])
            self.assertEqual(reg.enabled(), [])

    def test_install_and_remove(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            src = Path(td) / "incoming" / "web-research"
            _mk_skill(src, "web-research")
            reg = SkillRegistry(ws)
            out = reg.install(src)
            self.assertTrue(out["ok"])
            self.assertTrue(reg.remove("web-research"))
            self.assertEqual(reg.list(), [])

    def test_invalid_skill_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            bad = Path(td) / "bad"
            bad.mkdir()
            (bad / "skill.json").write_text('{"name": "x"}')  # no description
            reg = SkillRegistry(ws)
            out = reg.install(bad)
            self.assertFalse(out["ok"])
            self.assertTrue(out["errors"])

    def test_instructions_and_tools(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            _mk_skill(ws / "skills" / "demo")
            reg = SkillRegistry(ws)
            self.assertIn("do the thing", reg.instructions_for())
            self.assertIn("web_search", reg.allowed_tools())

    def test_disabled_persists(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            _mk_skill(ws / "skills" / "demo")
            reg = SkillRegistry(ws)
            reg.set_enabled("demo", False)
            reg2 = SkillRegistry(ws)
            self.assertFalse(reg2.list()[0]["enabled"])


class SimulationTests(unittest.TestCase):
    def test_simulation_does_not_execute(self):
        marker = Path(tempfile.mkdtemp()) / "created.txt"
        plan = {"steps": [
            {"title": "write file", "tool": "filesystem.write",
             "files": [str(marker)]},
            {"title": "run tests", "tool": "shell.execute"}]}
        out = simulate_plan(plan)
        self.assertTrue(out["simulated"])
        self.assertFalse(marker.exists())  # never executed
        self.assertEqual(out["step_count"], 2)
        self.assertIn("filesystem.write", out["permissions_required"])

    def test_policy_verdicts(self):
        class P:
            def check(self, action):
                # policy.check() receives an action class (run_shell),
                # not the raw permission id.
                return {"run_shell": "ask"}.get(action, "allow")
        plan = {"steps": [{"title": "x", "tool": "shell.execute"}]}
        out = simulate_plan(plan, policy=P(),
                            permission_map={"shell.execute": "shell.execute"})
        self.assertEqual(out["approvals_expected"], 1)
        self.assertTrue(out["failure_points"])

    def test_mission_dag_nodes_translate_to_steps(self):
        plan = {"nodes": [
            {"id": "n1", "title": "Index repo", "kind": "job",
             "metadata": {"job": "rag_update"}},
            {"id": "n2", "title": "Fix bug", "kind": "agent"},
            {"id": "n3", "title": "Verify", "kind": "verify"},
            {"id": "n4", "title": "Render diagram", "kind": "job",
             "metadata": {"job": "image"}},
            {"id": "n5", "title": "Wait", "kind": "wait"}]}
        out = simulate_plan(plan)
        self.assertTrue(out["simulated"])
        self.assertEqual(out["step_count"], 5)
        self.assertIn("filesystem.read", out["permissions_required"])
        self.assertIn("shell.execute", out["permissions_required"])
        self.assertIn("image.generate", out["permissions_required"])
        # Explicit steps win over nodes when both are present.
        out2 = simulate_plan({**plan,
                              "steps": [{"title": "s", "tool": "git.write"}]})
        self.assertEqual(out2["step_count"], 1)


class EvalLabTests(unittest.TestCase):
    def test_suite_run_and_history(self):
        with tempfile.TemporaryDirectory() as td:
            lab = EvalLab(Path(td))
            cases = [{"id": "c1"}, {"id": "c2"}]
            run = lab.run_suite("routing", cases,
                                lambda c: {"passed": c["id"] == "c1",
                                           "score": 1.0 if c["id"] == "c1" else 0.0},
                                subject="model-a")
            self.assertEqual(run["passed"], 1)
            self.assertEqual(len(lab.history(suite="routing")), 1)

    def test_compare(self):
        with tempfile.TemporaryDirectory() as td:
            lab = EvalLab(Path(td))
            cases = [{"id": "c1"}]
            lab.run_suite("s", cases, lambda c: {"passed": True, "score": 0.8},
                          subject="a")
            lab.run_suite("s", cases, lambda c: {"passed": True, "score": 0.9},
                          subject="b")
            cmp = lab.compare("s", "a", "b")
            self.assertTrue(cmp["ok"])
            self.assertAlmostEqual(cmp["delta_score"], 0.1)


class ExperimentTests(unittest.TestCase):
    def test_lifecycle(self):
        with tempfile.TemporaryDirectory() as td:
            ex = ExperimentStore(Path(td))
            e = ex.create("B beats A on latency",
                          arms=[{"name": "a", "config": {"model": "m1"}},
                                {"name": "b", "config": {"model": "m2"}}],
                          metric="latency")
            ex.record_result(e["id"], "a", metrics={"latency": 1.5})
            ex.record_result(e["id"], "b", metrics={"latency": 1.1})
            ex.conclude(e["id"], "b is faster")
            rows = ex.list()
            self.assertEqual(rows[0]["status"], "concluded")
            self.assertEqual(rows[0]["arms"][1]["result"]["latency"], 1.1)

    def test_reproducible_config(self):
        with tempfile.TemporaryDirectory() as td:
            ex = ExperimentStore(Path(td))
            e = ex.create("h", arms=[{"name": "x", "config": {"ctx": 8192}}])
            self.assertEqual(e["arms"][0]["config"]["ctx"], 8192)


class RagTests(unittest.TestCase):
    def _repo(self, td: str) -> Path:
        ws = Path(td)
        (ws / "src").mkdir()
        (ws / "src" / "main.py").write_text(
            "def handler():\n    return 42\n\nclass Greeter:\n    pass\n")
        (ws / "src" / "util.py").write_text("def helper():\n    pass\n")
        (ws / "README.md").write_text("# repo\narchitecture notes here\n")
        return ws

    def test_index_and_search(self):
        with tempfile.TemporaryDirectory() as td:
            ws = self._repo(td)
            idx = RepoIndex(ws)
            self.addCleanup(idx.close)
            out = idx.update()
            self.assertGreaterEqual(out["added"], 3)
            hits = idx.search("handler")
            self.assertTrue(any(h.get("name") == "handler" for h in hits))
            hits2 = idx.search("architecture")
            self.assertTrue(any(h["file"].endswith("README.md") for h in hits2))
            idx.close()

    def test_incremental_update(self):
        with tempfile.TemporaryDirectory() as td:
            ws = self._repo(td)
            idx = RepoIndex(ws)
            self.addCleanup(idx.close)
            idx.update()
            # Touch only one file's content.
            (ws / "src" / "util.py").write_text(
                "def helper():\n    pass\n\ndef new_fn():\n    pass\n")
            import os
            os.utime(ws / "src" / "util.py",
                     (time.time() + 2, time.time() + 2))
            out = idx.update()
            self.assertEqual(out["updated"], 1)
            self.assertTrue(any(h.get("name") == "new_fn"
                                for h in idx.search("new_fn")))
            idx.close()

    def test_deleted_file_dropped(self):
        with tempfile.TemporaryDirectory() as td:
            ws = self._repo(td)
            idx = RepoIndex(ws)
            self.addCleanup(idx.close)
            idx.update()
            (ws / "src" / "util.py").unlink()
            out = idx.update()
            self.assertEqual(out["removed"], 1)
            self.assertEqual(idx.search("helper"), [])
            idx.close()


if __name__ == "__main__":
    unittest.main()
