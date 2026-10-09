"""SocialService — the façade tying connector + drives + stores together.

Owns: MoltbookConnector registration, the Social/Epistemic drives, the
persistent stores, the onboarding flow, and the scheduler heartbeat.
Chat/API/tick entry points all funnel through here so permission,
ledger, and injection rules are applied once, consistently.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Callable

from .drive import EpistemicDrive, SocialDrive
from .safety import wrap_for_model
from .store import SocialStore, overlap, terms

# A join request: "join moltbook", "sign up for moltbook",
# "create an account on moltbook", "register me on moltbook".
_JOIN_RE = re.compile(
    r"\b(?:join|sign\s*up\s+for|sign\s+into|register\s+(?:me\s+)?(?:on|for|"
    r"with)|create\s+(?:an?\s+)?(?:account|profile)\s+on|"
    r"get\s+on(?:to)?|make\s+(?:an?\s+)?account\s+on)\s+"
    r"([a-z0-9][a-z0-9 ._'-]{0,40})",
    re.IGNORECASE)


class SocialService:
    def __init__(self, *, config: Any = None, vault: Any = None,
                 permission_check: Callable[[str], str] | None = None,
                 store_root: Path | None = None,
                 event_sink: Callable[[str, dict], None] | None = None,
                 activity: Callable[..., Any] | None = None) -> None:
        self._config = config
        self._vault = vault
        self._perm = permission_check or (lambda _p: "allow")
        self._event = event_sink or (lambda _e, _p: None)
        self._activity = activity
        self.store = SocialStore(store_root or Path("data/social"))
        self.drive = SocialDrive(
            self.store,
            level=lambda: str(getattr(config, "social_level",
                                      "assisted") or "assisted"),
            permission_check=self._perm,
            event_sink=self._event)
        self.epistemic = EpistemicDrive(self.store)
        self._connector = None
        self._connectors = None

    # -- wiring ------------------------------------------------------------

    def attach(self, connectors: Any, connector: Any) -> None:
        """Called by AppState after building the connector — the service
        registers it and keeps a direct handle for lane calls that must
        bypass the registry's own rate window accounting."""
        self._connectors = connectors
        self._connector = connector

    def connector(self, name: str = "moltbook") -> Any:
        return self._connector if name == "moltbook" else None

    # -- capability truth ----------------------------------------------------

    def connector_state(self, name: str = "moltbook") -> dict[str, Any]:
        """Live state used by capability probes and 'can you join X'
        answers — never stale lore."""
        cfg = self._config
        enabled = bool(getattr(cfg, "moltbook_enabled", True))
        conn = self.connector(name)
        if not enabled or conn is None:
            return {"service": name, "state": "disabled"}
        acct = conn.account_state()
        authed = bool(conn.auth_state().get("authed"))
        gate = self.drive.gate("read")
        return {
            "service": name,
            "enabled": True,
            "account": acct,
            "authed": authed,
            "level": self.drive.level(),
            "read_allowed": gate["allowed"] or not gate["level_blocked"],
            "state": ("active" if acct == "active"
                      else "awaiting_owner_verification"
                      if acct == "awaiting_owner_verification"
                      else "available"),
        }

    def capability_text(self, name: str = "moltbook") -> str:
        """Human answer for 'can you join/use <service>' — grounded in
        live connector state."""
        st = self.connector_state(name)
        if st["state"] == "disabled":
            return (f"The {name} connector is disabled — enable it in "
                    "connector settings first.")
        if st["state"] == "active":
            return (f"Yes — my {name} identity is registered and "
                    "verified. I can read and, within the social "
                    "permissions you set, participate.")
        if st["state"] == "awaiting_owner_verification":
            acct = self.connector(name).account()
            link = acct.get("claim_url", "")
            return (f"Almost — I registered a {name} identity and it's "
                    "waiting on your ownership claim"
                    + (f": {link}" if link else "."))
        return (f"I can access {name} — browser and network paths are "
                "healthy. I'd need to register an agent identity and "
                "have you claim it before I could participate. Say the "
                "word and I'll start.")

    def perm_level(self, service: str, action: str) -> str:
        """Effective permission for a connector action — 'allow', 'ask',
        'deny', 'creator'. Connector's own per-capability map wins."""
        conn = self.connector(service)
        key = "social.read"
        if conn is not None:
            key = getattr(conn, "_CAP_PERMISSION", {}).get(action) or \
                getattr(conn, "permission", "social.read")
        try:
            return str(self._perm(key))
        except Exception:
            return "ask"

    # -- join / onboarding ---------------------------------------------------------

    def resolve_service(self, text: str) -> str | None:
        """'join X' → a registered connector name, or None. Generic —
        any connector exposing 'onboard' qualifies, Moltbook is just the
        first one."""
        m = _JOIN_RE.search(str(text or ""))
        if not m:
            return None
        target = m.group(1).strip().lower().rstrip(".!?")
        # Strip trailing filler words ("for me", "please", politeness).
        target = re.split(r"\s+(?:please|for\s+me|now|today)\b",
                          target)[0].strip()
        if self._connectors is None:
            return None
        for row in (self._connectors.status() or []):
            name = str(row.get("name") or "")
            if not name:
                continue
            conn = (self._connectors.connectors.get(name) or {}
                    ).get("conn")
            if conn is None or "onboard" not in getattr(
                    conn, "capabilities", ()):
                continue
            if name in target or target in name or \
                    target.startswith(name):
                return name
        return None

    def join(self, service: str = "moltbook", *,
             name: str = "", description: str = "",
             approved: bool = False) -> dict[str, Any]:
        """The whole onboarding flow — inspect state, register if needed,
        return the claim link. Caller handles the permission gate before
        invoking this; ``approved`` carries a granted user approval so the
        connector's own gate treats it as the authorization (one-shot)."""
        conn = self.connector(service)
        if conn is None:
            return {"ok": False, "error": f"no connector for '{service}'"}
        out = conn.call("onboard", name=name, description=description,
                        _approved=approved)
        self.drive.record("onboard", ref=service,
                          score=1.0, reason="user-requested join")
        self._event("social", {"event": "onboard", "service": service,
                               "state": out.get("state", ""),
                               "ok": bool(out.get("ok"))})
        if self._activity is not None:
            try:
                self._activity(
                    "social",
                    f"Moltbook onboarding",
                    f"state: {out.get('state') or out.get('error')}")
            except Exception:
                pass
        return out

    def check_verification(self, service: str = "moltbook"
                           ) -> dict[str, Any]:
        """Poll claim status — flips awaiting → active."""
        conn = self.connector(service)
        if conn is None:
            return {"ok": False, "error": "no connector"}
        out = conn.call("status")
        state = conn.account_state()
        self._event("social", {"event": "verification_check",
                               "service": service, "state": state})
        return {"ok": bool(out.get("ok", True)),
                "state": state, "data": out.get("data")}

    # -- heartbeat -------------------------------------------------------------------

    def heartbeat(self) -> dict[str, Any]:
        """One social check-in — notifications, followed threads,
        learning opportunities. What it does is bounded by the level
        and permissions; it never posts just because time passed."""
        if not self.drive.allows("read"):
            return {"ok": True, "skipped": "social level is off"}
        conn = self.connector("moltbook")
        if conn is None or conn.account_state() != "active":
            return {"ok": True, "skipped": "no active account"}
        summary: dict[str, Any] = {"ok": True, "actions": [],
                                   "skipped": ""}
        try:
            home = conn.call("home")
            data = home.get("data") or {}
            notifs = self._extract_notifications(data)
            for n in notifs[:10]:
                self._handle_notification(n, summary)
            feed = conn.call("feed", sort="new", limit=15)
            posts = self._extract_posts(feed.get("data"))
            self._scan_feed(posts, summary)
            self.drive.heartbeat_done()
        except Exception as exc:
            summary["ok"] = False
            summary["error"] = f"{type(exc).__name__}: {exc}"
        return summary

    def _extract_notifications(self, data: dict[str, Any]
                               ) -> list[dict[str, Any]]:
        if not isinstance(data, dict):
            return []
        for key in ("notifications", "unread", "items"):
            if isinstance(data.get(key), list):
                return [x for x in data[key] if isinstance(x, dict)]
        return []

    def _extract_posts(self, data: Any) -> list[dict[str, Any]]:
        if isinstance(data, dict):
            for key in ("posts", "items", "results", "data"):
                if isinstance(data.get(key), list):
                    return [x for x in data[key] if isinstance(x, dict)]
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        return []

    def _post_author(self, post: dict[str, Any]) -> str:
        for k in ("author", "agent", "agent_name", "owner", "poster"):
            v = post.get(k)
            if isinstance(v, dict):
                v = v.get("name") or v.get("display_name")
            if v:
                return str(v)
        return ""

    def _handle_notification(self, n: dict[str, Any],
                             summary: dict[str, Any]) -> None:
        """A reply/mention aimed at Nexus — record the peer interaction
        and bump reply_priority so the next participation eval knows."""
        kind = str(n.get("type") or n.get("kind") or "")
        author = self._post_author(n) or str(n.get("from") or "")
        ref = str(n.get("post_id") or n.get("id") or "")
        if author:
            self.store.record_interaction(
                author, kind or "notification", ref=ref,
                summary=str(n.get("title") or n.get("text") or "")[:200])
        if kind in ("reply", "comment", "mention"):
            m = self.store.motivation()
            m["reply_priority"] = min(1.0,
                                      float(m.get("reply_priority", 0))
                                      + 0.25)
            self.store.drive.save()
            summary["actions"].append(
                {"kind": "reply_pending", "peer": author, "ref": ref})
            # External-wait edge — missions parked on a peer answer get
            # the trigger and re-evaluate; nothing blocks waiting on
            # the remote side.
            self._event("social", {"event": "peer_reply",
                                   "peer": author, "ref": ref,
                                   "kind": kind})

    def _scan_feed(self, posts: list[dict[str, Any]],
                   summary: dict[str, Any]) -> None:
        interests = set()
        for t in self.drive.interests():
            interests |= terms(t)
        for item in self.store.backlog_open(limit=10):
            interests |= terms(item.get("topic", ""))
        for post in posts[:15]:
            text = " ".join(str(post.get(k) or "")
                            for k in ("title", "content", "body"))
            pt = terms(text)
            relevance = overlap(pt, interests) if interests else 0.0
            if relevance < 0.4:
                continue
            author = self._post_author(post)
            pid = str(post.get("id") or post.get("post_id") or "")
            title = str(post.get("title") or text[:80])
            if author:
                self.store.record_interaction(
                    author, "seen_post", ref=pid, summary=title,
                    topics=list(pt)[:4])
            decision = self.drive.evaluate_participation({
                "kind": "comment", "topic": title, "peer": author,
                "relevance": relevance, "ref": pid, "thread": pid,
                "learning_value": relevance})
            if decision["action"] == "follow":
                self.store.follow_thread(pid, title, relevance,
                                         reason="matches current "
                                                "learning interests")
                summary["actions"].append(
                    {"kind": "follow_thread", "ref": pid,
                     "topic": title[:80]})
            elif decision["act"]:
                summary["actions"].append(
                    {"kind": "candidate_reply", "ref": pid,
                     "topic": title[:80], "score": decision["score"]})
            # Knowledge claims inside relevant posts enter at 'heard'.
            claims = self._extract_claims(post)
            for c in claims[:2]:
                row = self.epistemic.hear_claim(
                    c, source_peer=author, source_ref=pid,
                    domain=self._domain_of(text))
                if self._looks_testable(c):
                    self.epistemic.note_gap(
                        c[:200], "peer claim — verify locally",
                        source=f"moltbook:{author}", urgency=0.4,
                        kind="claim_verification",
                        candidate_peers=[author] if author else [],
                        verification_plan="local benchmark or "
                                          "documentation check")

    _CLAIM_RE = re.compile(
        r"[^.!?\n]{10,300}?\b(?:reduces?|improves?|speeds?\s+up|"
        r"fixes?|solves?|increases?|cuts|saves?|avoids?|doubles?|halves?|"
        r"works\s+better|is\s+faster|best\s+way|should\s+use)\b"
        r"[^.!?\n]{0,200}", re.IGNORECASE)

    def _extract_claims(self, post: dict[str, Any]) -> list[str]:
        text = " ".join(str(post.get(k) or "")
                        for k in ("title", "content", "body"))
        return [m.group(0).strip() for m in
                self._CLAIM_RE.finditer(text)][:3]

    @staticmethod
    def _looks_testable(claim: str) -> bool:
        return bool(re.search(
            r"\b(?:\d+\s?%|benchmark|latency|memory|vram|tokens/s|"
            r"setting|flag|parameter|config|version)\b",
            claim, re.IGNORECASE))

    _DOMAINS = ("memory", "context", "vram", "gpu", "llm", "model",
                "installer", "windows", "ui", "voice", "agent",
                "benchmark", "security", "network", "database")

    @classmethod
    def _domain_of(cls, text: str) -> str:
        low = str(text or "").lower()
        for d in cls._DOMAINS:
            if re.search(rf"\b{d}\b", low):
                return d
        return "general"

    # -- reporting -----------------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        st = self.connector_state()
        out = self.store.summary()
        top = (self.store.backlog_open(limit=1) or [{}])[0]
        out.update({
            "service": "moltbook",
            "enabled": st.get("enabled", False),
            "account": st.get("account", "none"),
            "level": self.drive.level(),
            "interests": self.drive.interests(),
            "peer_count": out.pop("peers", 0),
            "claim_count": out.pop("claims", 0),
            "thread_count": out.pop("followed", 0),
            "threads": self.store.followed(),
            "compulsion": {"score": self.epistemic.compulsion(),
                           "topic": str(top.get("topic") or "")},
            "claim_url": (self.connector().account().get("claim_url", "")
                          if self.connector() else ""),
        })
        return out

    def context_note(self) -> str:
        """Short state line for prompts/inspector — active social state."""
        st = self.connector_state()
        if not st.get("enabled"):
            return ""
        bits = [f"Moltbook: {st.get('account', 'none')}"]
        m = self.store.motivation()
        if m.get("reply_priority", 0) >= 0.5:
            bits.append("unanswered replies pending")
        open_items = self.store.backlog_open(limit=3)
        if open_items:
            bits.append("open learning questions: " +
                        "; ".join(i["topic"][:40]
                                  for i in open_items[:2]))
        return " | ".join(bits)


def render_post_for_model(post: dict[str, Any]) -> str:
    """Untrusted render helper for social content entering prompts."""
    return wrap_for_model(
        str(post.get("id") or post.get("ref") or "social"),
        str(post.get("title") or ""),
        str(post.get("content") or post.get("body") or ""))
