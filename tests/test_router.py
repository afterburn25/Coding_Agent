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

    def test_simple_task_uses_fast(self):
        d = self.router.choose("Rename the Save button to Apply")
        self.assertEqual(d.role, "fast_coder")
        self.assertEqual(d.model_id, "fast")

    def test_complex_task_uses_deep(self):
        d = self.router.choose("Find the root cause of this race condition and refactor the architecture after repeated compiler errors")
        self.assertEqual(d.role, "deep_reasoner")
        self.assertEqual(d.model_id, "deep")

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
        # light_coder canonicalizes to lightweight_reasoner; fast_coder →
        # primary_coder, so the light model should not match a fast_coder
        # request — the primary model serves it.
        self.assertEqual(d.model_id, "big")
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


if __name__ == "__main__":
    unittest.main()
