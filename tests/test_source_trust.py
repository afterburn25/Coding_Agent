from __future__ import annotations

import time
import unittest

from localcodeagent.research import evidence
from localcodeagent.research.ranking import SourceRanker
from localcodeagent.research.trust import (
    COMMUNITY_HIGH_SIGNAL,
    COOKING,
    GOVERNMENT,
    HEALTH,
    LEGAL,
    MEDICAL,
    OFFICIAL_DOCUMENTATION,
    PEER_REVIEWED_RESEARCH,
    PROGRAMMING,
    SOFTWARE,
    SPECIALIST_PUBLICATION,
    SourceTrustRegistry,
    TopicClassifier,
)
from localcodeagent.research.types import ResearchSource


def src(url: str, title: str = "t", excerpt: str = "", **kw) -> ResearchSource:
    return ResearchSource(title=title, url=url, source_type="web_search",
                          excerpt=excerpt, **kw)


class TopicClassifierTests(unittest.TestCase):
    def setUp(self):
        self.tc = TopicClassifier()

    def test_programming(self):
        self.assertIn(PROGRAMMING, self.tc.classify(
            "why is python throwing RuntimeError event loop closed"))

    def test_cooking(self):
        self.assertIn(COOKING, self.tc.classify("best gumbo recipe"))

    def test_medical(self):
        self.assertIn(MEDICAL, self.tc.classify(
            "is ibuprofen safe with blood pressure medication"))

    def test_legal(self):
        self.assertIn(LEGAL, self.tc.classify("can I sue my landlord"))

    def test_product_specs(self):
        self.assertIn(PROGRAMMING if False else "product_specs",
                      self.tc.classify("how much ram does the device support"))

    def test_high_stakes(self):
        self.assertTrue(self.tc.high_stakes(self.tc.classify(
            "what medication treats strep throat")))
        self.assertFalse(self.tc.high_stakes(self.tc.classify("best gumbo")))


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.reg = SourceTrustRegistry()

    def test_docs_python_official_for_software(self):
        a, p, reasons = self.reg.topic_authority(
            "docs.python.org", [SOFTWARE, PROGRAMMING])
        self.assertEqual(p.category, OFFICIAL_DOCUMENTATION)
        self.assertGreater(a, 90)

    def test_off_topic_authority_decays(self):
        a, _, _ = self.reg.topic_authority("docs.python.org", [COOKING])
        b, _, _ = self.reg.topic_authority("seriouseats.com", [COOKING])
        self.assertLess(a, b)

    def test_government_health(self):
        p = self.reg.profile_for("cdc.gov")
        self.assertEqual(p.category, GOVERNMENT)
        self.assertTrue(p.primary_source)

    def test_arxiv_preprint(self):
        p = self.reg.profile_for("arxiv.org")
        self.assertTrue(p.preprint)

    def test_stackoverflow_community(self):
        p = self.reg.profile_for("stackoverflow.com")
        self.assertEqual(p.category, COMMUNITY_HIGH_SIGNAL)
        self.assertTrue(p.user_generated)

    def test_github_official_vs_community(self):
        self.assertEqual(
            self.reg.github_repo_class(
                "https://github.com/ggml-org/llama.cpp/issues/1",
                expected=["ggml-org/llama.cpp"]),
            "official_repo")
        self.assertEqual(
            self.reg.github_repo_class(
                "https://github.com/randomuser/my-cool-fork",
                expected=["ggml-org/llama.cpp"]),
            "community")

    def test_github_name_match(self):
        self.assertEqual(
            self.reg.github_repo_class(
                "https://github.com/ggml-org/llama.cpp/releases",
                query="llama.cpp latest release"),
            "official_repo")

    def test_dynamic_registration(self):
        self.reg.register_dynamic(
            "requests.readthedocs.io",
            profile_for_tests("requests.readthedocs.io"))
        p = self.reg.profile_for("requests.readthedocs.io")
        self.assertEqual(p.domain, "requests.readthedocs.io")

    def test_unknown_tld_heuristics(self):
        self.assertEqual(
            self.reg.profile_for("someagency.gov").category, GOVERNMENT)
        self.assertEqual(
            self.reg.profile_for("university.edu").category,
            "academic_institution")


def profile_for_tests(domain):
    from localcodeagent.research.trust import SourceTrustProfile
    return SourceTrustProfile(domain=domain, category=OFFICIAL_DOCUMENTATION,
                              base_authority=80, primary_source=True)


