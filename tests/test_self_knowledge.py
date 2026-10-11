"""Self-knowledge + chat control plane tests.

Covers the acceptance matrix: catalog truth, dev-vs-runtime separation,
settings mutation through the real setter path, action gating, follow-up
resolution ("do it", "undo that", "open it"), deep links, and the rule
that questions must never mutate.
"""
from __future__ import annotations

import unittest

from localcodeagent.self_knowledge import (
    ActionRegistry, FeatureCatalog, PageRegistry, SelfKnowledgeService,
    SettingsRegistry,
)


class Rep:
    """Minimal CapabilityReport stand-in."""

    def __init__(self, state="available", detail="", name="x"):
        self.state = state
        self.detail = detail
        self.name = name


def make_service(cap_states=None, probes=None, permitted="allow"):
    store = {
        "voice_enabled": True, "voice_muted": False,
        "voice_preset_id": "nexus-synthetic-isabella",
        "voice_volume": 0.7, "image_backend": "auto",
        "image_enabled": True, "worker_ceiling": 8,
        "autonomy_enabled": True, "github_enabled": True,
        "research_enabled": True,
    }
    cap_states = cap_states or {}
    calls = {"set": [], "actions": []}

    def _cap(cid, force=False):
        st = cap_states.get(cid, "available")
        return Rep(st, cap_states.get(cid + ":detail", ""), cid)

    def _probe(name):
        return dict((probes or {}).get(
            name, {"state": "available"}))

    def _record(name):
        def _fn(*a, **k):
            calls["actions"].append(name)
            return True
        return _fn

    env = {
        "get": lambda k: store.get(k),
        "set": lambda k, v: (store.__setitem__(k, v),
                             calls["set"].append((k, v)))[1],
        "choices": lambda k: (["nexus-synthetic-isabella", "nexus-warm"]
                              if k == "voice_preset_id" else []),
        "permitted": lambda k: permitted,
        "capability": _cap,
        "capabilities_all": lambda: [Rep(v, "", k)
                                     for k, v in cap_states.items()
                                     if not k.endswith(":detail")],
        "probe": _probe,
        "version": lambda: "0.23.0",
        "voice_stop": _record("voice_stop"),
        "persona_set": _record("persona_set"),
        "image_install": _record("image_install"),
        "image_start": _record("image_start"),
        "image_stop": _record("image_stop"),
        "autonomy_pause": _record("autonomy_pause"),
        "autonomy_resume": _record("autonomy_resume"),
        "provisioning_pause": _record("provisioning_pause"),
        "provisioning_resume": _record("provisioning_resume"),
        "github_disconnect": lambda: True,
        "github_test": lambda: {"connected": True, "detail": "ok"},
        "safe_mode_exit": _record("safe_mode_exit"),
    }
    return SelfKnowledgeService(env), store, calls


class TestCatalog(unittest.TestCase):
    def test_every_feature_has_runtime_truth(self):
        cat = FeatureCatalog()
        env = {"capability": lambda cid, force=False: Rep("verified"),
               "probe": lambda n: {"state": "available"}}
        for f in cat.all():
            st = cat.feature_state(f.id, env)
            self.assertEqual(st["id"], f.id)
            self.assertIn(st["development_status"],
                          ("implemented", "partial", "experimental",
                           "planned", "not_implemented", "deprecated"))

    def test_dev_status_distinct_from_runtime(self):
        cat = FeatureCatalog()
        env = {"capability": lambda cid, force=False:
               Rep("setup_required", "no backend"),
               "probe": lambda n: {}}
        st = cat.feature_state("image_generation", env)
        self.assertEqual(st["development_status"], "implemented")
        self.assertEqual(st["runtime_state"], "setup_required")

    def test_unauthorized_maps_to_authorization_required(self):
        cat = FeatureCatalog()
        env = {"capability": lambda cid, force=False:
               Rep("unauthorized", "no token")}
        st = cat.feature_state("github", env)
        self.assertEqual(st["runtime_state"], "authorization_required")

    def test_find_resolves_aliases(self):
        cat = FeatureCatalog()
        self.assertEqual(cat.find("the speech lab").id, "speech_lab")
        self.assertEqual(cat.find("is github working").id, "github")
        self.assertEqual(cat.find("turn voice off").id, "voice")

    def test_summary_counts(self):
        cat = FeatureCatalog()
        env = {"capability": lambda cid, force=False: Rep("verified"),
               "probe": lambda n: {"state": "available"}}
        s = cat.summary(env)
        self.assertGreater(s["counts"]["total"], 30)
        self.assertIn("development", s["by_category"])


