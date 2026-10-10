#!/usr/bin/env python3
"""Repeated state-preservation test for deploy_local.ps1.

Builds a scratch "install" tree seeded with representative persistent
state under every protected location, then repeatedly:

    build -> deploy -> restart -> update -> failed/partial deploy
        -> retry deploy -> restart

verifying after every cycle that no persistent state was silently lost.
The governing invariant:

    Deployment must never destroy user state merely because two
    locations disagree. Conflicts are preserved, never silently
    deleted.

Usage:
    python scripts/state_preservation_test.py [--cycles 3] [--keep]

Exits non-zero on any state loss.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "scripts" / "deploy_local.ps1"

# Representative persistent state -> where it lives under the install.
# Mirrors deploy_local.ps1's protected set: data, output, runtime,
# models, tools, .git, .agent, .nexus, .repair-worktrees, config.json.
STATE_FILES = {
    "data/profile/user.json": "user profile",
    "data/conversations/store.jsonl": "conversation log",
    "data/brain/brain.json": "Brain knowledge",
    "data/brain/history/snap-001.json": "Brain history snapshot",
    "data/answer_memory/store.json": "Answer Memory",
    "data/identity/manager.json": "Identity Manager",
    "data/vault/secrets.vault": "Vault (opaque blob)",
    "data/github/state.json": "GitHub state",
    "data/social/graph.json": "social state",
    "data/social/peers/peer-01.json": "peer dossier",
    "data/consults/open-c7.json": "open consult",
    "data/autonomy/missions.json": "active mission",
    "data/autonomy/workstreams/ws-1.json": "workstream",
    "data/approvals/pending-a9.json": "pending approval",
    "output/artifacts/release-notes.md": "artifact",
    "data/image/config.json": "image configuration",
    "data/skills/generated/release-build.json": "generated skill",
    "data/procedures/proc-42.json": "learned procedure",
    ".agent/ledger/tasks.jsonl": "task ledger",
    ".agent/checkpoints/cp-1.json": "agent checkpoint",
    ".nexus/lane-2/workfile.txt": "live mission lane worktree",
    ".repair-worktrees/fix-3/patch.diff": "repair worktree",
    "runtime/voice/chatterbox/model.bin": "provisioned runtime",
    "models/qwen3-4b.gguf.marker": "model payload marker",
    "tools/InvokeAI/invoke.yaml": "tool payload",
    "config.json": "user configuration",
}

BUNDLE_FILES = {
    "NexusCore.exe": "binary-payload-v{n}",
    "backend/ChatNexus.Backend.exe": "backend-payload-v{n}",
    "backend/appsettings.json": '{{"version": "{n}"}}',
    "resources/app/version.txt": "{n}",
    # Real deploys ship Source\ as a fresh clone — it must exist in the
    # bundle or /MIR treats the whole tree as *EXTRA and purges it
    # wholesale, deeper /XD name exclusions notwithstanding.
    "Source/tracked.txt": "tracked v1\n",
    "Source/app.py": "print('app v{n}')\n",
}


def _seed_state(dest: Path) -> dict[str, str]:
    """Write every representative state file; return relpath->sha256."""
    manifest = {}
    for rel, label in STATE_FILES.items():
        p = dest / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        body = f"{label} :: seeded {time.time()}\n"
        p.write_text(body, encoding="utf-8")
        manifest[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return manifest


def _snapshot(dest: Path) -> dict[str, str]:
    out = {}
    for rel in STATE_FILES:
        p = dest / rel
        if p.is_file():
            out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def _build_bundle(bundle: Path, n: int) -> None:
    if bundle.exists():
        shutil.rmtree(bundle)
    for rel, tmpl in BUNDLE_FILES.items():
        p = bundle / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(tmpl.format(n=n), encoding="utf-8")


def _init_source(dest: Path) -> None:
    """A minimal git repo at install\\Source — the deploy preserves
    uncommitted work inside it."""
    src = dest / "Source"
    src.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=src, check=True)
    (src / "tracked.txt").write_text("tracked v1\n")
    subprocess.run(["git", "add", "-A"], cwd=src, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "init"], cwd=src, check=True)


def _deploy(source: Path, dest: Path) -> tuple[int, str]:
    r = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", str(DEPLOY), "-Source", str(source),
         "-Dest", str(dest)],
        capture_output=True, text=True, timeout=300)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


class PreservationFailure(Exception):
    pass


def _verify(dest: Path, manifest: dict[str, str], label: str,
            allow_extra: bool = True) -> None:
    now = _snapshot(dest)
    missing = [r for r in manifest if r not in now]
    changed = [r for r in manifest
               if r in now and now[r] != manifest[r]]
    lost = missing + changed
    if lost:
        raise PreservationFailure(
            f"{label}: {len(lost)} state file(s) lost or rewritten: "
            f"{lost[:5]}")


def run(cycles: int, keep: bool) -> int:
    tmp = Path(tempfile.mkdtemp(prefix="nexus-preserve-"))
    try:
        bundle = tmp / "bundle"
        dest = tmp / "install"
        dest.mkdir(parents=True)
        _init_source(dest)
        manifest = _seed_state(dest)

        print(f"scratch: {tmp}")
        print(f"state files: {len(manifest)}")

        failures = 0
        for n in range(1, cycles + 1):
            # ---- build -------------------------------------------------
            _build_bundle(bundle, n)

            # ---- dirty Source work (uncommitted user/mission output) ---
            dirty = dest / "Source" / "deliverable.md"
            dirty.write_text(f"mission output cycle {n}\n")

            # ---- failure mode varies by cycle --------------------------
            locker = helper = None
            if n % 2 == 0:
                # Locked exe: a mandatory-share lock on the deployed
                # binary forces a partial mirror (rc>=8) — deploy must
                # report it honestly, not silently half-land.
                lock_path = dest / "NexusCore.exe"
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                if not lock_path.exists():
                    lock_path.write_bytes(b"old-binary")
                locker = subprocess.Popen(
                    ["powershell", "-NoProfile", "-Command",
                     "$f=[System.IO.File]::Open("
                     f"'{lock_path}','Open','ReadWrite','None');"
                     "Start-Sleep -Seconds 25;$f.Close()"])
                time.sleep(1.5)  # let the lock take
            else:
                # Live process under the install root — deploy must reap
                # it before mirroring rather than failing mid-copy.
                helper_exe = dest / "nexus-helper.exe"
                shutil.copy(os.environ.get(
                    "COMSPEC", r"C:\Windows\System32\cmd.exe"), helper_exe)
                helper = subprocess.Popen(
                    [str(helper_exe), "/c", "ping", "-t", "127.0.0.1"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            # ---- deploy -------------------------------------------------
            rc, out = _deploy(bundle, dest)
            if locker is not None:
                locker.terminate()
            if helper is not None:
                helper.terminate()

            # rc>=8 means partial/failed mirror; deploy_local then
            # relaunches and throws — state must still be intact and the
            # dirty Source file restored from its snapshot.
            _verify(dest, manifest, f"cycle {n} deploy rc={rc}")
            if helper is not None and helper.poll() is None:
                raise PreservationFailure(
                    f"cycle {n}: process under install root survived "
                    f"deploy — mirror could have failed mid-copy")
            if not dirty.is_file() or \
                    dirty.read_text() != f"mission output cycle {n}\n":
                raise PreservationFailure(
                    f"cycle {n}: uncommitted Source work lost "
                    f"(rc={rc})\n{out[-800:]}")
            if rc != 0:
                # partial deploy — bundle changes may be incomplete, but
                # state + preserved work must survive. Retry must
                # converge.
                rc2, out2 = _deploy(bundle, dest)
                _verify(dest, manifest, f"cycle {n} retry rc={rc2}")
                if rc2 != 0:
                    print(f"cycle {n}: retry still rc={rc2} — "
                          f"reporting honestly, continuing")
                    failures += 1
            else:
                new_ver = (dest / "resources/app/version.txt")
                if not new_ver.is_file() or \
                        new_ver.read_text().strip() != str(n):
                    raise PreservationFailure(
                        f"cycle {n}: bundle change not applied "
                        f"(version.txt={new_ver.read_text()!r})")

            # ---- restart: user edits config between restarts -----------
            cfg = dest / "config.json"
            cfg.write_text(json.dumps({"cycle": n, "user": "after"}))
            manifest["config.json"] = hashlib.sha256(
                cfg.read_bytes()).hexdigest()

            print(f"cycle {n}: deploy rc={rc} — state intact "
                  f"({len(manifest)} files)")

        # ---- final: config must carry the last user edit ---------------
        last = json.loads((dest / "config.json").read_text())
        if last.get("cycle") != cycles:
            raise PreservationFailure("final config.json lost user edits")

        print(f"\n{'PASS' if failures == 0 else 'FAIL'} — {cycles} cycles, "
              f"{len(manifest)} state files preserved throughout"
              + (f" ({failures} retry failures)" if failures else ""))
        return 1 if failures else 0
    except PreservationFailure as e:
        print(f"\nFAIL — {e}")
        return 1
    finally:
        if not keep:
            shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycles", type=int, default=3)
    ap.add_argument("--keep", action="store_true",
                    help="keep the scratch tree for inspection")
    args = ap.parse_args()
    return run(args.cycles, args.keep)


if __name__ == "__main__":
    raise SystemExit(main())
