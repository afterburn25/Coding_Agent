from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


class GitHubApiError(RuntimeError):
    def __init__(self, message: str, *, status: int = 0, rate_remaining: int | None = None, rate_reset: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.rate_remaining = rate_remaining
        self.rate_reset = rate_reset


@dataclass(slots=True)
class GitHubApiResponse:
    data: Any
    rate_limit: int | None = None
    rate_remaining: int | None = None
    rate_reset: int | None = None
    rate_resource: str = ""


class GitHubApiClient:
    """Small dependency-free GitHub REST client for public research.

    The token is read from an environment variable by default and is never stored
    in the project configuration or returned in metadata/logs.
    """

    def __init__(
        self,
        *,
        base_url: str = "https://api.github.com",
        api_version: str = "2026-03-10",
        token_env: str = "GITHUB_TOKEN",
        token: str = "",
        timeout: int = 15,
        opener=None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_version = api_version
        self.token_env = token_env
        self._token = token or os.environ.get(token_env, "")
        self.timeout = max(3, int(timeout))
        self._opener = opener

    @property
    def authenticated(self) -> bool:
        return bool(self._token)

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": self.api_version,
            "User-Agent": "Local-Code-Agent-Research/0.5",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    @staticmethod
    def _int_header(headers: Any, name: str) -> int | None:
        try:
            value = headers.get(name)
            return int(value) if value not in {None, ""} else None
        except Exception:
            return None

    def request(self, path: str, *, params: dict[str, Any] | None = None) -> GitHubApiResponse:
        if not path.startswith("/"):
            path = "/" + path
        query = urlencode({k: v for k, v in (params or {}).items() if v not in {None, ""}})
        url = self.base_url + path + ("?" + query if query else "")
        request = Request(url, headers=self._headers(), method="GET")
        try:
            if self._opener is None:
                response = urlopen(request, timeout=self.timeout)
            else:
                response = self._opener(request, self.timeout)
            with response as handle:
                raw = handle.read()
                data = json.loads(raw.decode("utf-8")) if raw else None
                headers = handle.headers
                return GitHubApiResponse(
                    data=data,
                    rate_limit=self._int_header(headers, "X-RateLimit-Limit"),
                    rate_remaining=self._int_header(headers, "X-RateLimit-Remaining"),
                    rate_reset=self._int_header(headers, "X-RateLimit-Reset"),
                    rate_resource=str(headers.get("X-RateLimit-Resource") or ""),
                )
        except HTTPError as exc:
            remaining = self._int_header(exc.headers, "X-RateLimit-Remaining")
            reset = self._int_header(exc.headers, "X-RateLimit-Reset")
            try:
                payload = json.loads(exc.read().decode("utf-8", errors="replace"))
                detail = str(payload.get("message") or exc.reason)
            except Exception:
                detail = str(exc.reason)
            if exc.code in {403, 429} and remaining == 0:
                detail = f"GitHub API rate limit exhausted; reset={reset or 'unknown'}"
            raise GitHubApiError(detail, status=exc.code, rate_remaining=remaining, rate_reset=reset) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise GitHubApiError(f"GitHub API unavailable: {exc}") from exc

    @staticmethod
    def _rate(response: GitHubApiResponse) -> dict[str, Any]:
        return {
            "limit": response.rate_limit,
            "remaining": response.rate_remaining,
            "reset": response.rate_reset,
            "resource": response.rate_resource,
        }

    def search_repositories(self, query: str, *, limit: int = 8) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        response = self.request("/search/repositories", params={"q": query, "per_page": min(100, max(1, limit)), "sort": "updated"})
        return list((response.data or {}).get("items") or [])[:limit], self._rate(response)

    def search_issues(self, query: str, *, repo: str = "", limit: int = 8) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        q = query.strip()
        if repo:
            q = f"{q} repo:{repo}".strip()
        response = self.request("/search/issues", params={"q": q, "per_page": min(100, max(1, limit)), "sort": "updated"})
        return list((response.data or {}).get("items") or [])[:limit], self._rate(response)

    def search_code(self, query: str, *, repo: str = "", limit: int = 8) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        q = query.strip()
        if repo:
            q = f"{q} repo:{repo}".strip()
        response = self.request("/search/code", params={"q": q, "per_page": min(100, max(1, limit))})
        return list((response.data or {}).get("items") or [])[:limit], self._rate(response)

    def releases(self, repo: str, *, limit: int = 8) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if "/" not in repo:
            raise ValueError("repo must use owner/name form")
        owner, name = repo.split("/", 1)
        response = self.request(
            f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}/releases",
            params={"per_page": min(100, max(1, limit))},
        )
        return list(response.data or [])[:limit], self._rate(response)