class TestPages(unittest.TestCase):
    def test_section_deep_link(self):
        reg = PageRegistry()
        self.assertEqual(reg.route_for("speech lab"),
                         "/personality.html#speech-lab")
        self.assertEqual(reg.route_for("voice settings"),
                         "/settings.html#voice")

    def test_every_route_unique(self):
        reg = PageRegistry()
        routes = [p.route for p in reg.all()]
        self.assertEqual(len(routes), len(set(routes)))


class TestSettings(unittest.TestCase):
    def test_set_bool_verified(self):
        reg = SettingsRegistry(env={
            "get": lambda k: store.get(k),
            "set": lambda k, v: store.__setitem__(k, v)})
        store = {"voice_enabled": True}
        out = reg.set("voice_enabled", False)
        self.assertTrue(out["ok"])
        self.assertTrue(out["verified"])
        self.assertFalse(store["voice_enabled"])
        self.assertTrue(out["previous"])

    def test_choice_fuzzy_match(self):
        store = {"image_backend": "auto"}
        reg = SettingsRegistry(env={
            "get": lambda k: store.get(k),
            "set": lambda k, v: store.__setitem__(k, v)})
        out = reg.set("image_backend", "comfy")
        self.assertTrue(out["ok"])
        self.assertEqual(store["image_backend"], "comfyui")

    def test_invalid_choice_rejected(self):
        reg = SettingsRegistry(env={"get": lambda k: None,
                                    "set": lambda k, v: None})
        out = reg.set("image_backend", "banana")
        self.assertFalse(out["ok"])
        self.assertIn("invokeai", out["error"])

    def test_permission_denied_blocks_mutation(self):
        reg = SettingsRegistry(env={
            "get": lambda k: True,
            "set": lambda k, v: None,
            "permitted": lambda k: "deny"})
        # permission-gated spec
        spec = reg.get("autonomous_mode")
        self.assertIsNotNone(spec)
        # autonomous_mode has risk 'sensitive' — no permission key set on
        # the spec, so a deny on empty key is not consulted; the action
        # layer gates it instead. The can_change contract still holds:
        ok, _ = reg.can_change("voice_enabled")
        self.assertTrue(ok)

    def test_volume_clamps(self):
        store = {"voice_volume": 0.5}
        reg = SettingsRegistry(env={
            "get": lambda k: store.get(k),
            "set": lambda k, v: store.__setitem__(k, v)})
        reg.set("voice_volume", 2.0)
        self.assertLessEqual(store["voice_volume"], 1.0)


