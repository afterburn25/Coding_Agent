from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from localcodeagent.lkg import LkgStore, _sha
from localcodeagent.selfupdate import SelfUpdate


def _app(root: Path) -> Path:
    """Minimal install layout: backend/exe + config.json + VERSION."""
    app = root / "app"
    (app / "backend" / "_internal").mkdir(parents=True)
    (app / "backend" / "ChatNexus.Backend.exe").write_bytes(b"old-exe")
    (app / "backend" / "_internal" / "mod.bin").write_bytes(b"x")
    (app / "config.json").write_text("{}")
    (app / "VERSION").write_text("0.19.0\n")
    return app


class LkgTests(unittest.TestCase):
    def _store(self, td: str, app: Path) -> LkgStore:
        return LkgStore(app, Path(td) / "state" / "lkg")

    def test_snapshot_verify_restore(self):
        with tempfile.TemporaryDirectory() as td:
            app = _app(Path(td))
            lkg = self._store(td, app)
            out = lkg.snapshot(label="pre-update")
            self.assertTrue(out["ok"])
            name = out["name"]
            self.assertEqual(lkg.latest(), name)
            self.assertTrue(lkg.verify(name)["ok"])
            # Break the install — rollback must restore the snapshot.
            (app / "backend" / "ChatNexus.Backend.exe").write_bytes(b"new")
            (app / "VERSION").write_text("9.9.9")
            res = lkg.apply_rollback(name)
            self.assertTrue(res["ok"], res)
            self.assertEqual(
                (app / "backend" / "ChatNexus.Backend.exe").read_bytes(),
                b"old-exe")
            self.assertEqual((app / "VERSION").read_text(), "0.19.0\n")

    def test_verify_detects_tampered_exe(self):
        with tempfile.TemporaryDirectory() as td:
            app = _app(Path(td))
            lkg = self._store(td, app)
            name = lkg.snapshot()["name"]
            snap = lkg.root / name
            (snap / "backend" / "ChatNexus.Backend.exe").write_bytes(b"tamper")
            v = lkg.verify(name)
            self.assertFalse(v["ok"])
            self.assertIn("hash", v["reason"])
            # And a tampered snapshot cannot be restored or flagged.
            self.assertFalse(lkg.request_rollback(name)["ok"])
            self.assertFalse(lkg.apply_rollback(name)["ok"])

    def test_rollback_flag_roundtrip_and_single_consumption(self):
        with tempfile.TemporaryDirectory() as td:
            app = _app(Path(td))
            lkg = self._store(td, app)
            name = lkg.snapshot()["name"]
            out = lkg.request_rollback(reason="repeated crashes")
            self.assertTrue(out["ok"])
            self.assertTrue(lkg.rollback_flag.exists())
            flag = lkg.consume_rollback()
            self.assertEqual(flag["name"], name)
            self.assertEqual(flag["reason"], "repeated crashes")
            # Consumed — a second read must not re-trigger the restore.
            self.assertIsNone(lkg.consume_rollback())

    def test_update_flag_roundtrip_requires_exe(self):
        with tempfile.TemporaryDirectory() as td:
            app = _app(Path(td))
            lkg = self._store(td, app)
            # No staged build yet → refused.
            self.assertFalse(lkg.request_update()["ok"])
            staged = app / "backend-new"
            (staged / "_internal").mkdir(parents=True)
            (staged / "ChatNexus.Backend.exe").write_bytes(b"new")
            out = lkg.request_update(version="0.20.0")
            self.assertTrue(out["ok"], out)
            flag = lkg.consume_update()
            self.assertEqual(flag["staged_dir"], "backend-new")
            self.assertEqual(flag["version"], "0.20.0")
            self.assertIsNone(lkg.consume_update())

    def test_version_floor_refuses_stale_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            app = _app(Path(td))
            lkg = self._store(td, app)
            old = lkg.snapshot()["name"]
            self.assertEqual(lkg.floor(), "0.19.0")
            # Install a newer build that proves itself — the floor
            # ratchets and the older snapshot becomes unrestorable.
            (app / "VERSION").write_text("0.20.0\n")
            new = lkg.snapshot()["name"]
            self.assertEqual(lkg.floor(), "0.20.0")
            for fn in (lkg.request_rollback, lkg.apply_rollback):
                out = fn(old)
                self.assertFalse(out["ok"], out)
                self.assertIn("floor", out["reason"])
            # The current-version snapshot stays restorable.
            self.assertTrue(lkg.request_rollback(new)["ok"])
            lkg.consume_rollback()
            # Floor surfaces in status/listing for diagnostics.
            st = lkg.status()
            self.assertEqual(st["floor"], "0.20.0")
            flags = {s["name"]: s["below_floor"] for s in st["snapshots"]}
            self.assertTrue(flags[old])
            self.assertFalse(flags[new])

    def test_mark_proven_only_ratchets_up(self):
        with tempfile.TemporaryDirectory() as td:
            app = _app(Path(td))
            lkg = self._store(td, app)
            lkg.mark_proven("0.19.0")
            lkg.mark_proven("0.18.5")
            self.assertEqual(lkg.floor(), "0.19.0")
            lkg.mark_proven("0.20.0")
            self.assertEqual(lkg.floor(), "0.20.0")
            lkg.mark_proven("garbage")
            self.assertEqual(lkg.floor(), "0.20.0")

    def test_verify_detects_incoherent_versions(self):
        with tempfile.TemporaryDirectory() as td:
            app = _app(Path(td))
            lkg = self._store(td, app)
            name = lkg.snapshot()["name"]
            snap = lkg.root / name
            # A mixed-version bundle (the avatar/voice incident shape)
            # must never verify.
            (snap / "backend" / "_internal" / "VERSION").write_text("9.9.9")
            v = lkg.verify(name)
            self.assertFalse(v["ok"])
            self.assertIn("coherence", v["reason"])
            self.assertFalse(lkg.request_rollback(name)["ok"])

    def test_prunes_to_three_snapshots(self):
        with tempfile.TemporaryDirectory() as td:
            app = _app(Path(td))
            lkg = self._store(td, app)
            names = [lkg.snapshot()["name"] for _ in range(5)]
            remaining = sorted(p.name for p in lkg.root.glob("snap-*"))
            self.assertEqual(len(remaining), 3)
            self.assertEqual(remaining, names[2:])


