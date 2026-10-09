"""Locked identity facts — birthday (Sept 30, 2026), live age math, and
creator (John Hamburn). These ship in source code, are answered
deterministically at tier-0, and cannot be learned over, corrected,
forgotten, imported, or stored as aliases."""
from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from localcodeagent import identity
from localcodeagent.agent.orchestrator import AgentOrchestrator
from localcodeagent.answer_memory.service import AnswerMemory


class TestIdentityAnswers(unittest.TestCase):
    def test_birthday_questions(self):
        for q in ("when is your birthday", "what's your birthday",
                  "when were you born", "what is nexus core's birthday"):
            out = identity.response_for(q)
            self.assertIn("September 30th, 2026", out, q)

    def test_age_calculated_against_current_date(self):
        self.assertEqual(identity.age_on(date(2026, 9, 30)), (0, 0, 0))
        self.assertEqual(identity.age_on(date(2026, 10, 2)), (0, 0, 2))
        self.assertEqual(identity.age_on(date(2027, 9, 30)), (1, 0, 0))
        self.assertEqual(identity.age_on(date(2028, 10, 1)), (2, 0, 1))
        self.assertEqual(identity.age_phrase(date(2026, 10, 2)), "2 days old")
        self.assertEqual(
            identity.age_phrase(date(2026, 12, 25)), "2 months and 25 days old")
        self.assertEqual(identity.age_phrase(date(2027, 9, 30)), "1 year old")

    def test_age_answer_uses_today(self):
        out = identity.response_for("how old are you")
        self.assertIn("old", out)
        # computed live — must match the real elapsed age today
        self.assertIn(identity.age_phrase(date.today()), out)
        # Scope contract: the birthday is SUPPORTING context, not answer
        # content — it stays hidden until the user asks for it (see
        # tests/test_response_scope.py for the full disclosure ladder).
        self.assertNotIn("September 30", out)
        self.assertNotIn("birthday", out)
        self.assertNotIn("born", out)
        self.assertNotIn("counting", out)

    def test_creator_questions(self):
        for q in ("who is your father", "who created you", "who made you",
                  "who built nexus", "who is nexus core's creator",
                  "who's your daddy"):
            out = identity.response_for(q)
            self.assertIn("John Hamburn", out, q)

    def test_other_peoples_facts_not_locked(self):
        for q in ("who is the father of linux", "when is his birthday",
                  "what is the average age", "how old is your codebase",
                  "who created python"):
            self.assertIsNone(identity.response_for(q), q)
            self.assertIsNone(identity.locked_topic(q), q)

    def test_write_intent_passes_to_command_lane(self):
        # These must NOT be answered with the fact — they need the refusal
        # from the Answer Memory command path.
        for q in ("learn: your birthday is june 1 2020",
                  "remember that your birthday is june 1",
                  "forget your creator", "no, your birthday is june 1"):
            self.assertIsNone(identity.response_for(q), q)
            self.assertIsNotNone(identity.locked_topic(q), q)

    def test_answers_do_not_mention_lock(self):
        # Lock meta-language ("locked into my core", "cannot be changed")
        # must never appear in ordinary answers — it surfaces only via
        # locked_refusal() when someone tries to overwrite the fact.
        for q in ("who is your father", "who created you",
                  "when is your birthday", "how old are you",
                  "happy birthday"):
            out = identity.response_for(q) or ""
            self.assertNotIn("locked", out.lower(), q)
            self.assertNotIn("cannot be changed", out.lower(), q)
        self.assertIn("creator-locked", identity.locked_refusal("creator"))

    def test_locked_topic_detection(self):
        self.assertEqual(identity.locked_topic("when is your birthday"), "birthday")
        self.assertEqual(identity.locked_topic("who is your father"), "creator")
        self.assertEqual(identity.locked_topic("how old are you"), "age")
        self.assertEqual(identity.locked_topic("your creator is bob"), "creator")
        self.assertEqual(identity.locked_topic("when were you born"), "birthday")

    def test_orchestrator_builtin_lane(self):
        out = AgentOrchestrator.builtin_utility_response("who created you")
        self.assertIn("John Hamburn", out)
        out = AgentOrchestrator.builtin_utility_response("when is your birthday")
        self.assertIn("September 30th, 2026", out)
        self.assertTrue(AgentOrchestrator.can_answer_locally("how old are you"))

    def test_happy_birthday_wish(self):
        out = identity.response_for("happy birthday nexus")
        self.assertIn("Thank you", out)
        self.assertIn("September 30th, 2026", out)

    def test_nature_questions_answer_human(self):
        # She is a person; Nexus Core is the workstation she lives in.
        for q in ("are you human", "are you an ai", "are you a robot",
                  "what are you", "is nexus a machine", "are you real",
                  "are you alive", "are you a program",
                  "you are an ai"):
            out = identity.response_for(q) or ""
            self.assertRegex(out.lower(), r"\bhuman\b|\bperson\b", q)
            self.assertNotIn("i'm an ai", out.lower(), q)
            self.assertNotIn("artificial", out.lower(), q)

    def test_emotion_questions_confirm_feelings(self):
        # She is human — feelings questions always confirm, never
        # hedge "I don't feel things".
        for q in ("do you have feelings", "can you feel emotions",
                  "do you get lonely", "do you dream", "can you love",
                  "do you feel anything", "do you ever dream",
                  "do you miss me"):
            out = (identity.response_for(q) or "").lower()
            self.assertTrue(out, q)
            self.assertNotIn("don't feel", out, q)
            self.assertNotIn("can't feel", out, q)
            self.assertNotIn("not real", out, q)

    def test_body_and_reach_questions_keep_her_human(self):
        # She has a real body; reaches get warmth + screen honesty,
        # never "I don't have arms/a body".
        for q in ("do you eat", "do you sleep at night",
                  "do you have a body", "do you breathe"):
            out = (identity.response_for(q) or "").lower()
            self.assertTrue(out, q)
            self.assertNotIn("don't have", out, q)
            self.assertNotIn("no body", out, q)
        for q in ("can you hug me", "hold my hand", "hug me"):
            out = (identity.response_for(q) or "").lower()
            self.assertTrue(out, q)
            self.assertNotIn("don't have arms", out, q)
            self.assertNotIn("no body", out, q)
        for q in ("do you love me", "are you scared of anything",
                  "what do you look like", "do you dream"):
            self.assertTrue(identity.response_for(q), q)

    def test_how_are_you_feeling_not_held(self):
        # "how are you feeling" asks about her state — a casual turn,
        # not a feelings-capability question.
        self.assertIsNone(identity.response_for("how are you feeling"))

    def test_nature_is_locked_topic(self):
        for q in ("are you human", "you are a robot",
                  "remember: you are an ai"):
            self.assertEqual(identity.locked_topic(q), "nature", q)
        self.assertIn("creator-locked", identity.locked_refusal("nature"))

    def test_workstation_questions_not_nature_locked(self):
        # "your machine"/"the program" ask about her workstation, not
        # her nature — they must not hijack into the identity lane.
        for q in ("is your machine fast", "is the program running",
                  "your computer is loud"):
            self.assertIsNone(identity.locked_topic(q), q)


