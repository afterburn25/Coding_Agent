"""StateDB / DocStore — transactional state layer regressions.

Foundation 1 of the resilience milestone: critical document stores
ride a WAL-mode SQLite kv domain with quarantine-on-corrupt,
verified one-shot migration from legacy files, atomic multi-write
transactions, and a bounded boot integrity probe.
"""
import json
import sqlite3
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from localcodeagent.state_db import DocStore, StateDB


class StateDBTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = StateDB(self.root / "state.db")
        self.addCleanup(self.db.close)

    def test_wal_and_pragmas(self):
        with self.db._conn() as conn:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertEqual(mode, "wal")
            fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
            self.assertEqual(fk, 1)
            busy = conn.execute("PRAGMA busy_timeout").fetchone()[0]
            self.assertGreater(busy, 0)

    def test_schema_version_recorded(self):
        self.assertEqual(self.db.schema_version, 1)
        # Reopening applies no migrations again (idempotent).
        self.db.close()
        self.db = StateDB(self.root / "state.db")
        self.addCleanup(self.db.close)
        self.assertEqual(self.db.schema_version, 1)

    def test_txn_atomic_all_or_nothing(self):
        with self.db.txn() as conn:
            conn.execute(
                "INSERT INTO kv(domain,key,value,updated_at)"
                " VALUES('a','one','1',0)")
            conn.execute(
                "INSERT INTO kv(domain,key,value,updated_at)"
                " VALUES('a','two','2',0)")
        self.assertEqual(self.db.kv_get("a", "one"), "1")

        # Kill mid-transaction: a crash between statements leaves no
        # partial state behind.
        with self.assertRaises(RuntimeError):
            with self.db.txn() as conn:
                conn.execute(
                    "INSERT INTO kv(domain,key,value,updated_at)"
                    " VALUES('a','three','3',0)")
                raise RuntimeError("crash mid-write")
        self.assertIsNone(self.db.kv_get("a", "three"))

    def test_uncommitted_write_never_visible(self):
        """Crash-during-write: a connection that dies before COMMIT
        leaves the database exactly as it was — no torn rows."""
        db2 = sqlite3.connect(str(self.root / "state.db"),
                              isolation_level=None)
        db2.execute("BEGIN IMMEDIATE")
        db2.execute(
            "INSERT INTO kv(domain,key,value,updated_at)"
            " VALUES('x','pending','{}',0)")
        db2.close()  # connection death — uncommitted txn rolls back
        self.assertIsNone(self.db.kv_get("x", "pending"))

    def test_integrity_ok_and_bad_row_probe(self):
        self.db.kv_put("autonomy", "missions.json", '{"a": 1}')
        report = self.db.integrity()
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["quick_check"], "ok")
        self.assertEqual(report["kv_rows"], 1)
        # Inject a non-JSON row — the parse probe flags it.
        self.db.execute(
            "INSERT INTO kv(domain,key,value,updated_at)"
            " VALUES('broken','x','not-json{{{',0)")
        report = self.db.integrity()
        self.assertFalse(report["ok"])
        self.assertIn("broken/x", report["bad_rows"])

    def test_events_and_operations_tables(self):
        self.db.record_event("autonomy", "mission_start", {"m": "m-1"})
        rows = self.db.execute(
            "SELECT domain, action, detail FROM events")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "autonomy")
        self.db.execute(
            "INSERT INTO operations(operation_id,intent_hash,target,"
            "state,started_at) VALUES('op-1','h','github','planned',0)")
        row = self.db.execute(
            "SELECT state FROM operations WHERE operation_id='op-1'"
        )[0]
        self.assertEqual(row[0], "planned")


class DocStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = StateDB(self.root / "state.db")
        self.addCleanup(self.db.close)
        self.path = self.root / "missions.json"

    def test_file_backed_roundtrip(self):
        doc = DocStore(None, self.path)
        doc.save_json({"version": 1, "rows": [1, 2]})
        self.assertEqual(doc.load_json({}), {"version": 1, "rows": [1, 2]})

    def test_legacy_file_migrates_to_db(self):
        self.path.write_text(json.dumps({"version": 1, "rows": [1]}))
        doc = DocStore(self.db, self.path, domain="autonomy")
        obj = doc.load_json({})
        self.assertEqual(obj, {"version": 1, "rows": [1]})
        self.assertTrue(doc.migrated)
        # Original frozen, never deleted.
        self.assertTrue(
            (self.root / "missions.json.migrated").is_file())
        # DB row now holds truth.
        self.assertEqual(json.loads(
            self.db.kv_get("autonomy", "missions.json")), obj)

    def test_db_row_wins_over_stale_file(self):
        """After migration the file is only a shadow — a hand-edited
        stale file must not silently override the DB row."""
        doc = DocStore(self.db, self.path, domain="autonomy")
        doc.save_json({"version": 1, "rows": [1]})
        self.path.write_text(json.dumps({"version": 1, "rows": [999]}))
        doc2 = DocStore(self.db, self.path, domain="autonomy")
        self.assertEqual(doc2.load_json({}), {"version": 1, "rows": [1]})

    def test_save_writes_db_and_shadow(self):
        doc = DocStore(self.db, self.path, domain="autonomy")
        doc.save_json({"version": 1, "rows": [3]})
        self.assertEqual(
            json.loads(self.path.read_text())["rows"], [3])
        self.assertEqual(
            json.loads(self.db.kv_get("autonomy", "missions.json")),
            {"version": 1, "rows": [3]})

    def test_corrupt_file_quarantined_not_lost(self):
        self.path.write_text('{"missions": [broken')
        doc = DocStore(self.db, self.path, domain="autonomy")
        obj = doc.load_json({"version": 1, "rows": []})
        self.assertEqual(obj, {"version": 1, "rows": []})
        self.assertTrue(doc.degraded)
        # Quarantined in DB and copied next to the source.
        q = self.db.execute(
            "SELECT key, reason FROM quarantine")
        self.assertEqual(len(q), 1)
        self.assertTrue(list(self.root.glob("missions.json.corrupt-*")))
        self.assertTrue(self.db.degraded)

    def test_corrupt_db_row_falls_back_to_file(self):
        # Good file + poisoned DB row → quarantine the row, use file.
        self.path.write_text(json.dumps({"version": 1, "rows": [7]}))
        self.db.kv_put("autonomy", "missions.json", "garbage{{{")
        doc = DocStore(self.db, self.path, domain="autonomy")
        obj = doc.load_json({})
        self.assertEqual(obj, {"version": 1, "rows": [7]})
        self.assertTrue(doc.degraded)
        # Repaired row re-imported from the good file.
        self.assertEqual(json.loads(
            self.db.kv_get("autonomy", "missions.json")),
            {"version": 1, "rows": [7]})

    def test_migrated_backup_recovers_lost_db(self):
        self.path.write_text(json.dumps({"version": 1, "rows": [5]}))
        doc = DocStore(self.db, self.path, domain="autonomy")
        doc.load_json({})
        # Disaster: live DB row + live file gone, .migrated survives.
        self.db.execute(
            "DELETE FROM kv WHERE domain='autonomy'")
        self.path.unlink()
        doc2 = DocStore(self.db, self.path, domain="autonomy")
        self.assertEqual(doc2.load_json({}), {"version": 1, "rows": [5]})

    def test_migration_idempotent_across_reopen(self):
        self.path.write_text(json.dumps({"version": 1, "rows": [1]}))
        doc = DocStore(self.db, self.path, domain="autonomy")
        doc.load_json({})
        # Mutate only through the store.
        doc.save_json({"version": 1, "rows": [1, 2]})
        # Reopen — DB row (authoritative) must win over the shadow file.
        doc2 = DocStore(self.db, self.path, domain="autonomy")
        self.assertEqual(doc2.load_json({}),
                         {"version": 1, "rows": [1, 2]})

    def test_default_when_nothing_exists(self):
        doc = DocStore(self.db, self.path, domain="autonomy")
        self.assertEqual(doc.load_json({"v": 9}), {"v": 9})
        self.assertFalse(doc.degraded)


