import unittest

from localcodeagent.config import ModelProfile
from localcodeagent.models.router import ModelRouter


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.router = ModelRouter([
            ModelProfile(id="fast", endpoint="http://x", model="fast", roles=["utility", "fast_coder"], priority=10),
            ModelProfile(id="primary", endpoint="http://x", model="primary", roles=["primary_coder"], priority=10),
            ModelProfile(id="deep", endpoint="http://x", model="deep", roles=["deep_reasoner", "reviewer"], priority=10),
        ])

    def test_greetings_and_capability_questions_use_lightweight_utility_route(self):
        for prompt in ("hi", "Hello!", "what all can you do?"):
            with self.subTest(prompt=prompt):
                d = self.router.choose(prompt)
                self.assertEqual(d.role, "utility")
                self.assertEqual(d.model_id, "fast")

    def test_short_general_conversation_uses_utility(self):
        for prompt in ("tell me a joke", "how are you doing today?", "what do you think about coffee?"):
            with self.subTest(prompt=prompt):
                d = self.router.choose(prompt)
                self.assertEqual(d.role, "utility")
                self.assertEqual(d.model_id, "fast")

    def test_current_or_coding_requests_do_not_use_chat_only_route(self):
        self.assertNotEqual(self.router.choose("what is the latest Python release version?").role, "utility")
        self.assertNotEqual(self.router.choose("can you build me a webpage?").role, "utility")

    def test_coding_help_request_does_not_collapse_to_utility(self):
        d = self.router.choose("Help me debug this backend error")
        self.assertNotEqual(d.role, "utility")

    def test_simple_task_uses_lightest_tool_lane(self):
        # score<=1 requests take the lightest tool-capable lane
        # (light_coder) — the utility lane carries no tools.
        d = self.router.choose("Rename the Save button to Apply")
        self.assertEqual(d.role, "light_coder")
        self.assertEqual(d.model_id, "fast")

    def test_complex_task_uses_deep(self):
        d = self.router.choose("Find the root cause of this race condition and refactor the architecture after repeated compiler errors")
        self.assertEqual(d.role, "deep_reasoner")
        self.assertEqual(d.model_id, "deep")

    def test_question_shaped_chat_stays_utility(self):
        """Creation verbs aimed at a person are conversation — not work.
        'how could i have created you' must not buy a light-coder model
        just because it contains 'create'."""
        for prompt in (
                "how could i have created you",
                "if you're real then how could i have created you",
                "who made you", "who created you", "did i make you",
                "why are you ignoring me", "are you a bot",
                "if you were a function what would you do",
                "you were a great help today"):
            with self.subTest(prompt=prompt):
                d = self.router.choose(prompt)
                self.assertEqual(d.role, "utility")

    def test_question_with_artifact_keeps_work_lane(self):
        """Question shape alone isn't enough — an artifact object or
        demonstrative ('a logo', 'the repo', 'this') keeps tool-capable
        tiers reachable for real tasks."""
        for prompt in (
                "how do i fix this",
                "can you create a logo",
                "why did the build fail",
                "can you check the repo",
                "what is the latest python version"):
            with self.subTest(prompt=prompt):
                self.assertNotEqual(
                    self.router.choose(prompt).role, "utility")

    def test_review_phase(self):
        d = self.router.choose("Check the changes", phase="review")
        self.assertEqual(d.role, "reviewer")
        self.assertEqual(d.model_id, "deep")

    def test_canonical_role_matching_tier2(self):
        """A model configured with catalog tier-2 roles (light_coder)
        serves fast_coder/lightweight requests via alias resolution."""
        router = ModelRouter([
            ModelProfile(id="lite", endpoint="http://x", model="lite",
                         roles=["light_coder"], priority=10),
            ModelProfile(id="big", endpoint="http://x", model="big",
                         roles=["primary_coder"], priority=10),
        ])
        d = router.choose("Rename the Save button to Apply")
        # Simple tasks classify light_coder, which the tier-2 "lite" model
        # serves directly — no need for the primary model.
        self.assertEqual(d.model_id, "lite")
        d2 = router.choose("x", override="lightweight_reasoner")
        self.assertEqual(d2.model_id, "lite")

    def test_alias_roles_match_canonical_role(self):
        """primary_reasoner on a model satisfies a primary_coder request."""
        router = ModelRouter([
            ModelProfile(id="aliased", endpoint="http://x", model="m",
                         roles=["primary_reasoner"], priority=10),
        ])
        d = router.choose("fix this file")
        self.assertEqual(d.model_id, "aliased")

    def test_fallback_prefers_nearest_tier(self):
        """A light_coder request lands on the tier-2 model directly, never
        on the tier-1 utility model (which carries no tools)."""
        router = ModelRouter([
            ModelProfile(id="tiny", endpoint="http://x", model="tiny",
                         roles=["utility"], priority=10),
            ModelProfile(id="lite", endpoint="http://x", model="lite",
                         roles=["light_coder"], priority=10),
        ])
        d = router.choose("Rename the Save button to Apply")
        self.assertEqual(d.role, "light_coder")
        self.assertEqual(d.model_id, "lite")


    def test_vision_only_model_never_serves_text(self):
        """A vision-role model is modality-specialized: warm or not, it
        must never answer a text turn through the tier fallback — this is
        how qwen3-vl-4b once answered 'are you happy right now'."""
        router = ModelRouter([
            ModelProfile(id="vl", endpoint="http://x", model="vl",
                         roles=["vision"], priority=10),
            ModelProfile(id="big", endpoint="http://x", model="big",
                         roles=["primary_coder"], priority=10),
        ])
        # No utility model configured — the fallback must land on the
        # primary coder, not the vision model.
        d = router.choose("are you happy right now")
        self.assertEqual(d.model_id, "big")
        # Even with no primary either, a text turn degrades to an empty
        # candidate pool, never to vision.
        router2 = ModelRouter([
            ModelProfile(id="vl", endpoint="http://x", model="vl",
                         roles=["vision"], priority=10),
        ])
        self.assertRaises(ValueError, router2.choose,
                          "are you happy right now")

    def test_vision_role_still_selectable_for_vision(self):
        router = ModelRouter([
            ModelProfile(id="vl", endpoint="http://x", model="vl",
                         roles=["vision"], priority=10),
            ModelProfile(id="big", endpoint="http://x", model="big",
                         roles=["primary_coder"], priority=10),
        ])
        d = router.choose("x", override="vision")
        self.assertEqual(d.model_id, "vl")


if __name__ == "__main__":
    unittest.main()