class SelfUpdateTests(unittest.TestCase):
    def test_plan_rejects_non_repo(self):
        with tempfile.TemporaryDirectory() as td:
            app = _app(Path(td))
            lkg = LkgStore(app, Path(td) / "lkg")
            upd = SelfUpdate(app, Path(td) / "nosuch", lkg)
            self.assertFalse(upd.plan()["ok"])

    def _repo(self, td: str) -> Path:
        src = Path(td) / "src"
        src.mkdir()
        (src / "VERSION").write_text("0.20.0\n")
        (src / "packaging").mkdir()
        (src / "packaging" / "chat_nexus_backend_entry.py").write_text("x")
        (src / "web").mkdir()
        (src / "tools").mkdir()
        (src / "tests").mkdir()
        (src / "localcodeagent" / "voice" / "official").mkdir(parents=True)
        subprocess.run(["git", "init"], cwd=src, check=True,
                       capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=src,
                       check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=src,
                       check=True, capture_output=True)
        subprocess.run(["git", "add", "-A"], cwd=src, check=True,
                       capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=src,
                       check=True, capture_output=True)
        return src

    def test_apply_stages_update_and_lkg(self):
        with tempfile.TemporaryDirectory() as td:
            app = _app(Path(td))
            src = self._repo(td)
            lkg = LkgStore(app, Path(td) / "lkg")
            upd = SelfUpdate(app, src, lkg)

            def fake_git(self_, *a, **k):
                # git rev-parse → a hash; everything else succeeds quietly.
                cp = subprocess.CompletedProcess(a, 0, stdout="abc123\n",
                                                 stderr="")
                if a[:1] == ("rev-parse",):
                    cp.stdout = "abc123\n"
                elif a[:1] == ("rev-list",):
                    cp.stdout = "0\n"
                elif a[:1] == ("status",):
                    cp.stdout = ""
                return cp

            fake_build = src / "build" / "selfupdate-dist" \
                / "ChatNexus.Backend" / "ChatNexus.Backend.exe"
            fake_build.parent.mkdir(parents=True)
            fake_build.write_bytes(b"new-exe")
            (fake_build.parent / "_internal").mkdir()
            (fake_build.parent / "_internal" / "VERSION").write_text("0.20.0\n")

            with patch("localcodeagent.selfupdate._git", fake_git):
                out = upd.apply(run_tests=False, build=False)
            self.assertTrue(out["ok"], out)
            names = [s["name"] for s in out["stages"]]
            # LKG snapshot must precede staging — rollback has to exist
            # before the staged build does.
            self.assertLess(names.index("lkg_snapshot"),
                            names.index("stage"))
            staged = app / "backend-new" / "ChatNexus.Backend.exe"
            self.assertEqual(staged.read_bytes(), b"new-exe")
            flag = lkg.consume_update()
            self.assertEqual(flag["version"], "0.20.0")
            # Live install untouched — the host swaps at launch.
            self.assertEqual(
                (app / "backend" / "ChatNexus.Backend.exe").read_bytes(),
                b"old-exe")
            self.assertTrue(lkg.latest())

    def test_apply_refuses_dirty_checkout(self):
        with tempfile.TemporaryDirectory() as td:
            app = _app(Path(td))
            src = self._repo(td)
            (src / "VERSION").write_text("dirty\n")
            lkg = LkgStore(app, Path(td) / "lkg")
            upd = SelfUpdate(app, src, lkg)
            out = upd.apply(run_tests=False, build=False)
            self.assertFalse(out["ok"])
            guard = [s for s in out["stages"] if s["name"] == "guard"]
            self.assertTrue(guard and not guard[0]["ok"])
            # Nothing staged or flagged.
            self.assertIsNone(lkg.consume_update())


