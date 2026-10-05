"""Self-modification / update bootstrapper.

A bounded, staged update path:

    plan()   — inspect a source checkout: is it a git repo, how far ahead
               of the running install is it (VERSION + upstream commits)?
    apply()  — pull → optional unit tests → PyInstaller backend build →
               LKG snapshot of the live install → stage to backend-new/ →
               write update.flag for the desktop host to swap at launch.

A running frozen exe cannot overwrite itself, so apply() NEVER touches
the live backend — it stages and flags. Rollback symmetry: the LKG
snapshot is taken BEFORE staging so a bad build restores in one flag.

Deliberately NOT an agent tool: self-modification stays behind explicit
REST calls carrying ``confirm: true``.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from .fsutil import atomic_write_text

# Mirrors the PyInstaller invocation in packaging/build_windows.ps1 —
# backend-only; the desktop host and installer are not rebuilt here.
_BUILD_HIDDEN_IMPORTS = [
    "pybcj", "pyppmd", "pyzstd", "brotli", "Brotli", "inflate64",
    "multivolumefile", "Cryptodome",
]
_BUILD_COLLECT_ALL = [
    "cryptography", "onnxruntime", "kokoro_onnx", "phonemizer",
    "espeakng_loader", "numpy",
]


def _git(source: Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(source), *args],
        capture_output=True, text=True, timeout=timeout)


class SelfUpdate:
    def __init__(self, app_dir: Path, source_dir: Path, lkg,
                 *, runner: Callable | None = None) -> None:
        self.app_dir = Path(app_dir)
        self.source_dir = Path(source_dir)
        self.lkg = lkg
        self._runner = runner or self._default_runner

    @staticmethod
    def _default_runner(cmd: list[str], cwd: Path, timeout: int) -> dict:
        try:
            p = subprocess.run(cmd, cwd=str(cwd), capture_output=True,
                               text=True, timeout=timeout)
            return {"rc": p.returncode,
                    "out": (p.stdout or "")[-20000:],
                    "err": (p.stderr or "")[-20000:]}
        except subprocess.TimeoutExpired:
            return {"rc": -1, "out": "", "err": f"timeout after {timeout}s"}
        except OSError as exc:
            return {"rc": -1, "out": "", "err": str(exc)}

    # ------------------------------------------------------------------
    def _version(self, root: Path) -> str:
        try:
            return (root / "VERSION").read_text().strip()
        except OSError:
            return ""

    def plan(self, *, fetch: bool = True) -> dict:
        src = self.source_dir
        if not (src / ".git").exists():
            return {"ok": False, "reason": f"{src} is not a git checkout"}
        head = _git(src, "rev-parse", "--short", "HEAD")
        if fetch:
            _git(src, "fetch", "--quiet", timeout=120)
        behind = _git(src, "rev-list", "--count", "HEAD..@{u}")
        dirty = _git(src, "status", "--porcelain")
        buildable = (src / "packaging" / "chat_nexus_backend_entry.py").is_file()
        return {
            "ok": True,
            "source": str(src),
            "head": head.stdout.strip() if head.returncode == 0 else "",
            "commits_behind": int(behind.stdout.strip() or 0)
                if behind.returncode == 0 else -1,
            "dirty": bool(dirty.stdout.strip()) if dirty.returncode == 0 else None,
            "source_version": self._version(src),
            "installed_version": self._version(self.app_dir),
            "buildable": buildable,
        }

    # ------------------------------------------------------------------
    def _build_cmd(self, src: Path, out: Path, work: Path) -> list[str]:
        cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
               "--onedir", "--console", "--name", "ChatNexus.Backend",
               "--distpath", str(out), "--workpath", str(work),
               "--specpath", str(src / "build"),
               "--add-data", f"{src / 'VERSION'};.",
               "--add-data", f"{src / 'web'};web",
               "--add-data", f"{src / 'tools' / 'manifests'};tools/manifests",
               "--collect-submodules", "localcodeagent",
               "--collect-data", "localcodeagent",
               "--collect-submodules", "py7zr",
               "--add-data",
               f"{src / 'localcodeagent' / 'voice' / 'official'};"
               "localcodeagent/voice/official"]
        for c in _BUILD_COLLECT_ALL:
            cmd += ["--collect-all", c]
        for h in _BUILD_HIDDEN_IMPORTS:
            cmd += ["--hidden-import", h]
        cmd.append("packaging/chat_nexus_backend_entry.py")
        return cmd

    def apply(self, *, run_tests: bool = True, build: bool = True,
              timeout_s: int = 900) -> dict:
        """Run the staged update. Returns per-stage evidence — a stage that
        fails stops the chain and reports exactly where."""
        stages: list[dict] = []
        started = time.time()

        def stage(name: str, ok: bool, detail: str = "") -> dict:
            stages.append({"name": name, "ok": ok,
                           "detail": str(detail)[:2000]})
            return {"ok": ok, "stages": stages}

        plan = self.plan()
        if not plan.get("ok"):
            return stage("plan", False, plan.get("reason", "plan failed"))
        stage("plan", True,
              f"behind={plan['commits_behind']} "
              f"installed={plan['installed_version']} "
              f"source={plan['source_version']}")
        if plan.get("dirty"):
            return stage("guard", False,
                         "source checkout has uncommitted changes — "
                         "refusing to build an unreproducible update")

        # 1 — pull
        pull = _git(self.source_dir, "pull", "--ff-only", timeout=120)
        if pull.returncode != 0:
            return stage("pull", False,
                         (pull.stderr or pull.stdout or "pull failed")[:500])
        stage("pull", True, (pull.stdout or "").strip()[:300])

        # 2 — tests (source tree; never install-side)
        if run_tests:
            t = self._runner([sys.executable, "-m", "unittest", "discover",
                              "-s", "tests", "-t", "."],
                             self.source_dir, timeout_s)
            if t["rc"] != 0:
                return stage("tests", False, (t["err"] or t["out"])[-1500:])
            tail = [ln for ln in (t["out"] + t["err"]).splitlines()
                    if ln.strip().startswith(("OK", "FAILED", "Ran "))]
            stage("tests", True, " | ".join(tail[-3:]) or "suite passed")

        # 3 — build
        if build:
            dist = self.source_dir / "build" / "selfupdate-dist"
            work = self.source_dir / "build" / "selfupdate-work"
            b = self._runner(self._build_cmd(self.source_dir, dist, work),
                             self.source_dir, timeout_s)
            produced = dist / "ChatNexus.Backend" / "ChatNexus.Backend.exe"
            if b["rc"] != 0 or not produced.is_file():
                return stage("build", False, (b["err"] or b["out"])[-1500:]
                             or "backend exe not produced")
            stage("build", True, str(produced))
        else:
            produced = self.source_dir / "build" / "selfupdate-dist" \
                / "ChatNexus.Backend" / "ChatNexus.Backend.exe"
            if not produced.is_file():
                return stage("build", False,
                             "build=False and no prior build output exists")
            stage("build", True, "reusing prior build output")

        # 4 — LKG snapshot BEFORE staging (rollback must exist first)
        snap = self.lkg.snapshot(
            label=f"pre-update {plan['installed_version']}")
        if not snap.get("ok"):
            return stage("lkg_snapshot", False, "snapshot failed")
        stage("lkg_snapshot", True, snap["name"])

        # 5 — stage to backend-new/ (atomic-ish: fresh dir, flag last)
        staged = self.app_dir / "backend-new"
        try:
            if staged.exists():
                shutil.rmtree(staged)
            shutil.copytree(produced.parent, staged)
        except OSError as exc:
            return stage("stage", False, str(exc))
        stage("stage", True, str(staged))

        # 6 — flag for the host
        new_version = self._version(
            produced.parent / "_internal") or plan["source_version"]
        upd = self.lkg.request_update(
            "backend-new", version=new_version or plan["source_version"])
        if not upd.get("ok"):
            return stage("flag", False, upd.get("reason", ""))
        stage("flag", True, new_version or plan["source_version"])
        return {"ok": True, "stages": stages,
                "version": new_version or plan["source_version"],
                "elapsed_s": round(time.time() - started, 1),
                "detail": ("Update staged — it applies the next time "
                           "Nexus Core starts.")}