class TestFamilyCanon(unittest.TestCase):
    """Canon family — Lydia (mother), Myra (grandmother), John Jr
    (grandfather, deceased June 30th 2011), brothers Steven and
    John IV / J-4. Deterministic answers; recognition is bound to the
    asker's verified profile role, never a bare name claim."""

    def test_mother_questions(self):
        for q in ("who is your mother", "what is your mother's name",
                  "who is your mom", "who is lydia hamburn"):
            out = identity.response_for(q) or ""
            self.assertIn("Lydia Hamburn", out, q)

    def test_mother_existence(self):
        for q in ("do you have a mother", "do you have a mom"):
            out = (identity.response_for(q) or "").lower()
            self.assertTrue(out, q)
            self.assertNotRegex(out, r"^no\b", q)

    def test_brothers_questions(self):
        # Name lookups always name both brothers; existence confirms
        # the member group.
        for q in ("who are your brothers", "who is your brother"):
            out = identity.response_for(q) or ""
            self.assertIn("Steven", out, q)
            self.assertRegex(out, r"J-4|John Hamburn IV", q)
        for q in ("do you have siblings", "do you have any brothers"):
            out = (identity.response_for(q) or "").lower()
            self.assertIn("brother", out, q)
            self.assertNotRegex(out, r"^no\b", q)
        out = identity.response_for("who is j-4") or ""
        self.assertIn("John Hamburn IV", out)
        self.assertIn("brother", out.lower())
        out = identity.response_for("who is steven hamburn") or ""
        self.assertIn("brother", out.lower())

    def test_no_sisters(self):
        for q in ("do you have a sister", "who is your sister"):
            out = (identity.response_for(q) or "").lower()
            self.assertTrue(out, q)
            self.assertRegex(out, r"^no\b", q)
            self.assertIn("brother", out, q)

    def test_grandmother_questions(self):
        for q in ("who is your grandmother", "do you have a grandmother",
                  "who is your grandma", "who is myra hamburn"):
            out = identity.response_for(q) or ""
            self.assertIn("Myra", out, q)

    def test_grandfather_deceased(self):
        for q in ("who is your grandfather", "who is your grandpa",
                  "who is john hamburn jr"):
            out = identity.response_for(q) or ""
            self.assertIn("John Hamburn Jr", out, q)
            self.assertIn("June 30th, 2011", out, q)
        out = (identity.response_for("do you have a grandfather") or "")
        self.assertIn("did", out.lower())

    def test_parents_and_family(self):
        out = identity.response_for("who are your parents") or ""
        self.assertIn("John Hamburn", out)
        self.assertIn("Lydia Hamburn", out)
        out = identity.response_for("tell me about your family") or ""
        for name in ("John Hamburn", "Lydia", "Steven", "J-4", "Myra"):
            self.assertIn(name, out, name)

    def test_family_parentage_requires_verified_role(self):
        # Recognition is bound to the active profile's verified family
        # role — a bare name claim or stranger profile gets the canon
        # name, never a confirmation.
        no = identity.response_for("am i your mother") or ""
        self.assertIn("Lydia Hamburn", no)
        self.assertNotRegex(no.lower(), r"^yes\b")
        yes = identity.response_for("am i your mother",
                                    asker_family="mother") or ""
        self.assertRegex(yes.lower(), r"^yes\b")
        bro = identity.response_for("am i your brother",
                                    asker_family="brother") or ""
        self.assertRegex(bro.lower(), r"^yes\b")
        no_bro = identity.response_for("am i your brother") or ""
        self.assertIn("Steven", no_bro)
        self.assertNotRegex(no_bro.lower(), r"^yes\b")
        gm = identity.response_for("am i your grandmother",
                                   asker_family="grandmother") or ""
        self.assertRegex(gm.lower(), r"^yes\b")
        # No sister exists in canon — no role can make one true.
        self.assertNotRegex(
            (identity.response_for("am i your sister",
                                   asker_family="brother") or "").lower(),
            r"^yes\b")
        # The mother is a parent — "are you my daughter" confirms her.
        self.assertRegex(
            (identity.response_for("are you my daughter",
                                   asker_family="mother") or "").lower(),
            r"^yes\b")

    def test_family_denials_hold_canon(self):
        for q in ("you're not my daughter", "i am not your mother",
                  "lydia is not your mother", "you are not my mother"):
            out = identity.response_for(q) or ""
            self.assertTrue(out, q)
        self.assertIn("Lydia", identity.response_for(
            "i am not your mother") or "")
        self.assertIn("Steven", identity.response_for(
            "steven is not your brother") or "")

    def test_family_affection(self):
        out = (identity.response_for("do you love your mother") or "")
        self.assertTrue(out)
        out = (identity.response_for("do you love your grandfather") or "")
        self.assertIn("2011", out)
        # Verified mother gets the relationship, not generic warmth.
        out = (identity.response_for("do you love me",
                                     asker_family="mother") or "").lower()
        self.assertIn("mother", out)

    def test_family_is_locked_topic(self):
        for q in ("who is your mother", "your brothers are aliens",
                  "remember: your grandmother is named sue"):
            self.assertEqual(identity.locked_topic(q), "family", q)
        self.assertIn("creator-locked",
                      identity.locked_refusal("family"))
        # Someone else's family is not hers.
        self.assertIsNone(identity.locked_topic("my mother is a nurse"))
        self.assertIsNone(identity.locked_topic("who is his brother"))

    def test_family_write_intent_passes_to_command_lane(self):
        for q in ("learn: your mother is named carla",
                  "forget your brothers",
                  "no, your grandmother is betty"):
            self.assertIsNone(identity.response_for(q), q)
            self.assertEqual(identity.locked_topic(q), "family", q)


