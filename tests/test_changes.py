"""Universal change journal tests.

Covers the durable ledger itself plus its wiring into the chat control
plane: registry-mediated settings writes are journaled, "undo that"
falls back to the journal when the turn had no session action, and
"what did you change" answers from the ledger.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from localcodeagent.changes import ChangeJournal
from localcodeagent.self_knowledge import SelfKnowledgeService
from tests.test_self_knowledge import make_service


def make_journal(tmp: Path, **kw) -> ChangeJournal:
    return ChangeJournal(tmp / "changes.jsonl", **kw)


class TestJournal(unittest.TestCase):
    def test_record_and_recent(self):
        with tempfile.TemporaryDirectory() as d:
            j = make_journal(Path(d))
            row = j.record("setting", "Voice", before=True, after=False,
                           undo={"kind": "setting", "key": "voice_enabled",
                                 "value": True})
            self.assertEqual(row["action_type"], "setting")
            self.assertFalse(row["undone"])
            self.assertEqual(j.recent(1)[0]["id"], row["id"])
            self.assertIs(j.get(row["id"]), row)

    def test_persists_across_reload(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "changes.jsonl"
            j = ChangeJournal(path)
            j.record("setting", "Voice", before=True, after=False)
            j2 = ChangeJournal(path)
            self.assertEqual(len(j2.recent()), 1)
            self.assertEqual(j2.recent()[0]["subject"], "Voice")

    def test_limit_rewrites_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "changes.jsonl"
            j = ChangeJournal(path, limit=50)
            for i in range(60):
                j.record("test", f"change {i}")
            self.assertEqual(len(j.recent(100)), 50)
            self.assertEqual(j.recent(1)[0]["subject"], "change 59")
            lines = [ln for ln in path.read_text().splitlines() if ln]
            self.assertEqual(len(lines), 50)

    def test_file_mutation_upserts_per_task(self):
        with tempfile.TemporaryDirectory() as d:
            j = make_journal(Path(d))
            j.record_file_mutation("t1", "a.py")
            j.record_file_mutation("t1", "b.py")
            j.record_file_mutation("t2", "c.py")
            recs = j.recent(10)
            self.assertEqual(len(recs), 2)
            t1 = next(r for r in recs if r["task_id"] == "t1")
            self.assertEqual(sorted(t1["files"]), ["a.py", "b.py"])
            self.assertEqual(t1["undo"]["kind"], "checkpoint_restore")

    def test_undo_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            j = make_journal(Path(d))
            out = j.undo()
            self.assertFalse(out["ok"])
            self.assertIn("nothing", out["message"].lower())

    def test_undo_no_handler(self):
        with tempfile.TemporaryDirectory() as d:
            j = make_journal(Path(d))
            j.record("mystery", "thing", undo={"kind": "nope"})
            out = j.undo()
            self.assertFalse(out["ok"])
            self.assertIn("nope", out["message"])

    def test_undo_dispatches_and_marks(self):
        with tempfile.TemporaryDirectory() as d:
            calls = []
            j = make_journal(Path(d), undo_handlers={
                "fake": lambda rec: (calls.append(rec["id"]),
                                     {"ok": True, "verified": True})[1]})
            row = j.record("fake", "fake thing",
                           undo={"kind": "fake"})
            out = j.undo()
            self.assertTrue(out["ok"])
            self.assertTrue(out["verified"])
            self.assertEqual(calls, [row["id"]])
            self.assertTrue(j.get(row["id"])["undone"])
            # Second undo refuses — already undone.
            out2 = j.undo()
            self.assertFalse(out2["ok"])

    def test_undo_specific_record(self):
        with tempfile.TemporaryDirectory() as d:
            j = make_journal(Path(d), undo_handlers={
                "fake": lambda rec: {"ok": True}})
            old = j.record("fake", "old", undo={"kind": "fake"})
            j.record("fake", "new", undo={"kind": "fake"})
            out = j.undo(old["id"])
            self.assertTrue(out["ok"])
            self.assertTrue(j.get(old["id"])["undone"])

    def test_irreversible_refused(self):
        with tempfile.TemporaryDirectory() as d:
            j = make_journal(Path(d))
            j.record("push", "git push", reversible=False,
                     irreversible_reason="remote side effects")
            out = j.undo()
            self.assertFalse(out["ok"])
            self.assertIn("remote side effects", out["message"])


class TestJournalServiceWiring(unittest.TestCase):
    def _service_with_journal(self, tmp: Path):
        svc, store, calls = make_service()
        journal = ChangeJournal(tmp / "changes.jsonl")
        svc._env["change_journal"] = lambda: journal
        return svc, store, journal

    def test_setting_set_is_journaled(self):
        with tempfile.TemporaryDirectory() as d:
            svc, store, journal = self._service_with_journal(Path(d))
            seen = []
            svc.settings._env["on_change"] = (
                lambda spec=None, previous=None, value=None: seen.append(
                    (spec.key, previous, value)))
            res = svc.respond("turn voice off")
            self.assertIsNotNone(res)
            self.assertFalse(store["voice_enabled"])
            self.assertEqual(seen, [("voice_enabled", True, False)])

    def test_undo_falls_back_to_journal(self):
        with tempfile.TemporaryDirectory() as d:
            svc, store, journal = self._service_with_journal(Path(d))
            restored = []
            journal.undo_handlers["setting"] = lambda rec: (
                restored.append(rec["undo"]["key"]),
                {"ok": True, "verified": True,
                 "message": "Voice restored"})[1]
            journal.record("setting", "Voice", before=True, after=False,
                           undo={"kind": "setting",
                                 "key": "voice_enabled", "value": True})
            # Fresh service session — no last_executed — the journal
            # still answers "undo that".
            res = svc.respond("undo that")
            self.assertIsNotNone(res)
            self.assertEqual(res.kind, "execute")
            self.assertEqual(restored, ["voice_enabled"])

    def test_undo_journal_empty(self):
        with tempfile.TemporaryDirectory() as d:
            svc, store, journal = self._service_with_journal(Path(d))
            res = svc.respond("undo that")
            self.assertIsNotNone(res)
            self.assertIn("nothing to undo", res.text.lower())

    def test_what_did_you_change_lists_journal(self):
        with tempfile.TemporaryDirectory() as d:
            svc, store, journal = self._service_with_journal(Path(d))
            journal.record("setting", "Voice", before=True, after=False)
            journal.record("git_commit", "commit abc12345",
                           after="abc12345")
            res = svc.respond("what did you change")
            self.assertIsNotNone(res)
            self.assertIn("Voice", res.text)
            self.assertIn("commit", res.text)

    def test_what_did_you_change_empty(self):
        with tempfile.TemporaryDirectory() as d:
            svc, store, journal = self._service_with_journal(Path(d))
            res = svc.respond("what did you change")
            self.assertIsNotNone(res)
            self.assertIn("haven't recorded", res.text)


if __name__ == "__main__":
    unittest.main()