class PendingSwapTests(unittest.TestCase):
    """Generalized deferred replacement — stage now, host swaps at boot."""

    def test_stage_file_swap(self):
        with tempfile.TemporaryDirectory() as td:
            app = _app(Path(td))
            src = Path(td) / "new.bin"
            src.write_bytes(b"replacement-bytes")
            lkg = LkgStore(app, Path(td) / "lkg")
            out = lkg.stage_swap("backend/_internal/foo.dll", src,
                                 reason="native dep update")
            self.assertTrue(out["ok"])
            self.assertEqual(out["target"], "backend/_internal/foo.dll")
            swaps = lkg.pending_swaps()
            self.assertEqual(len(swaps), 1)
            row = swaps[0]
            self.assertEqual(row["kind"], "file")
            self.assertEqual(row["sha256"], _sha(src))
            payload = lkg.swaps_dir / row["id"] / "payload"
            self.assertEqual(payload.read_bytes(), b"replacement-bytes")

    def test_stage_dir_swap(self):
        with tempfile.TemporaryDirectory() as td:
            app = _app(Path(td))
            src = Path(td) / "pkgdir"
            (src / "mod").mkdir(parents=True)
            (src / "mod" / "x.py").write_text("x=1\n")
            lkg = LkgStore(app, Path(td) / "lkg")
            out = lkg.stage_swap("backend/_internal/pkg", src)
            self.assertTrue(out["ok"])
            self.assertEqual(out["kind"], "dir")
            row = lkg.pending_swaps()[0]
            self.assertEqual(row["sha256"], "")

    def test_rejects_escape_and_data_targets(self):
        with tempfile.TemporaryDirectory() as td:
            app = _app(Path(td))
            src = Path(td) / "f.txt"
            src.write_text("x")
            lkg = LkgStore(app, Path(td) / "lkg")
            for bad in ("../evil.dll", "/abs/x.dll", "..\\up",
                        "data/lkg/swaps/x", "", "."):
                out = lkg.stage_swap(bad, src)
                self.assertFalse(out["ok"], bad)
            self.assertEqual(lkg.pending_swaps(), [])

    def test_missing_source_refused(self):
        with tempfile.TemporaryDirectory() as td:
            app = _app(Path(td))
            lkg = LkgStore(app, Path(td) / "lkg")
            out = lkg.stage_swap("backend/x.bin",
                                 Path(td) / "nonexistent")
            self.assertFalse(out["ok"])


if __name__ == "__main__":
    unittest.main()