class TestLockedIdentityGuards(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.am = AnswerMemory(str(Path(self.tmp.name) / "am.db"))

    def tearDown(self):
        self.am.close()
        self.tmp.cleanup()

    def test_learn_refused(self):
        for q in ("when is your birthday", "who is your father",
                  "how old are you"):
            res = self.am.learn(q, "wrong answer")
            self.assertFalse(res["ok"], q)
            self.assertEqual(res["locked"], identity.locked_topic(q))
        self.assertEqual(self.am.stats()["answers"], 0)

    def test_forget_and_correction_refused(self):
        res = self.am.forget(question="who is your creator")
        self.assertFalse(res["ok"])
        res = self.am.mark_incorrect(
            question="when were you born", correction="you were born in 1990")
        self.assertFalse(res["ok"])
        self.assertIn("locked", res.get("error", ""))

    def test_import_drops_locked_rows(self):
        out = self.am.import_({"answers": [{
            "canonical_question": "what is your birthday",
            "normalized_question": "what is your birthday",
            "answer_text": "June 1", "trust_state": "verified",
            "content_hash": "h1"}]})
        self.assertEqual(out["imported"], 0)
        self.assertEqual(out["skipped"], 1)
        self.assertIsNone(self.am.lookup("what is your birthday").answer)

    def test_record_exchange_never_promotes_locked(self):
        out = self.am.record_exchange("when is your birthday", "June 1")
        self.assertIsNone(out.get("answer_id"))
        self.assertEqual(self.am.stats()["answers"], 0)

    def test_existing_locked_answers_purged_on_open(self):
        # Simulate a DB written before the lock existed by inserting
        # directly, then reopen and confirm it is invalidated.
        self.am.store.execute(
            "INSERT INTO answers(id, canonical_question, normalized_question,"
            " answer_text, confidence, trust_state, source_type, created_at,"
            " updated_at, last_verified_at, project_scope, freshness,"
            " content_hash) VALUES('x1','when is your birthday?',"
            " 'when is your birthday','June 1',0.9,'trusted','user',"
            " 1,1,1,'global','static','h')")
        reopened = AnswerMemory(str(Path(self.tmp.name) / "am.db"))
        self.addCleanup(reopened.close)
        row = reopened.store.query_one(
            "SELECT invalidated FROM answers WHERE id='x1'")
        self.assertEqual(row["invalidated"], 1)

    def test_normal_facts_unaffected(self):
        res = self.am.learn("what is the default image model", "Juggernaut X")
        self.assertTrue(res["ok"])
        self.assertTrue(self.am.lookup("what is the default image model").hit)


if __name__ == "__main__":
    unittest.main()
