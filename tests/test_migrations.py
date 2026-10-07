from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from localcodeagent.tools.base import ToolRegistry
from localcodeagent.tools.migrations import (
    detect_systems, register_migration_tools)


def _registry() -> ToolRegistry:
    return ToolRegistry({})  # empty perms map → default modes


class DetectTests(unittest.TestCase):
    def test_alembic_detected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "alembic.ini").write_text("[alembic]")
            (root / "alembic" / "versions").mkdir(parents=True)
            (root / "alembic" / "env.py").write_text("# env")
            (root / "alembic" / "versions" / "abc123_init.py") \
                .write_text("revision='abc123'")
            found = detect_systems(root)
            self.assertEqual([s["system"] for s in found], ["alembic"])
            self.assertEqual(found[0]["declared_migrations"], 1)

    def test_django_prisma_ef_flyway_rails(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "manage.py").write_text("# manage")
            (root / "app" / "migrations").mkdir(parents=True)
            (root / "app" / "migrations" / "__init__.py").write_text("")
            (root / "app" / "migrations" / "0001_initial.py").write_text("")
            (root / "prisma").mkdir()
            (root / "prisma" / "schema.prisma").write_text("model A {}")
            (root / "web").mkdir()
            (root / "web" / "web.csproj").write_text(
                "<PackageReference Include=\"Microsoft.EntityFrameworkCore\"/>")
            (root / "sql").mkdir()
            (root / "sql" / "V1__init.sql").write_text("create table t();")
            (root / "db" / "migrate").mkdir(parents=True)
            (root / "db" / "migrate" / "20240101000000_init.rb").write_text("")
            systems = {s["system"] for s in detect_systems(root)}
            self.assertEqual(
                systems, {"django", "prisma", "ef", "flyway", "rails"})

    def test_empty_tree(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(detect_systems(Path(td)), [])


class ToolTests(unittest.TestCase):
    def _tools(self, root: Path, runner=None) -> ToolRegistry:
        reg = _registry()
        register_migration_tools(reg, root, runner=runner)
        return reg

    def test_detect_tool(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "alembic.ini").write_text("[alembic]")
            reg = self._tools(root)
            out = json.loads(reg.execute("migrations_detect", {}, approved=True))
            self.assertEqual(out["count"], 1)

    def test_status_parses_django_pending(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "manage.py").write_text("")
            (root / "app" / "migrations").mkdir(parents=True)
            (root / "app" / "migrations" / "__init__.py").write_text("")

            def runner(r, argv, timeout):
                return 0, "app\n [X] 0001_initial\n [ ] 0002_auto\n"
            reg = self._tools(root, runner)
            out = json.loads(reg.execute("migrations_status", {}, approved=True))
            self.assertEqual(
                out["systems"][0]["pending"], ["[ ] 0002_auto"])

    def test_apply_runs_upgrade(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "alembic.ini").write_text("[alembic]")
            seen = []

            def runner(r, argv, timeout):
                seen.append(argv)
                return 0, "ok"
            reg = self._tools(root, runner)
            json.loads(reg.execute("migrations_apply", {}, approved=True))
            self.assertEqual(seen, [["alembic", "upgrade", "head"]])

    def test_apply_dry_run_never_mutates(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "alembic.ini").write_text("[alembic]")
            seen = []

            def runner(r, argv, timeout):
                seen.append(argv)
                return 0, "ok"
            reg = self._tools(root, runner)
            json.loads(reg.execute(
                "migrations_apply", {"dry_run": True}, approved=True))
            # status commands only — no upgrade.
            self.assertTrue(all("upgrade" not in a for a in seen))
            self.assertTrue(seen)

    def test_flyway_generate_scaffolds_sql(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "flyway.conf").write_text("flyway.url=x")
            reg = self._tools(root, runner=lambda r, a, t: (0, ""))
            out = json.loads(reg.execute(
                "migrations_generate", {"name": "add users"}, approved=True))
            f = Path(out["results"][0]["file"])
            self.assertTrue(f.name.startswith("V"))
            self.assertTrue(f.name.endswith("__add_users.sql"))
            self.assertTrue(f.is_file())

    def test_unknown_system_errors(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            reg = self._tools(root)
            out = reg.execute(
                "migrations_status", {"system": "alembic"}, approved=True)
            self.assertIn("ERROR", out)


if __name__ == "__main__":
    unittest.main()
