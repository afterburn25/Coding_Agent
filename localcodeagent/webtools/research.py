from __future__ import annotations

import html
import json
import re
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "svg"}:
            self.skip += 1
        if tag in {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "svg"} and self.skip:
            self.skip -= 1
        if tag in {"p", "div", "li", "h1", "h2", "h3", "h4", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)

    def text(self) -> str:
        raw = html.unescape("".join(self.parts))
        raw = re.sub(r"[ \t]+", " ", raw)
        raw = re.sub(r"\n\s*\n+", "\n\n", raw)
        return raw.strip()


class _DDGParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[dict[str, str]] = []
        self._href = ""
        self._capture = False
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "")
        if tag == "a" and "result__a" in classes:
            self._href = attrs.get("href", "")
            self._capture = True
            self._text = []

    def handle_data(self, data):
        if self._capture:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._capture:
            title = html.unescape("".join(self._text)).strip()
            href = self._href
            parsed = urllib.parse.urlparse(href)
            query = urllib.parse.parse_qs(parsed.query)
            if "uddg" in query:
                href = query["uddg"][0]
            if title and href:
                self.rows.append({"title": title, "url": href})
            self._capture = False


class WebResearchClient:
    def __init__(self, *, user_agent: str = "LocalCodeAgent/0.4", timeout: float = 15.0, max_bytes: int = 2_000_000) -> None:
        self.user_agent = user_agent
        self.timeout = timeout
        self.max_bytes = max_bytes

    def _request(self, url: str) -> tuple[bytes, str]:
        req = urllib.request.Request(url, headers={"User-Agent": self.user_agent, "Accept": "text/html,application/json,text/plain,*/*"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = resp.read(self.max_bytes + 1)
            if len(data) > self.max_bytes:
                data = data[: self.max_bytes]
            return data, resp.headers.get_content_type()

    def search(self, query: str, *, count: int = 8) -> list[dict[str, str]]:
        url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
        data, _ = self._request(url)
        parser = _DDGParser()
        parser.feed(data.decode("utf-8", errors="replace"))
        return parser.rows[: max(1, min(count, 20))]

    def fetch(self, url: str, *, max_chars: int = 30000) -> dict[str, Any]:
        data, content_type = self._request(url)
        if "json" in content_type:
            text = json.dumps(json.loads(data.decode("utf-8", errors="replace")), indent=2)
        elif content_type.startswith("text/") or "html" in content_type:
            raw = data.decode("utf-8", errors="replace")
            if "html" in content_type or "<html" in raw[:1000].lower():
                parser = _TextExtractor()
                parser.feed(raw)
                text = parser.text()
            else:
                text = raw
        else:
            return {"url": url, "content_type": content_type, "binary": True, "bytes": len(data)}
        return {"url": url, "content_type": content_type, "text": text[:max_chars], "truncated": len(text) > max_chars}