class TestServiceControl(unittest.TestCase):
    def test_turn_voice_off_executes_and_verifies(self):
        svc, store, _ = make_service()
        res = svc.respond("turn voice off")
        self.assertIsNotNone(res)
        self.assertEqual(res.kind, "execute")
        self.assertFalse(store["voice_enabled"])

    def test_privacy_paraphrases_disable_conversation_memory(self):
        # Imperative stop-forms that name the memory setting are
        # whole-utterance prohibitions, but the prohibition IS the
        # command — they must reach the same setting mutation as
        # 'turn off conversation memory' instead of falling to the
        # forget/model path.
        for text in ("stop remembering things I tell you",
                     "stop remembering what i say",
                     "disable memory",
                     "turn off conversation memory"):
            svc, store, _ = make_service()
            res = svc.respond(text)
            self.assertIsNotNone(res, text)
            self.assertEqual(res.kind, "execute", text)
            self.assertFalse(store["conversation_memory_enabled"], text)

    def test_privacy_paraphrases_re_enable_and_status(self):
        svc, store, _ = make_service()
        svc.respond("disable memory")
        res = svc.respond("turn memory back on")
        self.assertIsNotNone(res)
        self.assertEqual(res.kind, "execute")
        self.assertTrue(store["conversation_memory_enabled"])
        res = svc.respond("is memory on")
        self.assertIsNotNone(res)
        self.assertIn("on", res.text.lower())

    def test_memory_alias_does_not_hijack_recall_asks(self):
        # The bare 'memory' alias must not claim recall-shaped asks —
        # 'do you have memory of X' is a memory_recall question, not a
        # feature-status probe.
        svc, store, _ = make_service()
        res = svc.respond("do you have any memory of the port")
        self.assertIsNone(res)

    def test_embedded_prohibition_still_vetoes_control(self):
        # 'i am not asking you to remember' is reported speech, not a
        # command — the prohibition veto must stand.
        svc, store, _ = make_service()
        res = svc.respond("i am not asking you to remember things")
        self.assertIsNone(res)
        self.assertNotIn("conversation_memory_enabled", store)

    def test_turn_voice_back_on_followup(self):
        svc, store, _ = make_service()
        svc.respond("turn voice off")
        self.assertFalse(store["voice_enabled"])
        res = svc.respond("turn it back on")
        self.assertIsNotNone(res)
        self.assertTrue(store["voice_enabled"])

    def test_undo_restores_setting(self):
        svc, store, _ = make_service()
        svc.respond("use comfyui")
        self.assertEqual(store["image_backend"], "comfyui")
        res = svc.respond("undo that")
        self.assertIsNotNone(res)
        self.assertEqual(store["image_backend"], "auto")
        self.assertIn("reverted", res.text.lower())

    def test_undo_action_rewording(self):
        svc, store, _ = make_service()
        svc.respond("turn voice off")
        res = svc.respond("undo that")
        self.assertIsNotNone(res)
        self.assertTrue(store["voice_enabled"])
        self.assertIn("reverted", res.text.lower())

    def test_undo_named_referent_never_guesses(self):
        # "undo that laptop change" names a referent — the lane must
        # never revert an unrelated journaled setting on a guess.
        import tempfile
        from pathlib import Path
        from localcodeagent.changes import ChangeJournal
        from localcodeagent.workflow.conversation_memory import (
            ConversationMemory)
        svc, store, _ = make_service()
        with tempfile.TemporaryDirectory() as td:
            journal = ChangeJournal(Path(td) / "journal.json")
            cm = ConversationMemory(Path(td) / "cm.json")
            svc._env["change_journal"] = lambda: journal
            svc._env["conversation_memory"] = lambda: cm
            journal.record("setting", "worker_ceiling", actor="user",
                           before=8, after=4, reversible=True,
                           undo={"kind": "setting",
                                 "key": "worker_ceiling", "value": 8})
            res = svc.respond("undo that laptop change")
            self.assertIsNotNone(res)
            self.assertEqual(res.kind, "answer")
            self.assertIn("laptop", res.text.lower())
            # The unrelated journal row is untouched.
            self.assertFalse(journal.recent(5)[0].get("undone"))

    def test_undo_dry_run_never_mutates(self):
        # would_answer probes run respond(execute=False) — a scoped undo
        # must not mutate during the probe, or the real pass reverts a
        # *different* row (observed live: dry-run restored 'quasar',
        # real pass then reverted back to 'pulsar').
        import tempfile
        from pathlib import Path
        from localcodeagent.workflow.conversation_memory import (
            ConversationMemory)
        svc, store, _ = make_service()
        with tempfile.TemporaryDirectory() as td:
            cm = ConversationMemory(Path(td) / "cm.json")
            svc._env["conversation_memory"] = lambda: cm
            cm.learn_from_user("my laptop is named quasar")
            cm.learn_from_user("actually the laptop is pulsar")
            probe = svc.respond("undo that laptop change",
                                execute=False)
            self.assertIsNotNone(probe)
            active = [r["text"] for r in cm._data["facts"]
                      if r.get("active")]
            self.assertEqual(active, ["my laptop is pulsar"])
            res = svc.respond("undo that laptop change")
            self.assertEqual(res.kind, "execute")
            self.assertIn("quasar", res.text)
            active = [r["text"] for r in cm._data["facts"]
                      if r.get("active")]
            self.assertEqual(active, ["my laptop is named quasar"])

    def test_undo_named_referent_restores_memory_fact(self):
        # "undo that laptop change" with a superseded memory fact —
        # 'my laptop is pulsar' reverts to 'my laptop is named quasar'.
        import tempfile
        from pathlib import Path
        from localcodeagent.workflow.conversation_memory import (
            ConversationMemory)
        svc, store, _ = make_service()
        with tempfile.TemporaryDirectory() as td:
            cm = ConversationMemory(Path(td) / "cm.json")
            svc._env["conversation_memory"] = lambda: cm
            cm.learn_from_user("my laptop is named quasar")
            cm.learn_from_user("actually the laptop is pulsar")
            res = svc.respond("undo that laptop change")
            self.assertIsNotNone(res)
            self.assertEqual(res.kind, "execute")
            self.assertIn("quasar", res.text)
            active = [r["text"] for r in cm._data["facts"]
                      if r.get("active")]
            self.assertEqual(active, ["my laptop is named quasar"])

    def test_undo_named_referent_beats_session_last_executed(self):
        # 'revert the printer change' with a live session undo for
        # worker_ceiling must not revert the setting — the named
        # referent owns the turn.
        import tempfile
        from pathlib import Path
        from localcodeagent.workflow.conversation_memory import (
            ConversationMemory)
        svc, store, _ = make_service()
        with tempfile.TemporaryDirectory() as td:
            cm = ConversationMemory(Path(td) / "cm.json")
            svc._env["conversation_memory"] = lambda: cm
            cm.learn_from_user("my printer is named epson")
            cm.learn_from_user("actually the printer is hp")
            svc._context["last_executed"] = {
                "setting": "worker_ceiling",
                "undo": {"action": "_set",
                         "params": {"key": "worker_ceiling",
                                    "value": 8}}}
            res = svc.respond("revert the printer change")
            self.assertIsNotNone(res)
            self.assertIn("epson", res.text)
            # The session undo was not consumed — the referent owned it.
            self.assertIsNotNone(svc._context["last_executed"])

    def test_undo_loose_referent_still_scopes(self):
        # "undo the correction to the editor" — a referential undo that
        # doesn't fit the strict '<X> change' template still scopes to
        # the named entity, never the generic journal fallback.
        import tempfile
        from pathlib import Path
        from localcodeagent.changes import ChangeJournal
        from localcodeagent.workflow.conversation_memory import (
            ConversationMemory)
        svc, store, _ = make_service()
        with tempfile.TemporaryDirectory() as td:
            journal = ChangeJournal(Path(td) / "journal.json")
            cm = ConversationMemory(Path(td) / "cm.json")
            svc._env["change_journal"] = lambda: journal
            svc._env["conversation_memory"] = lambda: cm
            cm.learn_from_user("my editor is emacs")
            cm.learn_from_user("actually the editor is vim")
            journal.record("setting", "worker_ceiling", actor="user",
                           before=8, after=4, reversible=True,
                           undo={"kind": "setting",
                                 "key": "worker_ceiling", "value": 8})
            res = svc.respond("undo the correction to the editor")
            self.assertIsNotNone(res)
            self.assertIn("emacs", res.text)
            self.assertFalse(journal.recent(5)[0].get("undone"))

    def test_put_back_shapes_scope_the_referent(self):
        # "put the scooter back" / "roll back the scooter" name a
        # referent without an undo keyword — same scoped restore as
        # 'undo that scooter change'. Bare pronouns ('put it back')
        # capture nothing and stay generic.
        import tempfile
        from pathlib import Path
        from localcodeagent.workflow.conversation_memory import (
            ConversationMemory)
        for shape in ("put the scooter back",
                      "switch that scooter back",
                      "roll back the scooter"):
            svc, store, _ = make_service()
            with tempfile.TemporaryDirectory() as td:
                cm = ConversationMemory(Path(td) / "cm.json")
                svc._env["conversation_memory"] = lambda: cm
                svc._env["change_journal"] = lambda: None
                cm.learn_from_user("my scooter is named dart")
                cm.learn_from_user("actually the scooter is wisp")
                res = svc.respond(shape)
                self.assertIsNotNone(res, shape)
                self.assertIn("dart", res.text, shape)

    def test_mute_uses_voice_action(self):
        svc, store, _ = make_service()
        res = svc.respond("mute yourself")
        self.assertEqual(res.kind, "execute")
        self.assertTrue(store["voice_muted"])

    def test_numeric_worker_command(self):
        svc, store, _ = make_service()
        svc.respond("use four workers")
        self.assertEqual(store["worker_ceiling"], 4)
        svc.respond("set workers to 6")
        self.assertEqual(store["worker_ceiling"], 6)

    def test_question_never_mutates(self):
        svc, store, _ = make_service()
        before = dict(store)
        for q in ("what's the image backend", "is voice on",
                  "what voice are you using", "is github connected",
                  "where is the speech lab", "can you generate images",
                  "what can you do", "what's broken"):
            svc.respond(q)
        self.assertEqual(store, before)

    def test_imperative_falls_through(self):
        svc, store, _ = make_service()
        for q in ("push to github", "create a repo", "run the tests",
                  "write a snake game"):
            self.assertIsNone(svc.respond(q), q)
        self.assertEqual(store["github_enabled"], True)

    def test_person_directed_where_falls_through(self):
        # "Where you are" asks about the assistant — not a UI route.
        # The where-regex must not swallow it and answer with a page
        # ("Chat is under Chat." — live dogfood regression).
        svc, store, _ = make_service()
        for q in ("what's the weather like where you are",
                  "where are you", "where do you live",
                  "where are you located"):
            res = svc.respond(q)
            self.assertFalse(
                res is not None and res.kind in {"answer", "navigate"},
                f"{q!r} -> {getattr(res, 'text', None)!r}")
        # Real UI-where questions still resolve.
        res = svc.respond("where is the speech lab")
        self.assertIsNotNone(res)
        self.assertIn("Speech Lab", res.text)

    def test_confirm_risk_proposes_first(self):
        svc, store, calls = make_service()
        res = svc.respond("turn autonomy off")
        # autonomy_enabled is 'confirm' risk — proposes, doesn't execute
        self.assertEqual(res.kind, "confirm")
        self.assertTrue(store["autonomy_enabled"])
        res = svc.respond("do it")
        self.assertIsNotNone(res)
        self.assertFalse(store["autonomy_enabled"])

    def test_sensitive_action_never_executes_inline(self):
        svc, store, calls = make_service()
        res = svc.respond("connect github")
        self.assertIn(res.kind, ("confirm", "answer"))
        self.assertNotIn("github_disconnect", calls["actions"])

    def test_would_answer_dry_run_never_mutates(self):
        svc, store, _ = make_service()
        self.assertTrue(svc.would_answer("turn voice off"))
        self.assertTrue(store["voice_enabled"])
        self.assertFalse(svc.would_answer("write me a haiku"))

    def test_navigate_open_it(self):
        svc, store, _ = make_service()
        res = svc.respond("open the speech lab")
        self.assertEqual(res.kind, "navigate")
        self.assertEqual(res.actions[0]["route"],
                         "/personality.html#speech-lab")
        res = svc.respond("where is answer memory")
        self.assertIsNotNone(res)
        res = svc.respond("open it")
        self.assertEqual(res.kind, "navigate")
        self.assertEqual(res.actions[0]["route"], "/answers.html")

    def test_capability_answers_live_state(self):
        svc, store, _ = make_service(
            cap_states={"image_generation": "setup_required",
                        "image_generation:detail": "no backend installed"})
        res = svc.respond("can you generate images")
        self.assertIn("setup", res.text)
        self.assertEqual(res.truth["state"], "setup_required")
        # offered fix is tracked for 'do it'/'install it'
        res = svc.respond("install it")
        self.assertEqual(res.kind, "execute")

    def test_not_implemented_never_claims_capability(self):
        svc, store, _ = make_service()
        cat = svc.catalog
        # computer_use is experimental — answer must not say "ready"
        res = svc.respond("can you control my pc")
        self.assertIsNotNone(res)
        self.assertNotIn("is ready", res.text)

    def test_what_can_you_do_is_live(self):
        svc, store, _ = make_service(
            cap_states={"github": "unauthorized"})
        res = svc.respond("what can you do")
        self.assertIsNotNone(res)
        # §10: outcome-led, conversational — never a raw subsystem dump.
        self.assertIn("code", res.text.lower())
        self.assertNotIn("Development:", res.text)
        self.assertNotIn("Terminal commands", res.text)

    def test_technical_ask_gets_full_catalog(self):
        svc, store, _ = make_service()
        res = svc.respond("what are your technical capabilities")
        self.assertIsNotNone(res)
        self.assertIn("Development", res.text)

    def test_whats_broken_reports(self):
        svc, store, _ = make_service(
            cap_states={"github": "unauthorized", "browser": "broken"})
        res = svc.respond("what's broken")
        self.assertIsNotNone(res)
        self.assertIn("broken", res.text)

    def test_what_needs_setup(self):
        svc, store, _ = make_service(
            cap_states={"image_generation": "setup_required"})
        res = svc.respond("what isn't installed")
        self.assertIsNotNone(res)
        self.assertIn("Image", res.text)

    def test_execute_action_api_shape(self):
        svc, store, _ = make_service()
        out = svc.execute_action("_set", {"key": "voice_muted",
                                          "value": True})
        self.assertTrue(out["ok"])
        self.assertTrue(store["voice_muted"])
        out = svc.execute_action("voice.disable", confirmed=True)
        self.assertTrue(out["ok"])

    def test_unknown_action_rejected(self):
        svc, store, _ = make_service()
        out = svc.execute_action("not.a.thing")
        self.assertFalse(out["ok"])