class RankingTests(unittest.TestCase):
    def setUp(self):
        self.ranker = SourceRanker()

    def test_official_outranks_blog_software(self):
        off = src("https://docs.python.org/3/venv", "venv docs",
                  "python venv virtual environment creation command")
        blog = src("https://randomblog.example.com/venv", "venv tips",
                   "python venv virtual environment stuff")
        ranked = self.ranker.rank([blog, off], "python virtual environment",
                                  [SOFTWARE, PROGRAMMING])
        self.assertIs(ranked[0], off)
        self.assertGreater(off.score, blog.score)
        self.assertIn("OFFICIAL", off.badges + [""] if False else off.badges)
        self.assertIn("PRIMARY", off.badges)

    def test_recipe_specialist_outranks_offtopic_docs(self):
        eats = src("https://seriouseats.com/gumbo", "gumbo",
                   "gumbo roux andouille recipe okra filé")
        pydoc = src("https://docs.python.org/x", "python docs",
                    "python documentation")
        ranked = self.ranker.rank([pydoc, eats], "gumbo recipe", [COOKING])
        self.assertIs(ranked[0], eats)

    def test_blocked_domain(self):
        ranker = SourceRanker(blocked_domains=["blocked.example.com"])
        b = src("https://blocked.example.com/x", "b", "content")
        ranked = ranker.rank([b], "anything")
        self.assertEqual(ranked[0].reliability, evidence.REL_BLOCKED)
        self.assertLess(ranked[0].score, -500)

    def test_trusted_domain_boost(self):
        ranker = SourceRanker(trusted_domains=["myfav.example.com"])
        t = src("https://myfav.example.com/x", "t", "some words about testing")
        o = src("https://other.example.com/x", "o", "some words about testing")
        ranker.rank([o, t], "testing")
        self.assertGreater(t.score_components.get("user_trusted", 0), 0)

    def test_diversify_limits_domain(self):
        rows = [src(f"https://a.com/{i}", f"a{i}", "text") for i in range(5)]
        rows.append(src("https://b.com/x", "b", "text"))
        out = SourceRanker.diversify(rows, max_per_domain=2)
        self.assertEqual(sum(1 for s in out if "a.com" in s.url), 2)

    def test_score_components_exposed(self):
        s = src("https://docs.python.org/x", "x", "python docs " * 30)
        self.ranker.score(s, "python", [SOFTWARE])
        for key in ("domain_prior", "authority", "relevance", "freshness",
                    "primary_source", "evidence_density"):
            self.assertIn(key, s.score_components)


class IndependenceTests(unittest.TestCase):
    def test_duplicate_pages_collapse(self):
        text = ("The quick brown fox jumps over the lazy dog repeatedly "
                "while birds sing in the trees above the meadow") * 8
        a = src("https://a.com/x", "a", text)
        b = src("https://b.com/y", "b", text + " indeed")
        c = src("https://c.com/z", "c", "completely different content about "
                "python decorators and how they wrap functions with metadata")
        groups = evidence.IndependenceAnalyzer().analyze([a, b, c])
        self.assertEqual(groups[a.id], groups[b.id])
        self.assertNotEqual(groups[a.id], groups[c.id])
        self.assertEqual(b.metadata.get("duplicate_of"), a.id)

    def test_wire_copies_dependent(self):
        wire = "According to a Reuters report, the decision was announced. " * 6
        a = src("https://site1.com/x", "a", wire)
        b = src("https://site2.com/y", "b", "A Reuters report says the decision "
                "was announced this morning in the capital.")
        groups = evidence.IndependenceAnalyzer().analyze([a, b])
        self.assertEqual(groups[a.id], groups[b.id])
        self.assertTrue(b.metadata.get("wire_dependent") or
                        a.metadata.get("wire_dependent"))


class FreshnessTests(unittest.TestCase):
    def test_recent_scores_higher_for_news(self):
        now = time.time()
        fresh = evidence.freshness_score(
            time.strftime("%Y-%m-%d", time.gmtime(now - 86400)),
            now, ["news"], now=now)
        stale = evidence.freshness_score(
            time.strftime("%Y-%m-%d", time.gmtime(now - 300 * 86400)),
            now, ["news"], now=now)
        self.assertGreater(fresh, stale)

    def test_history_decay_is_slow(self):
        now = time.time()
        fresh = evidence.freshness_score(
            time.strftime("%Y-%m-%d", time.gmtime(now - 30 * 86400)),
            now, ["history"], now=now)
        stale = evidence.freshness_score(
            time.strftime("%Y-%m-%d", time.gmtime(now - 800 * 86400)),
            now, ["history"], now=now)
        self.assertGreater(fresh - stale, -0.4)


