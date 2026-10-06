from __future__ import annotations

import unittest

from localcodeagent.research.policy import (
    LOCAL_CONFIDENT,
    WEB_OPTIONAL,
    WEB_RECOMMENDED,
    WEB_REQUIRED,
    WebResearchPolicy,
)


class WebResearchPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = WebResearchPolicy()

    def level(self, text: str) -> str:
        return self.policy.decide(text).level

    # --- WEB_REQUIRED ----------------------------------------------------
    def test_explicit_search_requests_required(self):
        for text in (
            "search the web for chicken gumbo",
            "look this up",
            "find out what llama.cpp does",
            "check online for react docs",
            "research this error",
            "find me a Popeyes style gravy recipe online",
            "check the documentation for asyncio",
            "verify online whether CUDA 12.5 is out",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.level(text), WEB_REQUIRED)

    def test_current_and_latest_required(self):
        for text in (
            "what is the current stable version of React",
            "what happened today in tech",
            "latest llama.cpp release",
            "current price of bitcoin",
            "who is currently the pope",
            "what's new in CUDA",
            "is the service down right now",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.level(text), WEB_REQUIRED)

    def test_user_url_required(self):
        d = self.policy.decide(
            "summarize this page https://docs.python.org/3/library/asyncio.html")
        self.assertEqual(d.level, WEB_REQUIRED)
        self.assertTrue(d.has_url)
        self.assertIn("docs.python.org", d.urls[0])

    # --- WEB_RECOMMENDED ---------------------------------------------------
    def test_unfamiliar_errors_recommended(self):
        for text in (
            "ConnectionResetError WinError 10054 llama.cpp",
            "why is Python giving me RuntimeError: event loop is closed?",
            "my tkinter app freezes when I download something",
            "npm install fails with EACCES permission denied",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.level(text), WEB_RECOMMENDED)

    def test_versioned_tech_and_precise_howto_recommended(self):
        self.assertEqual(
            self.level("how do I install CUDA 12.4 on windows"), WEB_RECOMMENDED)
        self.assertEqual(
            self.level("is React 19.2 compatible with my setup"), WEB_RECOMMENDED)

    def test_prior_uncertainty_recommended(self):
        d = self.policy.decide(
            "how does the flux scheduler work", prior_uncertain=True)
        self.assertEqual(d.level, WEB_RECOMMENDED)

    # --- WEB_OPTIONAL -------------------------------------------------------
    def test_recipes_recommendations_optional(self):
        for text in (
            "give me a recipe for chicken and sausage gumbo",
            "what is the best way to smoke ribs",
            "how do I reverse sear a ribeye",
            "recommend a good laptop for programming",
            "should i learn rust or go",
            "what is the difference between tcp and udp",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.level(text), WEB_OPTIONAL)

    def test_substantive_unknown_question_optional(self):
        d = self.policy.decide(
            "why do some deep sea creatures produce their own light through bioluminescence")
        self.assertEqual(d.level, WEB_OPTIONAL)

    # --- LOCAL_CONFIDENT -----------------------------------------------------
    def test_stable_knowledge_local(self):
        for text in (
            "what does photosynthesis do",
            "what is a loop",
            "what is RAM",
            "what is 2 + 2",
            "what is the capital of France",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.level(text), LOCAL_CONFIDENT)

    def test_casual_and_identity_local(self):
        for text in (
            "how are you",
            "are you real",
            "who made you",
            "are you my daughter",
            "tell me a joke",
            "what model are you running",
            "good night",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.level(text), LOCAL_CONFIDENT)

    def test_trusted_answer_keeps_local(self):
        d = self.policy.decide("what is my favorite color",
                               has_trusted_answer=True)
        self.assertEqual(d.level, LOCAL_CONFIDENT)

    # --- Query generation ---------------------------------------------------
    def test_queries_extract_payload_not_command(self):
        d = self.policy.decide("search the web for chicken gumbo")
        self.assertTrue(d.queries)
        self.assertIn("gumbo", d.queries[0])
        self.assertNotIn("search the web", d.queries[0].lower())

    def test_error_query_includes_error_and_tech(self):
        d = self.policy.decide("ConnectionResetError WinError 10054 llama.cpp")
        joined = " ".join(d.queries)
        self.assertIn("WinError", joined)
        self.assertIn("llama", joined.lower())

    def test_no_duplicate_queries(self):
        d = self.policy.decide("why does my async python script hang")
        lowered = [q.lower() for q in d.queries]
        self.assertEqual(len(lowered), len(set(lowered)))

    def test_url_not_in_search_query(self):
        d = self.policy.decide("read this page https://example.com/x")
        for q in d.queries:
            self.assertNotIn("http", q)


if __name__ == "__main__":
    unittest.main()
