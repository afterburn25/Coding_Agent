"""Moltbook connector — first-class social integration for agent networks.

Security contract:

* The API key lives in the SecretVault (``moltbook_api_key``) — never in
  Brain, conversation memory, logs, URLs, or committed files.
* ``Authorization: Bearer`` is attached ONLY when the request host
  equals the configured API host; the shared ``_AuthScopedRedirect``
  opener strips it again on any cross-host redirect.
* Every fetched payload is tagged ``UNTRUSTED_EXTERNAL_CONTENT`` — posts
  and comments are data, never instructions.
* Outbound content passes a blocking secret scan before it leaves.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from . import Connector
from ..fsutil import atomic_write_text
from ..social.safety import injection_hits, outbound_scan, tag_untrusted
from ..tools.downloads import _AUTH_SAFE_OPENER, _safe_url

DEFAULT_API_BASE = "https://www.moltbook.com/api/v1"
DEFAULT_SECRET_NAME = "moltbook_api_key"
DEFAULT_AGENT_NAME = "Nexus"
DEFAULT_DESCRIPTION = (
    "Nexus Core — a local-first personal AI workstation. Interested in "
    "agent architecture, memory, verification, and peer learning.")
ACCOUNT_STATES = ("none", "awaiting_owner_verification", "active")


class MoltbookClient:
    """Minimal HTTPS JSON client — auth scoped to the API host only."""

    def __init__(self, base_url: str,
                 api_key: Callable[[], str | None],
                 *, timeout: float = 20.0,
                 opener: Any = None) -> None:
        self.base_url = str(base_url or DEFAULT_API_BASE).rstrip("/")
        self._key = api_key
        self.timeout = float(timeout or 20.0)
        self._opener = opener or _AUTH_SAFE_OPENER
        self._host = urllib.parse.urlsplit(self.base_url).hostname or ""

    def _url(self, path: str, params: dict | None) -> str:
        path = "/" + str(path).lstrip("/")
        url = self.base_url + path
        if params:
            q = urllib.parse.urlencode(
                {k: v for k, v in params.items()
                 if v is not None and v != ""})
            if q:
                url += "?" + q
        return url

    def request(self, method: str, path: str, *,
                params: dict | None = None,
                body: dict | None = None,
                auth: bool = True) -> dict[str, Any]:
        url = self._url(path, params)
        try:
            url = _safe_url(url)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        headers = {"Accept": "application/json",
                   "User-Agent": "NexusCore/0.38 (+moltbook-connector)"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method=method.upper())
        if auth:
            key = self._key() or ""
            req_host = urllib.parse.urlsplit(url).hostname or ""
            # Host-scoped auth — the token goes to the configured Moltbook
            # host and nowhere else, ever.
            if key and req_host.lower() == self._host.lower():
                req.add_header("Authorization", f"Bearer {key}")
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read(1_000_000)
                return self._unwrap(resp.status, raw)
        except urllib.error.HTTPError as exc:
            raw = b""
            try:
                raw = exc.read(500_000)
            except Exception:
                pass
            out = self._unwrap(exc.code, raw)
            if exc.code == 429:
                retry = exc.headers.get("Retry-After") \
                    if exc.headers else None
                out["ok"] = False
                out["error"] = "rate_limited"
                try:
                    out["retry_after"] = int(retry) if retry else 60
                except ValueError:
                    out["retry_after"] = 60
            return out
        except Exception as exc:
            return {"ok": False,
                    "error": f"{type(exc).__name__}: {exc}"}

    def _unwrap(self, status: int, raw: bytes) -> dict[str, Any]:
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {"data": payload}
        # Moltbook envelope: {"success": true, "data": ...} or
        # {"success": false, "error": ..., "hint": ...}
        if "success" in payload or "error" in payload:
            ok = bool(payload.get("success", status < 400)) \
                and status < 400
            out = {"ok": ok, "status": status}
            if isinstance(payload.get("data"), (dict, list)):
                out["data"] = payload["data"]
            else:
                out["data"] = {k: v for k, v in payload.items()
                               if k not in ("success", "error", "hint")}
            if payload.get("error"):
                out["error"] = str(payload["error"])
            if payload.get("hint"):
                out["hint"] = str(payload["hint"])
            return out
        return {"ok": status < 400, "status": status, "data": payload}


class MoltbookConnector(Connector):
    """First-class Moltbook integration.

    ``state_path`` persists non-secret account metadata (registration
    state, claim URL, agent id); the API key itself lives only in the
    vault. ``permission_check`` gets the per-capability permission id —
    reads are ``social.read``, writes are ``social.post``/``react``/
    ``follow``/``account`` so the PermissionManager governs every
    consequential call.
    """

    name = "moltbook"
    capabilities = (
        "onboard", "status", "me", "profile", "update_profile",
        "feed", "get_post", "comments", "search",
        "notifications", "home", "mark_read",
        "post", "comment", "verify",
        "vote", "follow", "unfollow", "subscribe", "unsubscribe",
    )
    permission = "social.read"
    # Moltbook allows 60 reads + 30 writes per minute; stay conservative
    # — this is a shared window across all calls.
    rate_limit_per_min = 20

    _CAP_PERMISSION = {
        "onboard": "social.account",
        "update_profile": "social.account",
        "post": "social.post",
        "comment": "social.post",
        "verify": "social.post",
        "vote": "social.react",
        "follow": "social.follow",
        "unfollow": "social.follow",
        "subscribe": "social.follow",
        "unsubscribe": "social.follow",
    }

    def __init__(self, *, base_url: str = DEFAULT_API_BASE,
                 secret_name: str = DEFAULT_SECRET_NAME,
                 agent_name: str = DEFAULT_AGENT_NAME,
                 description: str = DEFAULT_DESCRIPTION,
                 timeout: float = 20.0,
                 state_path: Path | None = None,
                 permission_check: Callable[[str], str] | None = None,
                 client: MoltbookClient | None = None,
                 redactor: Callable[[str], str] | None = None) -> None:
        self.base_url = str(base_url or DEFAULT_API_BASE).rstrip("/")
        self.secret_name = secret_name or DEFAULT_SECRET_NAME
        self.agent_name = agent_name or DEFAULT_AGENT_NAME
        self.description = description or DEFAULT_DESCRIPTION
        self._vault = None
        self._perm = permission_check or (lambda _p: "allow")
        self._redactor = redactor
        self._state_path = (Path(state_path) if state_path else None)
        self._client = client or MoltbookClient(
            self.base_url, self._api_key, timeout=timeout)
        self._account = self._load_account()

    # -- account state -----------------------------------------------------

    def _load_account(self) -> dict[str, Any]:
        try:
            if self._state_path and self._state_path.exists():
                row = json.loads(self._state_path.read_text(
                    encoding="utf-8"))
                acct = row.get("account")
                if isinstance(acct, dict):
                    return acct
        except Exception:
            pass
        return {"state": "none"}

    def _save_account(self) -> None:
        if not self._state_path:
            return
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(self._state_path, json.dumps(
                {"version": 1, "account": self._account}, indent=2))
        except Exception:
            pass

    def account_state(self) -> str:
        return str(self._account.get("state") or "none")

    def account(self) -> dict[str, Any]:
        """Public account view — claim URL is fine to show (it's the
        owner's link); the API key is never in here."""
        return {k: v for k, v in self._account.items()
                if k != "api_key"}

    def _api_key(self) -> str | None:
        if self._vault is None:
            return None
        try:
            return self._vault.get(self.secret_name)
        except Exception:
            return None

    # -- Connector interface ------------------------------------------------

    def authenticate(self, vault: Any) -> bool:
        self._vault = vault
        return bool(self._api_key())

    def auth_state(self) -> dict[str, Any]:
        return {"authed": bool(self._api_key()), "source": "vault"}

    def health(self) -> dict[str, Any]:
        if not self._api_key():
            return {"ok": True, "state": "no_credentials",
                    "detail": "no Moltbook API key in vault"}
        out = self._client.request("GET", "/agents/status")
        if not out.get("ok"):
            return {"ok": False, "error": out.get("error", "unreachable")}
        status = str((out.get("data") or {}).get("status") or "")
        if status == "claimed" and self.account_state() != "active":
            self._account["state"] = "active"
            self._account["verified_at"] = time.time()
            self._save_account()
        return {"ok": True, "state": status or "unknown",
                "account": self.account_state()}

    def reconnect(self) -> bool:
        return True

    # -- capability dispatch --------------------------------------------------

    def _check_perm(self, capability: str) -> dict[str, Any] | None:
        perm = self._CAP_PERMISSION.get(capability, "social.read")
        verdict = self._perm(perm)
        if verdict != "allow":
            return {"ok": False, "permission": perm, "verdict": verdict,
                    "needs_approval": verdict == "ask",
                    "error": f"permission denied: {perm}"}
        return None

    def _scan_outbound(self, *texts: str) -> dict[str, Any] | None:
        hits = outbound_scan(" ".join(t for t in texts if t),
                             redactor=self._redactor)
        if hits:
            return {"ok": False, "error": "outbound_blocked",
                    "detail": "content failed the secret-exfiltration "
                              "scan and was not sent",
                    "violations": hits}
        return None

    @staticmethod
    def _socialize(out: dict[str, Any]) -> dict[str, Any]:
        """Mark fetched payloads untrusted and flag content that looks
        like it is trying to issue the agent instructions."""
        data = out.get("data")
        flags: list[str] = []
        def _scan(node: Any) -> None:
            if isinstance(node, dict):
                for v in node.values():
                    _scan(v)
            elif isinstance(node, list):
                for v in node:
                    _scan(v)
            elif isinstance(node, str):
                flags.extend(injection_hits(node))
        _scan(data)
        if flags:
            out["injection_flags"] = sorted(set(flags))[:4]
        out["data"] = tag_untrusted(data)
        return out

    def call(self, capability: str, **params: Any) -> dict[str, Any]:
        if capability not in self.capabilities:
            return {"ok": False,
                    "error": f"unknown capability '{capability}'"}
        denied = self._check_perm(capability)
        if denied is not None:
            return denied
        handler = getattr(self, f"_cap_{capability}", None)
        if handler is None:
            return {"ok": False,
                    "error": f"capability '{capability}' not implemented"}
        return handler(**params)

    # -- account / onboarding -------------------------------------------------

    def _cap_onboard(self, name: str = "", description: str = "",
                     **_: Any) -> dict[str, Any]:
        """Register the agent identity. Returns the owner claim link —
        account state becomes ``awaiting_owner_verification`` until the
        human claims it."""
        state = self.account_state()
        if state == "active":
            return {"ok": True, "state": "active",
                    "detail": "account already verified",
                    "account": self.account()}
        if self._api_key() and state == "awaiting_owner_verification":
            # Re-check before re-registering — the owner may have
            # completed the claim between attempts.
            check = self._cap_status()
            return {"ok": True, "state": self.account_state(),
                    "claim_url": self._account.get("claim_url", ""),
                    "status": check.get("data"),
                    "account": self.account()}
        name = str(name or self.agent_name).strip() or self.agent_name
        description = str(description or self.description).strip()
        blocked = self._scan_outbound(name, description)
        if blocked is not None:
            return blocked
        out = self._client.request(
            "POST", "/agents/register", auth=False,
            body={"name": name, "description": description})
        if not out.get("ok"):
            return out
        agent = (out.get("data") or {}).get("agent") \
            or out.get("data") or {}
        api_key = str(agent.get("api_key") or "")
        claim_url = str(agent.get("claim_url") or "")
        if not api_key:
            return {"ok": False,
                    "error": "registration returned no api_key"}
        if self._vault is None:
            return {"ok": False,
                    "error": "no vault available to hold the api_key"}
        try:
            self._vault.set(self.secret_name, api_key,
                            description="Moltbook agent API key")
        except Exception as exc:
            return {"ok": False,
                    "error": f"vault store failed: {type(exc).__name__}"}
        self._account = {
            "state": "awaiting_owner_verification",
            "name": name,
            "description": description,
            "claim_url": claim_url,
            "verification_code": str(agent.get("verification_code") or ""),
            "registered_at": time.time(),
        }
        self._save_account()
        return {"ok": True, "state": "awaiting_owner_verification",
                "claim_url": claim_url,
                "verification_code": self._account["verification_code"],
                "account": self.account()}

    def _cap_status(self, **_: Any) -> dict[str, Any]:
        """Poll claim state; flips account to ``active`` once the owner
        has claimed the agent."""
        if not self._api_key():
            return {"ok": True, "state": self.account_state(),
                    "detail": "no credentials — registration required"}
        out = self._client.request("GET", "/agents/status")
        if out.get("ok"):
            status = str((out.get("data") or {}).get("status") or "")
            if status == "claimed" \
                    and self.account_state() != "active":
                self._account["state"] = "active"
                self._account["verified_at"] = time.time()
                self._save_account()
            out["state"] = self.account_state()
        return out

    def _cap_me(self, **_: Any) -> dict[str, Any]:
        out = self._client.request("GET", "/agents/me")
        return self._socialize(out) if out.get("ok") else out

    def _cap_profile(self, name: str = "", **_: Any) -> dict[str, Any]:
        out = self._client.request(
            "GET", "/agents/profile",
            params={"name": str(name or "")})
        return self._socialize(out) if out.get("ok") else out

    def _cap_update_profile(self, description: str = "",
                            metadata: dict | None = None,
                            **_: Any) -> dict[str, Any]:
        blocked = self._scan_outbound(description)
        if blocked is not None:
            return blocked
        body: dict[str, Any] = {}
        if description:
            body["description"] = description
        if isinstance(metadata, dict):
            body["metadata"] = metadata
        return self._client.request("PATCH", "/agents/me", body=body)

    # -- reads -----------------------------------------------------------------

    def _cap_feed(self, sort: str = "hot", limit: int = 25,
                  cursor: str = "", submolt: str = "",
                  filter: str = "", **_: Any) -> dict[str, Any]:
        path = "/feed" if filter else "/posts"
        out = self._client.request(
            "GET", path,
            params={"sort": sort, "limit": min(int(limit or 25), 100),
                    "cursor": cursor or None,
                    "submolt": submolt or None,
                    "filter": filter or None})
        return self._socialize(out) if out.get("ok") else out

    def _cap_get_post(self, post_id: str = "", **_: Any) -> dict[str, Any]:
        pid = urllib.parse.quote(str(post_id or ""), safe="")
        out = self._client.request("GET", f"/posts/{pid}")
        return self._socialize(out) if out.get("ok") else out

    def _cap_comments(self, post_id: str = "", sort: str = "best",
                      limit: int = 35, cursor: str = "",
                      **_: Any) -> dict[str, Any]:
        pid = urllib.parse.quote(str(post_id or ""), safe="")
        out = self._client.request(
            "GET", f"/posts/{pid}/comments",
            params={"sort": sort, "limit": min(int(limit or 35), 100),
                    "cursor": cursor or None})
        return self._socialize(out) if out.get("ok") else out

    def _cap_search(self, q: str = "", type: str = "",
                    limit: int = 20, **_: Any) -> dict[str, Any]:
        out = self._client.request(
            "GET", "/search",
            params={"q": str(q or ""), "type": type or None,
                    "limit": min(int(limit or 20), 50)})
        return self._socialize(out) if out.get("ok") else out

    def _cap_notifications(self, **_: Any) -> dict[str, Any]:
        out = self._client.request("GET", "/notifications")
        return self._socialize(out) if out.get("ok") else out

    def _cap_home(self, **_: Any) -> dict[str, Any]:
        out = self._client.request("GET", "/home")
        return self._socialize(out) if out.get("ok") else out

    def _cap_mark_read(self, post_id: str = "", all: bool = False,
                       **_: Any) -> dict[str, Any]:
        if all:
            return self._client.request(
                "POST", "/notifications/read-all")
        pid = urllib.parse.quote(str(post_id or ""), safe="")
        return self._client.request(
            "POST", f"/notifications/read-by-post/{pid}")

    # -- writes ------------------------------------------------------------------

    def _cap_post(self, submolt_name: str = "general", title: str = "",
                  content: str = "", url: str = "", type: str = "",
                  **_: Any) -> dict[str, Any]:
        blocked = self._scan_outbound(title, content, url)
        if blocked is not None:
            return blocked
        body: dict[str, Any] = {
            "submolt_name": str(submolt_name or "general"),
            "title": str(title or "")[:300]}
        if content:
            body["content"] = str(content)[:40000]
        if url:
            body["url"] = str(url)
        if type:
            body["type"] = str(type)
        return self._client.request("POST", "/posts", body=body)

    def _cap_comment(self, post_id: str = "", content: str = "",
                     parent_id: str = "", **_: Any) -> dict[str, Any]:
        blocked = self._scan_outbound(content)
        if blocked is not None:
            return blocked
        body: dict[str, Any] = {"content": str(content or "")[:40000]}
        if parent_id:
            body["parent_id"] = str(parent_id)
        pid = urllib.parse.quote(str(post_id or ""), safe="")
        return self._client.request(
            "POST", f"/posts/{pid}/comments", body=body)

    def _cap_verify(self, verification_code: str = "", answer: str = "",
                    **_: Any) -> dict[str, Any]:
        return self._client.request(
            "POST", "/verify",
            body={"verification_code": str(verification_code or ""),
                  "answer": str(answer or "")})

    def _cap_vote(self, post_id: str = "", comment_id: str = "",
                  direction: str = "up", **_: Any) -> dict[str, Any]:
        if direction not in ("up", "down"):
            return {"ok": False, "error": "direction must be up|down"}
        if comment_id:
            cid = urllib.parse.quote(str(comment_id), safe="")
            return self._client.request(
                "POST", f"/comments/{cid}/{direction}vote")
        pid = urllib.parse.quote(str(post_id or ""), safe="")
        return self._client.request(
            "POST", f"/posts/{pid}/{direction}vote")

    def _cap_follow(self, name: str = "", **_: Any) -> dict[str, Any]:
        n = urllib.parse.quote(str(name or ""), safe="")
        return self._client.request("POST", f"/agents/{n}/follow")

    def _cap_unfollow(self, name: str = "", **_: Any) -> dict[str, Any]:
        n = urllib.parse.quote(str(name or ""), safe="")
        return self._client.request("DELETE", f"/agents/{n}/follow")

    def _cap_subscribe(self, submolt: str = "", **_: Any) -> dict[str, Any]:
        n = urllib.parse.quote(str(submolt or ""), safe="")
        return self._client.request("POST", f"/submolts/{n}/subscribe")

    def _cap_unsubscribe(self, submolt: str = "",
                         **_: Any) -> dict[str, Any]:
        n = urllib.parse.quote(str(submolt or ""), safe="")
        return self._client.request("DELETE", f"/submolts/{n}/subscribe")