class StoreIntegrationTests(unittest.TestCase):
    """The migrated Tier-1 stores behave identically DB-backed."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = StateDB(self.root / "data" / "state.db")
        self.addCleanup(self.db.close)

    def test_autonomy_store_survives_reopen(self):
        from localcodeagent.autonomy.state import AutonomyStore
        store = AutonomyStore(self.root / "data" / "autonomy",
                              db=self.db)
        store.missions.data["missions"] = [{"id": "m-1",
                                            "status": "running"}]
        store.missions.save()
        store.approvals.data["approvals"] = [{"id": "a-1",
                                              "state": "pending"}]
        store.approvals.save()
        # New process instance over the same DB.
        self.db.close()
        db2 = StateDB(self.root / "data" / "state.db")
        self.addCleanup(db2.close)
        store2 = AutonomyStore(self.root / "data" / "autonomy", db=db2)
        self.assertEqual(store2.missions.data["missions"][0]["id"],
                         "m-1")
        self.assertEqual(store2.approvals.data["approvals"][0]["state"],
                         "pending")
        self.db = db2

    def test_action_ledger_survives_reopen(self):
        from localcodeagent.action_ledger import ActionLedger
        ledger = ActionLedger(self.root / "data" / "action_ledger.json",
                              db=self.db)
        entry = ledger.begin(kind="filesystem", action="create folder",
                             capability="filesystem.write")
        ledger.finish(entry["id"], status="verified",
                      permission="allow", verification="dir exists")
        self.db.close()
        db2 = StateDB(self.root / "data" / "state.db")
        self.addCleanup(db2.close)
        ledger2 = ActionLedger(self.root / "data" / "action_ledger.json",
                               db=db2)
        self.assertEqual(ledger2.get(entry["id"])["status"], "verified")
        self.db = db2

    def test_identity_survives_reopen(self):
        from localcodeagent.identity_mgr import IdentityManager
        mgr = IdentityManager(self.root / "data", state_db=self.db)
        mgr.record_account("github", handle="nexus-agent",
                           state="active")
        self.db.close()
        db2 = StateDB(self.root / "data" / "state.db")
        self.addCleanup(db2.close)
        mgr2 = IdentityManager(self.root / "data", state_db=db2)
        accounts = mgr2.status()["accounts"]
        self.assertTrue(any(a.get("service") == "github"
                            for a in accounts))
        self.db = db2

    def test_safemode_counter_survives_and_quarantines(self):
        from localcodeagent.safemode import SafeModeStore
        sm = SafeModeStore(self.root / "data" / "safe_mode.json",
                           db=self.db)
        sm.record_boot(previous_clean=False)
        self.assertEqual(sm.consecutive_failures, 1)
        # Corrupt the live file AND poison the row — fallback boots
        # degraded with the counter preserved via quarantine, not
        # silently reset.
        self.db.execute(
            "UPDATE kv SET value='broken{{' WHERE domain='safemode'")
        (self.root / "data" / "safe_mode.json").write_text("junk{{")
        sm2 = SafeModeStore(self.root / "data" / "safe_mode.json",
                            db=self.db)
        self.assertTrue(sm2._doc.degraded)

    def test_learning_stores_share_db(self):
        from localcodeagent.learning import LearningGovernor
        gov = LearningGovernor(self.root / "data" / "learning",
                               state_db=self.db)
        gov.lessons.add({"id": "L-1", "kind": "correction",
                         "text": "prefer sqlite"})
        gov.procedures._save()
        gov.skill_promotion._save()
        domains = self.db.kv_keys("learning")
        self.assertIn("lessons.json", domains)
        self.assertIn("procedures.json", domains)
        self.assertIn("skill_candidates.json", domains)

    def test_cross_store_atomic_commit(self):
        """The phase-A invariant: approval + ledger + event commit in
        one transaction — a crash mid-write can't split them."""
        with self.db.txn() as conn:
            conn.execute(
                "INSERT INTO kv(domain,key,value,updated_at)"
                " VALUES('autonomy','approvals.json','{}',0)")
            conn.execute(
                "INSERT INTO events(ts,domain,action,detail)"
                " VALUES(0,'autonomy','approved','')")
        # And a crash rolls back the whole unit.
        with self.assertRaises(RuntimeError):
            with self.db.txn() as conn:
                conn.execute(
                    "INSERT INTO kv(domain,key,value,updated_at)"
                    " VALUES('identity','x','{}',0)")
                conn.execute(
                    "INSERT INTO events(ts,domain,action,detail)"
                    " VALUES(0,'identity','created','')")
                raise RuntimeError("crash")
        self.assertIsNone(self.db.kv_get("identity", "x"))
        self.assertEqual(self.db.execute(
            "SELECT COUNT(*) FROM events WHERE domain='identity'"
        )[0][0], 0)


if __name__ == "__main__":
    unittest.main()
