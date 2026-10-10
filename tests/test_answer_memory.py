"""Nexus Answer Memory — persistence, trust, retrieval, invalidation, UI ops."""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from localcodeagent.answer_memory import AnswerMemory
from localcodeagent.answer_memory import feedback, invalidation, ttl, validation
from localcodeagent.answer_memory.embeddings import embedder
from localcodeagent.answer_memory.normalization import normalize_question
from localcodeagent.answer_memory.service import AnswerMemory as _AM
from localcodeagent.agent.orchestrator import AgentOrchestrator
from localcodeagent.config import AgentConfig, ModelProfile
from localcodeagent.models.provider import ProviderResponse
from localcodeagent.models.router import ModelRouter
from localcodeagent.tools.base import ToolRegistry
from localcodeagent.workflow.checkpoint import CheckpointManager
from localcodeagent.workflow.conversation_manager import ConversationManager
from localcodeagent.workflow.memory import ProjectMemory
from localcodeagent.workflow.repository import RepositoryIndex
from localcodeagent.workflow.tasks import TaskStore


def _mem(td: str, **kw) -> AnswerMemory:
    return AnswerMemory(str(Path(td) / "am.db"), **kw)


class StoreAndSchemaTests(unittest.TestCase):
    def test_database_initializes(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            am = _mem(td)
            self.assertTrue(am.available)
            self.assertTrue(Path(td, "am.db").exists())
            row = am.store.query_one("PRAGMA user_version")
            self.assertEqual(int(row["user_version"]), 2)
            am.store.close()

    def test_wal_and_indexes(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            am = _mem(td)
            mode = am.store.query_one("PRAGMA journal_mode")
            self.assertEqual(str(mode["journal_mode"]).lower(), "wal")
            idx = {r["name"] for r in am.store.query("SELECT name FROM sqlite_master WHERE type='index'")}
            self.assertIn("idx_ans_norm", idx)
            self.assertIn("idx_exp_norm", idx)
            am.store.close()

    def test_existing_db_survives_upgrade(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            am = _mem(td)
            am.learn("What is the widget?", "A local AI workstation.")
            am.store.close()
            am2 = _mem(td)  # reopen — migrations must not destroy data
            self.assertTrue(am2.available)
            m = am2.lookup("What is the widget?")
            self.assertTrue(m.hit)
            am2.store.close()

    def test_partially_stamped_v2_db_self_heals(self):
        """A DB stamped user_version=2 but missing a migration-2 column
        (interrupted migration, manual stamp, partial restore) must heal
        on open — version-keyed migrations alone leave it broken forever."""
        import sqlite3
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            am = _mem(td)
            am.store.close()
            # Simulate the partial stamp: drop the column, keep v2.
            conn = sqlite3.connect(str(Path(td) / "am.db"))
            conn.execute(
                "ALTER TABLE experiences DROP COLUMN profile_id")
            conn.commit()
            cols = {r[1] for r in conn.execute(
                "PRAGMA table_info(experiences)")}
            self.assertNotIn("profile_id", cols)
            conn.close()
            am2 = _mem(td)  # reopen — must re-assert missing columns
            self.assertTrue(am2.available)
            out = am2.record_exchange(
                "What is a tensor?", "A multi-dimensional array.",
                model_id="m", model_role="utility", inference_time_ms=10)
            self.assertIsNotNone(out["experience_id"])
            am2.store.close()

    def test_experience_record_saved(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            am = _mem(td)
            out = am.record_exchange("What is a tensor?", "A multi-dimensional array.",
                                     model_id="qwen", model_role="utility",
                                     inference_time_ms=1200)
            self.assertIsNotNone(out["experience_id"])
            row = am.store.query_one("SELECT * FROM experiences WHERE id=?", (out["experience_id"],))
            self.assertEqual(row["model_id"], "qwen")
            self.assertEqual(row["raw_answer"], "A multi-dimensional array.")
            am.store.close()

    def test_restart_preserves_learned_answers(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            am = _mem(td)
            am.learn("What is the default image model?", "Juggernaut X v10.")
            am.store.close()
            am2 = _mem(td)
            m = am2.lookup("What is the default image model?")
            self.assertTrue(m.hit)
            self.assertIn("Juggernaut", m.answer["answer_text"])
            am2.store.close()


class LookupTests(unittest.TestCase):
    def test_exact_trusted_lookup_bypasses(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            am = _mem(td)
            am.learn("What is the default image model?", "Juggernaut X v10.")
            m = am.lookup("what is the default image model?!")
            self.assertEqual(m.kind, "exact")
            self.assertLess(m.latency_ms, 100)
            am.store.close()

    def test_semantic_equivalent_matches(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            am = _mem(td)
            am.learn("What image checkpoint does Nexus Core normally use?", "Juggernaut X v10.")
            m = am.lookup("Which model does Nexus normally generate images with?")
            self.assertEqual(m.kind, "semantic")
            self.assertTrue(m.hit)
            am.store.close()

    def test_weak_semantic_does_not_bypass(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            am = _mem(td)
            am.learn("What image checkpoint does Nexus Core normally use?", "Juggernaut X v10.")
            m = am.lookup("How do I bake bread at home?")
            self.assertFalse(m.hit)
            am.store.close()

    def test_entity_swap_does_not_match(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            am = _mem(td)
            am.learn("What is the capital of France?", "Paris.")
            m = am.lookup("What is the capital of Italy?")
            self.assertFalse(m.hit, "France answer must not leak to Italy question")
            am.store.close()

    def test_version_swap_does_not_match(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            am = _mem(td)
            am.learn("What does Qwen 14B do?", "It is the coding model.")
            m = am.lookup("What does Qwen 30B do?")
            self.assertFalse(m.hit)
            am.store.close()

    def test_untrusted_answer_cannot_bypass(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            am = _mem(td)
            am.record_exchange("What is photosynthesis?", "Plants convert light to energy.")
            m = am.lookup("What is photosynthesis?")
            self.assertFalse(m.hit, "observed answers must not bypass")
            self.assertEqual(m.kind, "no_match")
            am.store.close()

    def test_repetition_promotes_to_trusted(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            am = _mem(td)
            for _ in range(5):
                am.record_exchange("What is photosynthesis?", "Plants convert light to energy.")
            # Repetition still promotes the stored row internally...
            row = am.store.query_one(
                "SELECT trust_state, source_type FROM answers"
            )
            self.assertEqual(row["trust_state"], "trusted")
            # ...but model-sourced answers never serve — replaying model
            # output freezes whatever the model happened to say.
            m = am.lookup("What is photosynthesis?")
            self.assertFalse(m.hit)
            am.store.close()

    def test_invalidated_never_bypasses(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            am = _mem(td)
            r = am.learn("What is the default model?", "OldModel.")
            am.forget(answer_id=r["id"])
            m = am.lookup("What is the default model?")
            self.assertFalse(m.hit)
            am.store.close()

    def test_expired_answer_does_not_bypass(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            am = _mem(td)
            am.learn("What is the latest release?", "v1.2", freshness="time_sensitive")
            am.store.execute("UPDATE answers SET expires_at=?", (time.time() - 10,))
            m = am.lookup("What is the latest release?")
            self.assertFalse(m.hit)
            # Volatile questions reject at the cacheability gate before
            # dependency resolution runs — the row is unreachable rather
            # than marked stale, which is the same guarantee for callers.
            am.store.close()

    def test_live_questions_never_cached(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            am = _mem(td)
            am.learn("What is the weather right now?", "Sunny.")
            # learn() stores it, but cacheability gate must refuse lookup
            am.store.execute(
                "INSERT INTO answers(id, canonical_question, normalized_question, answer_text,"
                " confidence, trust_state, created_at, updated_at, freshness)"
                " VALUES('x1','stock price of nvidia','stock price of nvidia','$900',0.9,'trusted',1,1,'live')"
            )
            m = am.lookup("What is the stock price of NVIDIA today?")
            self.assertFalse(m.hit)
            am.store.close()

    def test_parameterized_sql_injection_safe(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            am = _mem(td)
            evil = "'); DROP TABLE answers; --"
            am.lookup(evil)
            am.learn(evil, "harmless")
            rows = am.store.query("SELECT COUNT(*) AS n FROM answers")
            self.assertEqual(rows[0]["n"], 1)
            m = am.lookup(evil)
            self.assertTrue(m.hit)
            am.store.close()


class LearningAndCorrectionTests(unittest.TestCase):
    def test_correction_invalidates_old_answer(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            am = _mem(td)
            am.learn("What is the default image model?", "Qwen Image 2.1")
            am.mark_incorrect(
                question="What is the default image model?",
                correction="No, the answer is Juggernaut X v10",
            )
            old = am.lookup("What is the default image model?")
            self.assertFalse(old.hit and "Qwen" in old.answer["answer_text"])
            # Replacement was promoted as trusted (explicit user correction)
            row = am.store.query_one(
                "SELECT * FROM answers WHERE invalidated=0 ORDER BY updated_at DESC")
            self.assertEqual(row["trust_state"], "trusted")
            self.assertEqual(row["source_type"], "correction")
            self.assertIn("Juggernaut", row["answer_text"])
            am.store.close()

    def test_mark_incorrect_then_relearn(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            am = _mem(td)
            am.learn("What is the default?", "Wrong answer")
            am.mark_incorrect(question="What is the default?")
            am.learn("What is the default?", "Correct answer")
            m = am.lookup("What is the default?")
            self.assertTrue(m.hit)
            self.assertEqual(m.answer["answer_text"], "Correct answer")
            am.store.close()

    def test_superseded_fact_invalidates_answers_carrying_it(self):
        # 'We switched to SQLite' must retire every learned answer still
        # asserting PostgreSQL — otherwise the memory fast path keeps
        # serving the dead value with a 'trusted answer' badge.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            am = _mem(td)
            am.learn("What database does the store use?",
                     "The store uses PostgreSQL.")
            am.learn("What is the capital of France?",
                     "Paris is the capital of France.")
            self.assertEqual(
                am.invalidate_superseded(["store uses postgresql"]), 1)
            rows = am.store.query(
                "SELECT * FROM answers ORDER BY id")
            dead = [r for r in rows if r["invalidated"]]
            live = [r for r in rows if not r["invalidated"]]
            self.assertEqual(len(dead), 1)
            self.assertIn("superseded fact", dead[0]["invalidation_reason"])
            self.assertIn("Paris", live[0]["answer_text"])
            # The dead answer can no longer bypass to a reply.
            m = am.lookup("What database does the store use?")
            self.assertFalse(
                m.hit and "postgres" in str(
                    (m.answer or {}).get("answer_text") or "").lower())
            # Empty/no-match calls are harmless.
            self.assertEqual(am.invalidate_superseded([]), 0)
            self.assertEqual(
                am.invalidate_superseded(["unrelated fact"]), 0)
            am.store.close()

    def test_positive_feedback_promotes(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            am = _mem(td)
            for _ in range(2):
                am.record_exchange("What is a tensor?", "A multidimensional array.",
                                   conversation_id="c1")
            exp = am.store.query_one("SELECT id FROM experiences ORDER BY ts DESC")
            from localcodeagent.answer_memory import learning
            learning.apply_positive_feedback(am.store, exp["id"])
            row = am.store.query_one("SELECT * FROM answers")
            self.assertIn(row["trust_state"], {"candidate", "trusted"})
            am.store.close()

    def test_thanks_does_not_verify(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            am = _mem(td)
            self.assertFalse(feedback.is_positive("thanks"))
            self.assertFalse(feedback.is_positive("thank you so much"))
            self.assertTrue(feedback.is_positive("that's correct"))
            am.store.close()

    def test_casual_wrong_word_not_a_correction(self):
        self.assertFalse(feedback.is_correction("what did I do wrong in this code?"))
        self.assertTrue(feedback.is_correction("that's wrong"))
        self.assertTrue(feedback.is_correction("no, the answer is 42"))
        # Uncontracted + 'X not Y' replacement forms must flag too.
        self.assertTrue(feedback.is_correction("no that is wrong"))
        self.assertTrue(feedback.is_correction("no, i meant the store uses Redis"))
        self.assertTrue(feedback.is_correction("i meant Redis"))
        self.assertTrue(feedback.is_correction("it was emacs not vim"))
        self.assertTrue(feedback.is_correction("the port was 5433 not 8080"))
        # Plain statements stay unflagged.
        self.assertFalse(feedback.is_correction("it was a good movie"))
        self.assertFalse(feedback.is_correction("the report is done"))
        self.assertFalse(feedback.is_correction("the plan is Postgres"))

    def test_noise_not_recorded(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            am = _mem(td)
            for noise in ("hi", "ok", "?", "", "a"):
                out = am.record_exchange(noise, "hello")
                self.assertIsNone(out["experience_id"], noise)
            am.store.close()

    def test_secrets_never_persisted(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            am = _mem(td)
            out = am.record_exchange(
                "What is this key sk-abcdefghijklmnop123456?",
                "That is an API key.")
            self.assertIsNone(out["experience_id"])
            self.assertTrue(out["suppressed"])
            res = am.learn("my password is hunter2", "remembered")
            self.assertFalse(res.get("ok"))
            self.assertEqual(
                am.store.query_one("SELECT COUNT(*) AS n FROM answers")["n"], 0)
            am.store.close()

    def test_learn_command_natural_language(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            am = _mem(td)
            r = am.handle_command("when I ask what is the default model, answer Juggernaut X")
            self.assertIn("Learned", r)
            m = am.lookup("what is the default model")
            self.assertTrue(m.hit)
            self.assertEqual(m.answer["answer_text"], "Juggernaut X")
            am.store.close()

    def test_error_reply_never_learned(self):
        # A transient failure report ("unknown tool", "couldn't reach")
        # is not knowledge — storing it replays the outage forever.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            am = _mem(td)
            out = am.record_exchange(
                "can you explain what github is for someone new?",
                "I couldn't reach GitHub — unknown tool "
                "'github_repo_activity'.")
            self.assertTrue(out["suppressed"])
            self.assertIsNone(out["experience_id"])
            self.assertEqual(
                am.store.query_one("SELECT COUNT(*) AS n FROM answers")["n"],
                0)
            am.store.close()

    def test_poisoned_answer_invalidated_at_lookup(self):
        # Rows poisoned before the record-gate existed self-heal: the
        # lookup invalidates the error answer in place instead of
        # replaying it.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            am = _mem(td)
            r = am.learn("What does the github integration do?",
                         "I couldn't reach GitHub — unknown tool "
                         "'github_repo_activity'.")
            self.assertTrue(r.get("ok"))
            m = am.lookup("What does the github integration do?")
            self.assertFalse(m.hit)
            row = am.store.query_one(
                "SELECT trust_state, invalidation_reason FROM answers")
            self.assertEqual(row["trust_state"], "invalidated")
            self.assertIn("error output", row["invalidation_reason"])
            am.store.close()

    def test_capability_probe_never_cached(self):
        # "can you browse websites?" — the truth lives in live
        # capability state; a learned answer goes stale on the next
        # toggle and must neither store nor replay.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            am = _mem(td)
            for probe in ("can you browse websites?",
                          "can you see my screen right now?",
                          "can you hear me?"):
                out = am.record_exchange(probe, "Yes — ready.")
                self.assertIsNone(out["answer_id"], probe)
            self.assertEqual(
                am.store.query_one("SELECT COUNT(*) AS n FROM answers")["n"],
                0)
            am.learn("What automation is available?",
                     "Yes — Browser automation is ready.")
            m = am.lookup("can you browse websites?")
            self.assertFalse(m.hit)
            am.store.close()

    def test_forget_command(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            am = _mem(td)
            am.learn("What is the default?", "X")
            r = am.handle_command("forget the answer for what is the default")
            self.assertIn("Forgot", r)
            self.assertFalse(am.lookup("what is the default").hit)
            am.store.close()

    def test_learn_this_answer_uses_last_exchange(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            am = _mem(td)
            r = am.handle_command(
                "learn this answer",
                last_exchange=("What is GRPO?", "A reinforcement learning method."),
            )
            self.assertIn("Learned", r)
            self.assertTrue(am.lookup("What is GRPO?").hit)
            am.store.close()

    def test_what_have_you_learned(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            am = _mem(td)
            am.learn("What is the default image model?", "Juggernaut X")
            r = am.handle_command("what have you learned about image generation")
            self.assertIn("Juggernaut", r or "")
            am.store.close()

    def test_duplicate_questions_alias_not_duplicate(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            am = _mem(td)
            am.learn("What is the default model?", "Juggernaut")
            am.record_exchange("What is the default model?", "Juggernaut")
            n = am.store.query_one("SELECT COUNT(*) AS n FROM answers")["n"]
            self.assertEqual(n, 1)
            am.store.close()


class FreshnessAndScopeTests(unittest.TestCase):
    def test_repository_dependent_stale_on_commit_change(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            heads = ["aaa111"]
            am = AnswerMemory(
                str(Path(td) / "am.db"),
                repo_head_fn=lambda: heads[0],
            )
            am.learn("Where is the image router?", "localcodeagent/image/router.py",
                     freshness="repository_dependent")
            self.assertTrue(am.lookup("Where is the image router?").hit)
            heads[0] = "bbb222"
            m = am.lookup("Where is the image router?")
            self.assertFalse(m.hit)
            row = am.store.query_one("SELECT trust_state FROM answers")
            self.assertEqual(row["trust_state"], "stale")
            am.store.close()

    def test_config_dependent_stale_on_fingerprint_change(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            fps = ["fp1"]
            am = AnswerMemory(
                str(Path(td) / "am.db"),
                config_fingerprint_fn=lambda: fps[0],
            )
            am.learn("Which model is default?", "Juggernaut",
                     freshness="configuration_dependent")
            self.assertTrue(am.lookup("Which model is default?").hit)
            fps[0] = "fp2"
            self.assertFalse(am.lookup("Which model is default?").hit)
            am.store.close()

    def test_project_scope_isolation(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            am = _mem(td)
            am.learn("Where is the router?", "src/router.py",
                     scope="project", project_id="project-a")
            # Same normalized question in another project must not hit.
            other = am.lookup("Where is the router?", project_id="project-b")
            self.assertFalse(other.hit)
            own = am.lookup("Where is the router?", project_id="project-a")
            self.assertTrue(own.hit)
            am.store.close()

    def test_global_scope_reusable_everywhere(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            am = _mem(td)
            am.learn("What does RAM stand for?", "Random Access Memory.")
            self.assertTrue(am.lookup("What does RAM stand for?", project_id="p1").hit)
            self.assertTrue(am.lookup("What does RAM stand for?", project_id="p2").hit)
            am.store.close()

    def test_refresh_restale_recovers(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            fps = ["a"]
            am = AnswerMemory(str(Path(td) / "am.db"), config_fingerprint_fn=lambda: fps[0])
            r = am.learn("Which model is default?", "X", freshness="configuration_dependent")
            fps[0] = "b"
            self.assertFalse(am.lookup("Which model is default?").hit)
            am.refresh(r["id"])
            self.assertTrue(am.lookup("Which model is default?").hit)
            am.store.close()


class ProfileIsolationTests(unittest.TestCase):
    """Profile-scoped learned answers must never leak to another profile —
    neither through trusted bypass nor through prompt-context hints."""

    def _swap_profile(self, am: AnswerMemory, holder: list[str], pid: str) -> None:
        holder[0] = pid

    def test_profile_answer_not_served_to_other_profile(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            current = ["alice"]
            am = _mem(td, profile_id_fn=lambda: current[0])
            am.learn("What editor do I prefer?", "Visual Studio")
            row = am.store.query_one(
                "SELECT profile_id FROM answers WHERE normalized_question=?",
                ("what editor do i prefer",),
            )
            self.assertEqual(row["profile_id"], "alice")
            self.assertTrue(am.lookup("What editor do I prefer?").hit)
            # Bob sees neither a direct hit nor a context hint.
            current[0] = "bob"
            match = am.lookup("What editor do I prefer?")
            self.assertIsNone(match.answer)
            self.assertEqual(match.context_answers, [])
            # Paraphrase must not leak either (semantic path).
            match = am.lookup("Which editor is my preference?")
            visible = [match.answer, *match.context_answers]
            self.assertFalse(
                any(r and "Visual Studio" in r.get("answer_text", "") for r in visible)
            )
            am.store.close()

    def test_learned_list_scoped_to_profile(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            current = [""]
            am = _mem(td, profile_id_fn=lambda: current[0])
            am.learn("What is the main branch?", "master")  # shared/legacy row
            current[0] = "alice"
            am.learn("What editor do I prefer?", "Visual Studio")
            am.learn("Where is my config?", "~/.nexus")  # alice-private too
            current[0] = "bob"
            am.learn("What editor do I prefer?", "VS Code")
            rows = am.list_answers()
            texts = {r["canonical_question"]: r["answer_text"] for r in rows}
            self.assertEqual(texts["What editor do I prefer?"], "VS Code")
            self.assertEqual(texts["What is the main branch?"], "master")
            self.assertNotIn("Where is my config?", texts)
            self.assertEqual(am.lookup("What editor do I prefer?").answer["answer_text"], "VS Code")
            # Alice still sees her own answer — profiles coexist, not collide.
            current[0] = "alice"
            self.assertEqual(
                am.lookup("What editor do I prefer?").answer["answer_text"],
                "Visual Studio",
            )
            am.store.close()

    def test_recorded_exchange_stamps_active_profile(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            current = ["alice"]
            am = _mem(td, profile_id_fn=lambda: current[0])
            am.record_exchange("What is your favorite drink?", "Coffee")
            exp = am.store.query_one("SELECT profile_id FROM experiences LIMIT 1")
            self.assertEqual(exp["profile_id"], "alice")
            am.store.close()

    def test_unprofiled_answers_stay_shared(self):
        # Rows recorded before profiles existed (profile_id='') remain visible
        # to every profile — technical knowledge is intentionally shared.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            am = _mem(td, profile_id_fn=lambda: "")
            am.learn("What port does the backend use?", "8081")
            am.store.execute(
                "UPDATE answers SET profile_id='' WHERE normalized_question=?",
                ("what port does the backend use",),
            )
            current = ["alice"]
            am._profile_id_fn = lambda: current[0]
            self.assertTrue(am.lookup("What port does the backend use?").hit)
            current[0] = "bob"
            self.assertTrue(am.lookup("What port does the backend use?").hit)
            am.store.close()

    def test_forget_cannot_reach_other_profile(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            current = ["alice"]
            am = _mem(td, profile_id_fn=lambda: current[0])
            am.learn("What editor do I prefer?", "Visual Studio")
            current[0] = "bob"
            result = am.forget(question="What editor do I prefer?")
            self.assertFalse(result.get("ok"))
            current[0] = "alice"
            self.assertTrue(am.lookup("What editor do I prefer?").hit)
            am.store.close()


class AdminTests(unittest.TestCase):
    def test_edit_answer(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            am = _mem(td)
            r = am.learn("What is the default?", "Old")
            am.edit(r["id"], answer_text="New answer")
            m = am.lookup("What is the default?")
            self.assertEqual(m.answer["answer_text"], "New answer")
            am.store.close()

    def test_merge_aliases(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            am = _mem(td)
            a = am.learn("What is the default model?", "Juggernaut")["id"]
            b = am.learn("What model is the default?", "Juggernaut")["id"]
            am.merge(b, a)
            m = am.lookup("What model is the default?")
            self.assertTrue(m.hit)
            self.assertEqual(m.answer["id"], a)
            am.store.close()

    def test_export_import_roundtrip(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td, tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td2:
            am = _mem(td)
            am.learn("What is the widget?", "A workstation.")
            payload = am.export()
            self.assertEqual(payload["version"], 1)
            self.assertEqual(len(payload["answers"]), 1)
            self.assertNotIn("embedding", payload["answers"][0])
            am.store.close()
            am2 = _mem(td2)
            r = am2.import_(payload)
            self.assertTrue(r["ok"] and r["imported"] == 1)
            self.assertTrue(am2.lookup("What is the widget?").hit)
            am2.store.close()

    def test_import_conflict_keeps_stronger(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            am = _mem(td)
            am.learn("What is the widget?", "Current trusted answer.")
            payload = {"version": 1, "answers": [{
                "canonical_question": "What is the widget?", "answer_text": "Stale import",
                "trust_state": "observed", "normalized_question": "what is the widget",
            }]}
            r = am.import_(payload)
            self.assertEqual(r["conflicts"], 1)
            m = am.lookup("What is the widget?")
            self.assertIn("Current", m.answer["answer_text"])
            am.store.close()

    def test_clear_experiences_keeps_trusted(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            am = _mem(td)
            am.record_exchange("What is X?", "X is a thing.")
            am.learn("What is Y?", "Y is another thing.")
            am.clear("experiences")
            self.assertEqual(am.store.query_one("SELECT COUNT(*) AS n FROM experiences")["n"], 0)
            self.assertTrue(am.lookup("What is Y?").hit)
            am.store.close()

    def test_retention_prunes_experiences(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            am = AnswerMemory(str(Path(td) / "am.db"), retention_days=1)
            am.record_exchange("What is X?", "X.")
            am.store.execute("UPDATE experiences SET ts=?", (time.time() - 3 * 86400,))
            am.record_exchange("What is Z?", "Z.")  # triggers prune
            rows = am.store.query("SELECT raw_question FROM experiences")
            self.assertEqual([r["raw_question"] for r in rows], ["What is Z?"])
            am.store.close()

    def test_rebuild_index(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            am = _mem(td)
            am.learn("What is the widget?", "A workstation.")
            am.store.execute("UPDATE answers SET embedding=NULL")
            r = am.rebuild_index()
            self.assertTrue(r["ok"] and r["reembedded"] == 1)
            am.store.close()

    def test_stats(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            am = _mem(td)
            am.learn("What is the widget?", "A workstation.")
            am.lookup("What is the widget?")
            s = am.stats()
            self.assertEqual(s["trusted"], 1)
            self.assertEqual(s["exact_hits"], 1)
            self.assertEqual(s["model_calls_avoided"], 1)
            self.assertGreaterEqual(s["avg_lookup_ms"], 0)
            am.store.close()

    def test_corrupt_db_quarantined_not_deleted(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            path = Path(td) / "am.db"
            path.write_bytes(b"not a sqlite database at all" * 100)
            am = AnswerMemory(str(path))
            self.assertTrue(am.available)  # quarantined + recreated
            self.assertTrue(am.store.corrupt_quarantine)
            self.assertTrue(Path(am.store.corrupt_quarantine).exists())
            am.store.close()

    def test_disabled_service_degrades_gracefully(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            am = AnswerMemory(str(Path(td) / "am.db"), enabled=False)
            self.assertFalse(am.available)
            self.assertFalse(am.lookup("anything").hit)
            self.assertIsNone(am.record_exchange("q", "a")["experience_id"])


class OrchestratorIntegrationTests(unittest.TestCase):
    """Memory hits must bypass model inference end-to-end."""

    def _agent(self, root: Path, am: AnswerMemory):
        profile = ModelProfile(
            id="local", endpoint="http://unused/v1", model="x",
            roles=["utility", "fast_coder", "primary_coder"], runtime="external",
        )
        config = AgentConfig(
            models=[profile], permissions={}, research_enabled=False,
            auto_verify_after_changes=False, review_after_changes=False,
        )

        class _RT:
            def refresh_hardware(self): pass
            def fresh_hardware(self, max_age_s=15.0): return self.refresh_hardware()
            def resident_model_ids(self): return []
            def rewarm_keep_loaded(self): return []
            def ensure_ready(self, p): return "http://127.0.0.1:1/v1"
            def recover(self, p): return "http://127.0.0.1:1/v1"

        agent = AgentOrchestrator(
            config, ModelRouter(config.models), ToolRegistry(config.permissions), _RT(),
            tasks=TaskStore(root), checkpoints=CheckpointManager(root),
            memory=ProjectMemory(root), repository_index=RepositoryIndex(root),
            conversation_manager=ConversationManager(root / "conv.json"),
            answer_memory=am,
        )
        return agent

    def test_trusted_hit_skips_model(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            root = Path(td)
            am = _mem(td)
            am.learn("What is the default image model?", "Juggernaut X v10")
            agent = self._agent(root, am)
            provider_calls = []
            agent._provider_for = lambda _p: (_ for _ in ()).throw(AssertionError("model invoked"))
            result = agent.run("What is the default image model?")
            self.assertEqual(result.content, "Juggernaut X v10")
            self.assertEqual(result.response_source, "answer_memory")
            self.assertTrue(result.memory["model_inference_skipped"])
            self.assertEqual(result.memory["memory_match_type"], "exact")
            self.assertEqual(result.routing.model_id, "answer-memory")
            self.assertFalse(provider_calls)
            am.store.close()

    def test_semantic_hit_skips_model(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            am = _mem(td)
            am.learn("What image checkpoint does Nexus Core normally use?", "Juggernaut X v10")
            agent = self._agent(Path(td), am)
            agent._provider_for = lambda _p: (_ for _ in ()).throw(AssertionError("model invoked"))
            result = agent.run("Which model does Nexus normally generate images with?")
            self.assertEqual(result.content, "Juggernaut X v10")
            self.assertEqual(result.response_source, "answer_memory")
            am.store.close()

    def test_timeline_event_for_memory_hit(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            am = _mem(td)
            am.learn("What is the default model?", "Juggernaut X")
            agent = self._agent(Path(td), am)
            events = []
            result = agent.run("What is the default model?", event_callback=events.append)
            kinds = [e.get("event", {}).get("type") for e in events if e.get("type") == "model"]
            self.assertIn("answer_memory", kinds)
            self.assertNotIn("selected", [e.get("event", {}).get("type") for e in events if e.get("type") == "model"],
                             "model-selection event must not fire when inference is skipped")
            am.store.close()

    def test_memory_unavailable_falls_back_to_model(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            am = _mem(td)
            am.available = False  # simulate DB failure mid-session
            agent = self._agent(Path(td), am)

            class _P:
                def complete(self, **kw):
                    return ProviderResponse(message={"role": "assistant", "content": "model answer"}, raw={})

            agent._provider_for = lambda _p: _P()
            result = agent.run("What is a neural network?")
            self.assertEqual(result.content, "model answer")
            self.assertNotEqual(result.response_source, "answer_memory")
            am.store.close()


class BenchmarkTests(unittest.TestCase):
    """Real latency measurements — generous bounds, values printed for the report."""

    def test_lookup_latency(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            am = _mem(td)
            for i in range(200):
                am.learn(f"What is concept number {i}?", f"Answer {i}.")
            import statistics
            exact, semantic = [], []
            for _ in range(20):
                t = time.perf_counter()
                am.lookup("What is concept number 42?")
                exact.append((time.perf_counter() - t) * 1000)
                t = time.perf_counter()
                am.lookup("Tell me about concept number 77")
                semantic.append((time.perf_counter() - t) * 1000)
            exact_med = statistics.median(exact)
            sem_med = statistics.median(semantic)
            print(f"\n[bench] exact={exact_med:.2f}ms semantic={sem_med:.2f}ms (200 answers)")
            self.assertLess(exact_med, 50)
            self.assertLess(sem_med, 500)
            am.store.close()


if __name__ == "__main__":
    unittest.main()


class TestContextDependentUtterances(unittest.TestCase):
    """'do it' / 'yes' refer to the previous turn — they must never be
    learned or resolved as standalone questions."""

    def test_context_dependent_detection(self):
        from localcodeagent.answer_memory import validation
        for text in ("do it", "yes", "yes do it", "go ahead", "sure",
                     "the second one", "proceed", "try again", "forget it"):
            self.assertTrue(validation.is_context_dependent(text), text)
        for text in ("do it again with the release build",
                     "continue the refactor of parser.py",
                     "what does this function do"):
            self.assertFalse(validation.is_context_dependent(text), text)

    def test_context_dependent_not_cacheable(self):
        from localcodeagent.answer_memory import validation
        for text in ("do it", "yes", "yes do it", "go ahead"):
            self.assertEqual(validation.classify_cacheability(text),
                             "task_specific", text)
            self.assertTrue(validation.is_noise(text), text)

    def test_action_requests_not_cacheable(self):
        """'Can you build me an app' is a request for work — a stored
        answer (e.g. a capability self-intro) is never a valid reply and
        must not surface as a lookup hit or injected hint."""
        from localcodeagent.answer_memory import validation
        for text in ("can you build me an app",
                     "i need you to build me an app",
                     "build me a website",
                     "can you fix the bug in parser.py",
                     "could you deploy the update",
                     "please implement the retry logic",
                     "create a new python script"):
            self.assertEqual(validation.classify_cacheability(text),
                             "task_specific", text)
        # Real capability/information questions still reuse memory.
        for text in ("what can you do", "can you explain decorators",
                     "how do I center a div", "what is a monad"):
            self.assertNotEqual(validation.classify_cacheability(text),
                                "task_specific", text)

    def test_context_dependent_exchange_not_learned(self):
        from localcodeagent.answer_memory import AnswerMemory
        with tempfile.TemporaryDirectory() as tmp:
            am = AnswerMemory(str(Path(tmp) / "am.db"))
            try:
                out = am.record_exchange("yes do it", "Done.")
                self.assertIsNone(out["answer_id"])
                rows = am.store.query("SELECT * FROM answers")
                self.assertEqual(rows, [])
            finally:
                am.close()

    def test_discourse_references_are_context_dependent(self):
        """Asks whose referent is the live conversation ("what were we
        talking about") must never match a stored answer — the observed
        defect replayed a stale voice-status answer for a summarize-
        the-chat ask. Stored answers can only describe the world, not
        the current thread."""
        from localcodeagent.answer_memory import validation
        for text in (
            "can u summarize what we were just talking about",
            "what preset were we discussing",
            "what were those things we still needed to fix",
            "did we ever settle on a port",
            "where were we",
            "back to the voice",
            "the other one from before",
            "ok continue",
        ):
            self.assertTrue(validation.is_context_dependent(text), text)
            self.assertEqual(validation.classify_cacheability(text),
                             "task_specific", text)
        # Ordinary standalone questions still use answer memory.
        for text in ("what time is it", "how do i make chicken quesadillas",
                     "what are your capabilities", "whats my ip",
                     "who is your father"):
            self.assertFalse(validation.is_context_dependent(text), text)