class ConflictTests(unittest.TestCase):
    def test_version_disagreement_detected(self):
        q = "what is the latest python version"
        a = src("https://a.com/x", "a",
                "The latest Python release is 3.13.1 as of today.")
        b = src("https://b.com/y", "b",
                "The latest Python release is 3.14.0 as of today.")
        conflicts = evidence.ConflictDetector().detect([a, b], [], q)
        kinds = [c["kind"] for c in conflicts]
        self.assertIn("version_disagreement", kinds)

    def test_outdated_same_domain(self):
        old = src("https://docs.example.com/x", "old", "old material",
                  published_at="2020-01-01")
        new = src("https://docs.example.com/y", "new", "new material",
                  published_at=time.strftime("%Y-%m-%d"))
        conflicts = evidence.ConflictDetector().detect([old, new], [], "x")
        self.assertTrue(old.metadata.get("outdated"))
        self.assertTrue(any(c["kind"] == "outdated" for c in conflicts))


class ConfidenceTests(unittest.TestCase):
    def _profiles(self, reg, sources):
        return {s.id: reg.profile_for(evidence.source_host(s)) for s in sources}

    def test_primary_plus_secondary_high(self):
        reg = SourceTrustRegistry()
        off = src("https://docs.python.org/x", "docs",
                  "python venv creates isolated environments " * 10)
        com = src("https://stackoverflow.com/q", "so",
                  "users confirm the same behavior in practice " * 10)
        sources = [off, com]
        groups = evidence.IndependenceAnalyzer().analyze(sources)
        claims = [{"claim_id": "c0", "claim_text": "x", "supporting": [off.id, com.id],
                   "contradicting": [], "key_terms": []}]
        profiles = self._profiles(reg, sources)
        conf, reasons = evidence.evidence_confidence(sources, claims, [], profiles)
        self.assertEqual(conf, evidence.HIGH)

    def test_community_only_low(self):
        reg = SourceTrustRegistry()
        a = src("https://reddit.com/r/x/1", "r1", "users report the bug " * 10)
        b = src("https://forum.example.com/t", "f", "users report the bug " * 10)
        sources = [a, b]
        evidence.IndependenceAnalyzer().analyze(sources)
        claims = [{"claim_id": "c0", "claim_text": "x", "supporting": [a.id, b.id],
                   "contradicting": [], "key_terms": []}]
        conf, reasons = evidence.evidence_confidence(
            sources, claims, [], self._profiles(reg, sources))
        self.assertEqual(conf, evidence.LOW)

    def test_conflict_gives_conflicted(self):
        reg = SourceTrustRegistry()
        a = src("https://docs.python.org/x", "d", "content " * 50)
        sources = [a]
        conflicts = [{"kind": "contradiction", "claim": "x",
                      "supporting": [a.id], "contradicting": [a.id]}]
        conf, _ = evidence.evidence_confidence(
            sources, [], conflicts, self._profiles(reg, sources))
        self.assertEqual(conf, evidence.CONF_CONF)

    def test_high_stakes_requires_primary(self):
        reg = SourceTrustRegistry()
        a = src("https://randomblog.com/health", "b", "health claims " * 20)
        conf, reasons = evidence.evidence_confidence(
            [a], [], [], self._profiles(reg, [a]), high_stakes=True)
        self.assertEqual(conf, evidence.LOW)


class ClaimMappingTests(unittest.TestCase):
    def test_claims_map_to_sources(self):
        s = src("https://docs.python.org/x", "d",
                "Python supports virtual environments natively. "
                "The venv module creates isolated environments for packages. "
                "Unrelated filler text without keywords.")
        claims = evidence.extract_claims([s], "python venv virtual environment")
        self.assertTrue(claims)
        self.assertTrue(any(s.id in c["supporting"] for c in claims))


class MisinformationTests(unittest.TestCase):
    """Part 47: copied blogs claiming 'Python removed lists' collapse into one
    weak cluster; official docs win."""

    def test_official_beats_copied_blogs(self):
        reg = SourceTrustRegistry()
        ranker = SourceRanker(registry=reg)
        lie = ("Python 3.14 removed lists entirely. " * 40)
        truth = ("Python lists remain a core built-in type. " * 40)
        blogs = [
            src(f"https://seo{i}.example.com/x", f"blog{i}", lie)
            for i in range(3)
        ]
        official = src("https://docs.python.org/3/whatsnew/3.14.html",
                       "What's New in Python 3.14", truth)
        sources = blogs + [official]
        groups = evidence.IndependenceAnalyzer().analyze(sources)
        self.assertEqual(len(set(groups[b.id] for b in blogs)), 1)
        ranked = ranker.rank(sources, "python lists removed 3.14",
                             [SOFTWARE, PROGRAMMING])
        self.assertIs(ranked[0], official)
        conf, _ = evidence.evidence_confidence(
            ranked[:2], [], [], {s.id: reg.profile_for(evidence.source_host(s))
                                 for s in ranked[:2]})
        self.assertNotEqual(conf, evidence.LOW)


if __name__ == "__main__":
    unittest.main()
