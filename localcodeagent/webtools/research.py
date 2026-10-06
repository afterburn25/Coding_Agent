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


class _BingParser(HTMLParser):
    """Bing organic results — ``<li class="b_algo">`` blocks whose first
    ``<h2><a>`` is the result link. Ad blocks (``b_ad``) are skipped by
    construction since they never carry the ``b_algo`` class."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[dict[str, str]] = []
        self._in_algo = False
        self._in_h2 = False
        self._href = ""
        self._capture = False
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "li" and "b_algo" in a.get("class", ""):
            self._in_algo = True
        elif tag == "h2" and self._in_algo:
            self._in_h2 = True
        elif (tag == "a" and self._in_algo and self._in_h2
              and not self._capture):
            self._href = a.get("href", "")
            self._capture = True
            self._text = []

    def handle_data(self, data):
        if self._capture:
            self._text.append(data)

    @staticmethod
    def _unwrap(href: str) -> str:
        """Bing wraps result URLs in ``bing.com/ck/a?...&u=a1<base64url>``.
        Domain trust ranking needs the real destination, not the tracker."""
        if "bing.com/ck/a" not in href:
            return href
        try:
            query = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
            enc = (query.get("u") or [""])[0]
            if enc.startswith("a1"):
                import base64
                decoded = base64.urlsafe_b64decode(
                    enc[2:] + "=" * (-len(enc[2:]) % 4))
                target = decoded.decode("utf-8", errors="replace")
                if target.startswith("http"):
                    return target
        except Exception:
            pass
        return href

    def handle_endtag(self, tag):
        if tag == "a" and self._capture:
            title = html.unescape("".join(self._text)).strip()
            href = self._unwrap(self._href)
            if title and href.startswith("http"):
                self.rows.append({"title": title, "url": href})
            self._capture = False
        elif tag == "h2":
            self._in_h2 = False
        elif tag == "li" and self._in_algo:
            self._in_algo = False


class WebResearchClient:
    #: Search backends tried in order — DuckDuckGo html first (legacy
    #: default), Bing as resilience when a network blocks DDG.
    DEFAULT_BACKENDS = ("ddg", "bing")

    def __init__(self, *, user_agent: str = "LocalCodeAgent/0.4", timeout: float = 15.0, max_bytes: int = 2_000_000, search_backends: tuple[str, ...] | None = None) -> None:
        self.user_agent = user_agent
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.search_backends = tuple(search_backends or self.DEFAULT_BACKENDS)
        #: Which backend produced the last successful search — observability.
        self.last_backend = ""

    def _request(self, url: str) -> tuple[bytes, str]:
        req = urllib.request.Request(url, headers={"User-Agent": self.user_agent, "Accept": "text/html,application/json,text/plain,*/*"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = resp.read(self.max_bytes + 1)
            if len(data) > self.max_bytes:
                data = data[: self.max_bytes]
            return data, resp.headers.get_content_type()

    def _search_backend(self, backend: str, query: str, *, count: int) -> list[dict[str, str]]:
        if backend == "bing":
            url = "https://www.bing.com/search?" + urllib.parse.urlencode(
                {"q": query, "count": max(count, 10)})
            parser: HTMLParser = _BingParser()
        else:
            url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
            parser = _DDGParser()
        data, _ = self._request(url)
        parser.feed(data.decode("utf-8", errors="replace"))
        return parser.rows  # type: ignore[attr-defined]

    def search(self, query: str, *, count: int = 8) -> list[dict[str, str]]:
        limit = max(1, min(count, 20))
        errors: list[str] = []
        for backend in self.search_backends:
            try:
                rows = self._search_backend(backend, query, count=limit)
            except Exception as exc:
                errors.append(f"{backend}: {exc}")
                continue
            if rows:
                self.last_backend = backend
                return rows[:limit]
        if errors:
            raise RuntimeError(
                "all search backends failed: " + "; ".join(errors))
        return []

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
