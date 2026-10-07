"""Database migration awareness.

Detects migration systems by their on-disk shape — no subprocesses, so
``migrations_detect`` is a filesystem.read tool. Status/generate/apply/
check run each system's real CLI via the shell lane (shell.execute
permission) and parse its output where the format is stable.

Systems: Alembic, Django, Prisma, Entity Framework, Flyway, Rails.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from ..procutil import no_window_flags
from .base import ToolRegistry, ToolSpec

_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv",
              "dist", "build", "target", ".agent", ".idea", ".vs"}
_MAX_DEPTH = 5
_MAX_FILES = 4000


def _iter_tree(root: Path):
    """Yield files up to depth/size bounds."""
    stack = [(root, 0)]
    seen = 0
    while stack and seen < _MAX_FILES:
        d, depth = stack.pop()
        try:
            children = list(d.iterdir())
        except OSError:
            continue
        for c in children:
            if c.is_dir() and not c.is_symlink():
                if depth < _MAX_DEPTH and c.name not in _SKIP_DIRS \
                        and not c.name.startswith("."):
                    stack.append((c, depth + 1))
            elif c.is_file():
                seen += 1
                yield c
                if seen >= _MAX_FILES:
                    return


def _alembic_root(root: Path, files: list[Path]) -> Path | None:
    for f in files:
        if f.name == "alembic.ini":
            return f.parent
        if f.name == "env.py" and f.parent.name in ("alembic", "migrations"):
            return f.parent.parent
    return None


def _has_django(root: Path, files: list[Path]) -> bool:
    has_manage = any(f.name == "manage.py" and f.parent == root for f in files)
    has_migrations = any(
        f.name == "__init__.py" and f.parent.name == "migrations"
        for f in files)
    return has_manage and has_migrations


def _has_ef(root: Path, files: list[Path]) -> bool:
    for f in files:
        if f.suffix == ".csproj":
            try:
                if "EntityFrameworkCore" in f.read_text(
                        encoding="utf-8", errors="replace"):
                    return True
            except OSError:
                pass
        if f.name.endswith("ModelSnapshot.cs"):
            return True
    return False


def _has_flyway(root: Path, files: list[Path]) -> bool:
    for f in files:
        if f.name in ("flyway.conf", "flyway.toml"):
            return True
        if f.suffix == ".sql" and re.match(r"^[VRU]\d", f.name):
            return True
    return False


def _has_rails(root: Path, files: list[Path]) -> bool:
    return any(
        f.suffix == ".rb" and f.parent.name == "migrate"
        and f.parent.parent.name == "db" for f in files)


def _count_migrations(root: Path, files: list[Path], system: str) -> int:
    n = 0
    for f in files:
        if system == "alembic" and f.parent.name == "versions" \
                and f.suffix == ".py":
            n += 1
        elif system == "django" and f.parent.name == "migrations" \
                and re.match(r"^\d{4}_", f.name):
            n += 1
        elif system == "prisma" and f.parent.name == "migration.sql":
            n += 1
        elif system == "ef" and f.parent.name == "Migrations" \
                and re.match(r"^\d{14}_", f.name):
            n += 1
        elif system == "flyway" and f.suffix == ".sql" \
                and re.match(r"^V\d", f.name):
            n += 1
        elif system == "rails" and f.parent.name == "migrate" \
                and re.match(r"^\d{14}_", f.name):
            n += 1
    return n


# system -> command templates. Each entry is a list of argv prefixes;
# callables receive (name/message) for generate.
_COMMANDS: dict[str, dict[str, Any]] = {
    "alembic": {
        "binary": "alembic",
        "status": [["alembic", "current"], ["alembic", "heads"]],
        "history": [["alembic", "history", "-v"]],
        "generate": lambda m: ["alembic", "revision", "--autogenerate",
                               "-m", m],
        "apply": [["alembic", "upgrade", "head"]],
        "check": [["alembic", "check"]],
    },
    "django": {
        "binary": "python",
        "status": [["python", "manage.py", "showmigrations", "--list"]],
        "history": [["python", "manage.py", "showmigrations", "--list"]],
        "generate": lambda m: ["python", "manage.py", "makemigrations"] +
                              (["-n", m] if m else []),
        "apply": [["python", "manage.py", "migrate"]],
        "check": [["python", "manage.py", "makemigrations",
                   "--check", "--dry-run"]],
    },
    "prisma": {
        "binary": "npx",
        "status": [["npx", "prisma", "migrate", "status"]],
        "history": [["npx", "prisma", "migrate", "status"]],
        "generate": lambda m: ["npx", "prisma", "migrate", "dev",
                               "--name", m or "migration"],
        "apply": [["npx", "prisma", "migrate", "deploy"]],
        "check": [["npx", "prisma", "migrate", "status"]],
    },
    "ef": {
        "binary": "dotnet",
        "status": [["dotnet", "ef", "migrations", "list"]],
        "history": [["dotnet", "ef", "migrations", "list"]],
        "generate": lambda m: ["dotnet", "ef", "migrations", "add",
                               m or "Migration"],
        "apply": [["dotnet", "ef", "database", "update"]],
        "check": [["dotnet", "ef", "migrations",
                   "has-pending-model-changes"]],
    },
    "flyway": {
        "binary": "flyway",
        "status": [["flyway", "info"]],
        "history": [["flyway", "info"]],
        # Flyway has no generator — versioned SQL files are authored.
        # The tool scaffolds a V<ts>__name.sql stub instead.
        "generate": None,
        "apply": [["flyway", "migrate"]],
        "check": [["flyway", "validate"]],
    },
    "rails": {
        "binary": "rails",
        "status": [["rails", "db:migrate:status"]],
        "history": [["rails", "db:migrate:status"]],
        "generate": lambda m: ["rails", "g", "migration",
                               m or "Migration"],
        "apply": [["rails", "db:migrate"]],
        "check": [["rails", "db:migrate:status"]],
    },
}


def detect_systems(root: Path) -> list[dict]:
    """Pure file-shape detection — no subprocesses."""
    root = Path(root)
    files = list(_iter_tree(root))
    found = []
    ar = _alembic_root(root, files)
    if ar is not None:
        found.append(("alembic", ar))
    pr = next((f.parent.parent for f in files
               if f.name == "schema.prisma"
               and f.parent.name == "prisma"), None)
    if pr is not None:
        found.append(("prisma", pr))
    if _has_django(root, files):
        found.append(("django", root))
    if _has_ef(root, files):
        found.append(("ef", root))
    if _has_flyway(root, files):
        found.append(("flyway", root))
    if _has_rails(root, files):
        found.append(("rails", root))
    out = []
    for system, sroot in found:
        sroot = Path(sroot)
        rel_files = [f for f in files
                     if str(f).startswith(str(sroot))]
        out.append({
            "system": system,
            "root": str(sroot),
            "declared_migrations": _count_migrations(
                sroot, rel_files, system),
            "binary": _COMMANDS[system]["binary"],
        })
    return out


def _parse_pending(system: str, text: str) -> list[str]:
    """Best-effort pending-migration list from status output."""
    pending: list[str] = []
    if system == "django":
        for line in text.splitlines():
            if line.strip().startswith("[ ]"):
                pending.append(line.strip())
    elif system == "flyway":
        for line in text.splitlines():
            if "Pending" in line:
                pending.append(line.strip())
    elif system == "rails":
        for line in text.splitlines():
            if line.strip().lower().startswith("down"):
                pending.append(line.strip())
    elif system == "prisma":
        for line in text.splitlines():
            if re.search(r"not been applied|pending", line, re.I):
                pending.append(line.strip())
    # alembic/ef: status output needs pair-comparison — callers report
    # raw status text rather than pretending a parse exists.
    return pending


def register_migration_tools(
        registry: ToolRegistry, workspace: Path,
        runner: Callable | None = None,
        extra_roots=None) -> None:

    def _run(root: Path, argv: list[str],
             timeout: int = 120) -> tuple[int, str]:
        if runner is not None:
            return runner(root, argv, timeout)
        try:
            proc = subprocess.run(
                argv, cwd=root, text=True, capture_output=True,
                timeout=timeout, creationflags=no_window_flags())
            return proc.returncode, (proc.stdout + proc.stderr).rstrip()
        except FileNotFoundError:
            return 127, f"{argv[0]}: command not found"
        except subprocess.TimeoutExpired:
            return 124, f"timed out after {timeout}s"
        except OSError as exc:
            return 1, str(exc)

    def _roots() -> list[Path]:
        roots = [workspace.resolve()]
        if extra_roots:
            try:
                for r in extra_roots() or []:
                    p = Path(r).resolve()
                    if p not in roots:
                        roots.append(p)
            except Exception:
                pass
        return roots

    def _root_for(args: dict) -> Path:
        raw = str(args.get("path", "") or "").strip()
        if not raw:
            return workspace.resolve()
        cand = Path(raw)
        root = (workspace / cand).resolve() if not cand.is_absolute() \
            else cand.resolve()
        if not any(root == b or b in root.parents for b in _roots()):
            raise ValueError(
                f"path '{raw}' is outside registered workspaces")
        return root

    def _systems_for(args: dict) -> tuple[Path, list[dict]]:
        root = _root_for(args)
        found = detect_systems(root)
        want = str(args.get("system", "") or "").strip().lower()
        if want:
            found = [f for f in found if f["system"] == want]
            if not found:
                raise ValueError(
                    f"no '{want}' migration setup detected under {root}")
        return root, found

    def migrations_detect(args: dict) -> str:
        try:
            root = _root_for(args)
        except ValueError as exc:
            return f"ERROR: {exc}"
        found = detect_systems(root)
        return json.dumps({"root": str(root), "systems": found,
                           "count": len(found)}, indent=2)

    def migrations_status(args: dict) -> str:
        try:
            root, found = _systems_for(args)
        except ValueError as exc:
            return f"ERROR: {exc}"
        report = []
        for s in found:
            system = s["system"]
            outputs, rc = [], 0
            for argv in _COMMANDS[system]["status"]:
                code, out = _run(Path(s["root"]), argv)
                rc = rc or code
                outputs.append({"argv": argv, "rc": code,
                                "output": out[-4000:]})
            report.append({
                **s,
                "outputs": outputs,
                "pending": _parse_pending(
                    system, "\n".join(o["output"] for o in outputs)),
                "declared_on_disk": s["declared_migrations"]})
        return json.dumps({"root": str(root), "systems": report},
                          indent=2)

    def migrations_generate(args: dict) -> str:
        message = str(args.get("name") or "").strip()
        try:
            root, found = _systems_for(args)
        except ValueError as exc:
            return f"ERROR: {exc}"
        results = []
        for s in found:
            system = s["system"]
            gen = _COMMANDS[system]["generate"]
            if gen is None:
                # Flyway-style: scaffold a versioned SQL file.
                ts = time.strftime("%Y%m%d%H%M%S", time.gmtime())
                safe = re.sub(r"[^A-Za-z0-9_]+", "_",
                              message or "change").strip("_")
                mig_dir = Path(s["root"]) / "sql"
                mig_dir.mkdir(parents=True, exist_ok=True)
                f = mig_dir / f"V{ts}__{safe}.sql"
                if f.exists():
                    results.append({"system": system, "ok": False,
                                    "detail": f"{f.name} already exists"})
                    continue
                f.write_text(
                    "-- Migration scaffolded by Nexus Core.\n"
                    "-- Write the forward SQL below; undo goes in a "
                    "separate U/R file per Flyway conventions.\n",
                    encoding="utf-8")
                results.append({"system": system, "ok": True,
                                "file": str(f)})
                continue
            argv = gen(message)
            code, out = _run(Path(s["root"]), argv, timeout=300)
            results.append({"system": system, "argv": argv,
                            "ok": code == 0, "rc": code,
                            "output": out[-4000:]})
        return json.dumps({"root": str(root), "results": results},
                          indent=2)

    def migrations_apply(args: dict) -> str:
        try:
            root, found = _systems_for(args)
        except ValueError as exc:
            return f"ERROR: {exc}"
        if args.get("dry_run"):
            # Honest dry-run: status only — never pretend apply-sql.
            return migrations_status(args)
        results = []
        for s in found:
            for argv in _COMMANDS[s["system"]]["apply"]:
                code, out = _run(Path(s["root"]), argv, timeout=600)
                results.append({"system": s["system"], "argv": argv,
                                "ok": code == 0, "rc": code,
                                "output": out[-4000:]})
        return json.dumps({"root": str(root), "results": results},
                          indent=2)

    def migrations_check(args: dict) -> str:
        try:
            root, found = _systems_for(args)
        except ValueError as exc:
            return f"ERROR: {exc}"
        results = []
        for s in found:
            for argv in _COMMANDS[s["system"]]["check"]:
                code, out = _run(Path(s["root"]), argv, timeout=300)
                results.append({"system": s["system"], "argv": argv,
                                "ok": code == 0, "rc": code,
                                "output": out[-4000:]})
        return json.dumps({"root": str(root), "results": results},
                          indent=2)

    registry.register(ToolSpec(
        "migrations_detect",
        "Detect database migration systems in a workspace by file shape "
        "(Alembic, Django, Prisma, Entity Framework, Flyway, Rails). "
        "Filesystem-only — never runs project code.",
        {"type": "object",
         "properties": {"path": {"type": "string",
                                 "description": "workspace dir (default: "
                                                "primary workspace)"}}},
        "filesystem.read", migrations_detect, category="devops",
        capabilities=["migrations", "database", "detect"]))

    registry.register(ToolSpec(
        "migrations_status",
        "Run each detected migration system's status command and parse "
        "pending migrations where the output format is stable.",
        {"type": "object",
         "properties": {
             "path": {"type": "string"},
             "system": {"type": "string",
                        "description": "limit to one system"}}},
        "shell.execute", migrations_status, category="devops",
        capabilities=["migrations", "database", "status"]))

    registry.register(ToolSpec(
        "migrations_generate",
        "Create a new migration (alembic revision --autogenerate, django "
        "makemigrations, prisma migrate dev, ef migrations add, rails g "
        "migration). Flyway gets a V<ts>__name.sql scaffold.",
        {"type": "object",
         "properties": {
             "path": {"type": "string"},
             "system": {"type": "string"},
             "name": {"type": "string",
                      "description": "migration name/message"}},
         "required": ["name"]},
        "shell.execute", migrations_generate, category="devops",
        capabilities=["migrations", "database", "generate"]))

    registry.register(ToolSpec(
        "migrations_apply",
        "Apply pending migrations (alembic upgrade head, django migrate, "
        "prisma migrate deploy, ef database update, flyway migrate, rails "
        "db:migrate). dry_run=true reports status without applying.",
        {"type": "object",
         "properties": {
             "path": {"type": "string"},
             "system": {"type": "string"},
             "dry_run": {"type": "boolean", "default": False}}},
        "shell.execute", migrations_apply, category="devops",
        capabilities=["migrations", "database", "apply"]))

    registry.register(ToolSpec(
        "migrations_check",
        "Model-vs-migration consistency checks (alembic check, django "
        "makemigrations --check, prisma status, ef "
        "has-pending-model-changes, flyway validate, rails status).",
        {"type": "object",
         "properties": {
             "path": {"type": "string"},
             "system": {"type": "string"}}},
        "shell.execute", migrations_check, category="devops",
        capabilities=["migrations", "database", "verify"]))
