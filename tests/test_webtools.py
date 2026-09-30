from __future__ import annotations

import unittest

from localcodeagent.webtools.browser import BrowserRunner
from localcodeagent.webtools.research import WebResearchClient


class WebResearchTests(unittest.TestCase):
    def test_search_parses_results(self):
        client = WebResearchClient()
        html = b'''<html><a class="result__a" href="https://example.com/docs">Example Docs</a></html>'''
        client._request = lambda url: (html, "text/html")  # type: ignore[method-assign]
        rows = client.search("example")
        self.assertEqual(rows[0]["title"], "Example Docs")
        self.assertEqual(rows[0]["url"], "https://example.com/docs")

    def test_fetch_extracts_readable_text(self):
        client = WebResearchClient()
        html = b'''<html><style>bad</style><body><h1>Hello</h1><p>Useful text</p><script>bad()</script></body></html>'''
        client._request = lambda url: (html, "text/html")  # type: ignore[method-assign]
        row = client.fetch("https://example.com")
        self.assertIn("Hello", row["text"])
        self.assertIn("Useful text", row["text"])
        self.assertNotIn("bad()", row["text"])

    def test_browser_dependency_probe_is_boolean(self):
        self.assertIsInstance(BrowserRunner.available(), bool)


if __name__ == "__main__":
    unittest.main()