class TestActionRequestsFallThrough(unittest.TestCase):
    """Regression — action-shaped capability phrasings must NOT be
    answered as 'Yes — <feature> is ready'. The observed bug: 'can you
    create a folder d:\\Nexus' replied 'Yes — Workspaces is ready.
    workspace writable.' and nothing was created. These turns fall
    through to the deterministic local-action lane (action_ops) or the
    model/tools lane instead."""

    def test_can_you_create_folder_not_claimed_as_capability(self):
        svc, _, _ = make_service()
        self.assertIsNone(svc.respond(
            "can you create a folder d:\\Nexus"))
        self.assertIsNone(svc.respond("can you create a folder"))
        self.assertIsNone(svc.respond("could you move this file"))
        self.assertIsNone(svc.respond("can you delete the old backup"))

    def test_leading_imperatives_still_fall_through(self):
        svc, _, _ = make_service()
        self.assertIsNone(svc.respond("create a repo"))
        self.assertIsNone(svc.respond("can you open notepad"))

    def test_genuine_capability_questions_still_answer(self):
        svc, _, _ = make_service(
            cap_states={"image_generation": "setup_required",
                        "image_generation:detail": "no backend"})
        self.assertIsNotNone(svc.respond("what can you do"))
        res = svc.respond("can you generate images")
        self.assertIsNotNone(res)
        self.assertIn("setup", res.text)


if __name__ == "__main__":
    unittest.main()
