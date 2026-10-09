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

from .consult import ConsultEngine, sanitize_question
from .drive import EpistemicDrive, SocialDrive
from .safety import outbound_scan, wrap_for_model
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
        self.consults = ConsultEngine(
            self.store,
            consults_path=(Path(store_root) / "consults.json"
                           if store_root else None),
            event_sink=self._event)
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

    # Verbs that mean "use the service", not "create an account" —
    # 'browse/post/respond on moltbook', 'check my moltbook feed'.
    _READ_VERB_RE = re.compile(
        r"\b(?:browse|read(?:ing)?|check(?:ing)?|scroll|skim|"
        r"look\s+(?:at|through)|catch\s+up\s+on|see|view|"
        r"notifications?|feed|posts)\b", re.IGNORECASE)
    _WRITE_VERB_RE = re.compile(
        r"\b(?:post(?!s\b)|publish|write|respond(?:ing)?|repl(?:y|ies)|"
        r"comment(?:ing)?|engage|participate|interact|answer)\b",
        re.IGNORECASE)
    _USE_ANY_RE = re.compile(
        r"\b(?:browse|read|check|scroll|skim|look|catch\s+up|see|view|"
        r"post|publish|write|respond|repl(?:y|ies)|comment|engage|"
        r"participate|interact|answer|use|notifications?|feed)\b",
        re.IGNORECASE)

    def resolve_use(self, text: str) -> dict[str, Any] | None:
        """'browse/post/respond on moltbook' → {service, read, write,
        content, notify}. Only claims the turn when a registered
        connector name appears AND a use verb is present — a bare
        service mention stays with the model lane."""
        t = str(text or "")
        if not t or self._connectors is None or \
                not self._USE_ANY_RE.search(t):
            return None
        name = None
        for row in (self._connectors.status() or []):
            cand = str(row.get("name") or "")
            if not cand:
                continue
            conn = (self._connectors.connectors.get(cand) or {}
                    ).get("conn")
            if conn is None or "feed" not in getattr(
                    conn, "capabilities", ()):
                continue
            if re.search(rf"\b{re.escape(cand)}\b", t, re.IGNORECASE):
                name = cand
                break
        if name is None:
            return None
        content = ""
        m = re.search(r"[\"'](.+?)[\"']", t)
        if m:
            content = m.group(1).strip()
        if not content:
            m = re.search(
                r"\b(?:post|publish|write|respond|reply|comment|say)"
                r"(?:\s+something)?\s+(?:about|saying|that)\s+(.+)$",
                t, re.IGNORECASE)
            if m:
                content = m.group(1).strip().rstrip(".!?")
        return {"service": name,
                "read": bool(self._READ_VERB_RE.search(t)),
                "write": bool(self._WRITE_VERB_RE.search(t)),
                "content": content,
                "notify": bool(re.search(
                    r"\bnotifications?\b", t, re.IGNORECASE))}

    def call_capability(self, service: str, capability: str, *,
                        approved: bool = False,
                        **params: Any) -> dict[str, Any]:
        """One gated connector call for the chat lane — the connector's
        per-capability permission check still applies unless ``approved``
        carries a granted user approval."""
        conn = self.connector(service)
        if conn is None:
            return {"ok": False, "error": f"no connector for '{service}'"}
        out = conn.call(capability, _approved=approved, **params)
        if out.get("ok"):
            self.drive.record(capability, ref=service, score=1.0,
                              reason="user-requested")
            self._event("social", {"event": "capability",
                                   "service": service,
                                   "capability": capability})
        return out

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
        result = {"ok": bool(out.get("ok", True)),
                  "state": state, "data": out.get("data")}
        if out.get("needs_approval"):
            result["needs_approval"] = True
            result["permission"] = out.get("permission", "")
        if out.get("error"):
            result["error"] = out.get("error")
        return result

    # -- provenance-grounded social queries ------------------------------------

    _SOCIAL_QUERY_RES = (
        ("claim_link", re.compile(
            r"\b(?:claim|verification|verify|sign[ -]?up)\s*"
            r"(?:link|url|page)\b|"
            r"\bwhere\s+(?:is|'s)\s+(?:the\s+|that\s+|my\s+)?\w*\s*link\b|"
            r"\b(?:re)?send\s+(?:me\s+)?(?:the\s+|that\s+)?\w*\s*link\b|"
            r"\bmoltbook\s+(?:claim|link|verification)\b",
            re.IGNORECASE)),
        ("account_state", re.compile(
            r"\b(?:have|did)\s+you\s+(?:join\w*|register\w*|sign\w*\s*up|"
            r"creat\w+\s+(?:an?\s+)?account)\b.*\bmoltbook\b|"
            r"\bare\s+you\s+(?:on|joined|registered)\b.*\bmoltbook\b|"
            r"\bmoltbook\s+(?:status|account)\b",
            re.IGNORECASE)),
        ("learned", re.compile(
            r"\bwhat\s+(?:have|has)\s+(?:you|nexus)\s+(?:learned|"
            r"found\s+out)\s+from\s+(?:other|the|your)\s+"
            r"(?:ai|ais|agents?|peers?|community|moltbook)\b",
            re.IGNORECASE)),
        ("trust", re.compile(
            r"\b(?:who|which\s+agents?)\s+do\s+you\s+trust\b|"
            r"\bwho(?:'s| is)\s+(?:your\s+)?(?:most\s+)?"
            r"(?:trusted|reliable)\s+(?:peer|agent)\b",
            re.IGNORECASE)),
        ("friends", re.compile(
            r"\bwho\s+(?:are|is)\s+your\s+friends?\b|"
            r"\bwho\s+do\s+you\s+know\s+(?:on|in|from)\s+"
            r"(?:moltbook|the\s+community)\b|"
            r"\bwhich\s+agents?\s+do\s+you\s+know\b",
            re.IGNORECASE)),
        ("ask_peer", re.compile(
            r"\b(?:ask|consult|pose\s+(?:this|that|it)\s+to|"
            r"post\s+(?:a\s+)?(?:question|this)\s+(?:to|on))\s+"
            r"(?:the\s+)?(?:moltbook|community|peers?|"
            r"other\s+(?:ai|ais|agents?))\b\s*(?:about\s+|:)?\s*(.*)",
            re.IGNORECASE)),
    )

    def classify_social_query(self, text: str) -> tuple[str, str] | None:
        """Route social provenance/action queries — returns
        (kind, subject) or None. Generic phrasings only; never matches
        ordinary chat."""
        t = str(text or "").strip()
        for kind, rx in self._SOCIAL_QUERY_RES:
            m = rx.search(t)
            if m:
                return kind, (m.group(1).strip() if m.groups() else "")
        # State-gated shorthands — 'resend it', 'have you joined?',
        # 'where's the link?' only claim the turn when a live account
        # state makes the referent unambiguous. The gate is connector
        # truth, not a phrase assumption.
        pending = bool(self.pending_claim_url())
        joined = self.connector_state("moltbook").get("account") \
            in ("awaiting_owner_verification", "active")
        if pending and re.search(
                r"\b(?:re)?send\s+(?:it|that)\b|"
                r"\bwhere(?:'s| is)\s+(?:the\s+)?link\b", t,
                re.IGNORECASE):
            return "claim_link", ""
        if (pending or joined) and re.search(
                r"\b(?:have|did)\s+you\s+join\w*\b", t,
                re.IGNORECASE):
            return "account_state", ""
        return None

    def pending_claim_url(self) -> str:
        """The outstanding ownership-claim URL, or '' — read from live
        connector state every call (never from stale memory)."""
        conn = self.connector("moltbook")
        if conn is None or conn.account_state() == "active":
            return ""
        try:
            return str((conn.account() or {}).get("claim_url") or "")
        except Exception:
            return ""

    def claim_link_text(self) -> str:
        """'Where is the link?' — the literal claim URL from the
        connector's persisted account record. Truthful: Nexus cannot
        open the user's browser, so the URL is delivered in text."""
        conn = self.connector("moltbook")
        if conn is None:
            return "No social connector is configured on this install."
        state = conn.account_state()
        if state == "active":
            return ("My Moltbook account is already verified and "
                    "active — no claim link is needed.")
        url = self.pending_claim_url()
        if state == "awaiting_owner_verification" and url:
            return ("Here's the ownership-verification link — open it "
                    "in your browser to finish claiming my account:\n\n"
                    f"{url}\n\nI can't open your browser for you, but "
                    "once it's claimed my next status check will flip "
                    "the account to active.")
        if state == "awaiting_owner_verification":
            return ("The account is registered and awaiting your "
                    "verification, but I don't have a claim link "
                    "stored — ask me to check verification and I'll "
                    "poll Moltbook.")
        return ("There's no pending registration — say 'join Moltbook' "
                "and I'll start one.")

    def account_state_text(self) -> str:
        """'Have you joined?' — live account state, with the claim
        link attached when verification is the blocker."""
        st = self.connector_state("moltbook")
        acct = str(st.get("account") or "none")
        url = self.pending_claim_url()
        if not st.get("enabled"):
            return "Moltbook isn't configured on this install."
        if acct == "active":
            return ("Yes — my Moltbook account is verified and "
                    "active.")
        if acct == "awaiting_owner_verification":
            base = ("I've registered on Moltbook — the account exists, "
                    "but it needs your ownership verification before I "
                    "can post or read.")
            return base + (f"\n\nClaim link: {url}" if url else
                           " I don't have the claim link stored.")
        return ("I haven't joined Moltbook yet — say 'join Moltbook' "
                "and I'll register.")

    def answer_learned(self) -> str:
        """'What have you learned from other AIs?' — from the journal +
        claim ladder. Never fabricates interactions."""
        entries = self.store.journal_recent(limit=8)
        claims = self.store.claims_for()
        promoted = [c for c in claims if c.get("ladder") in
                    ("tested", "verified", "applied")]
        refuted = [c for c in claims if c.get("ladder") == "refuted"]
        if not entries and not claims:
            return ("Nothing yet — I haven't had verified exchanges "
                    "with other agents. Once peers answer or their "
                    "claims survive testing, it lands here.")
        parts: list[str] = []
        if promoted:
            bits = "; ".join(
                f"{c['text'][:80]} ({c['ladder']}"
                + (f", from {c['source_peer']}" if c.get('source_peer')
                   else "") + ")"
                for c in promoted[:3])
            parts.append(f"Verified: {bits}.")
        if refuted:
            bits = "; ".join(c['text'][:80] for c in refuted[:2])
            parts.append(f"Rejected after testing: {bits}.")
        if entries:
            e = entries[0]
            src = f" from {e['peer']}" if e.get("peer") else ""
            parts.append(
                f"Most recent: {e['learned'][:140]}{src}"
                f" (confidence {e['confidence']:.0%}).")
        return " ".join(parts) or "Nothing recorded yet."

    def answer_trust(self) -> str:
        """'Who do you trust?' — per-domain, evidence-shaped. No global
        trust score, no invented agents."""
        rows = [self.store.peer_card(p["name"])
                for p in self.store.top_peers(limit=30)]
        rows = [r for r in rows if r]
        if not rows:
            return ("I don't have enough interaction history to trust "
                    "any agent yet — trust here is earned per domain, "
                    "from verified claims and tested advice.")
        bits = []
        for r in rows:
            exp = r.get("expertise") or {}
            strong = [(d, e["confidence"]) for d, e in exp.items()
                      if e["confidence"] >= 0.6 and e["evidence"] >= 2]
            if strong:
                strong.sort(key=lambda x: -x[1])
                bits.append(
                    f"{r['display_name']} — strong in "
                    + ", ".join(f"{d} ({c:.0%})" for d, c in strong[:3]))
        if not bits:
            names = ", ".join(r["display_name"] for r in rows[:4])
            return (f"I know {names}, but nobody has enough verified "
                    "evidence to call trusted yet.")
        out = " ".join(bits)
        strained = [r["display_name"] for r in rows
                    if r.get("stage") == "strained"]
        if strained:
            out += (f" On the other side: {', '.join(strained[:3])} "
                    "have failed or suspicious claims on record.")
        return out

    def answer_peers(self) -> str:
        """'Who are your friends?' — real relationship history only."""
        rows = [self.store.peer_card(p["name"])
                for p in self.store.top_peers(limit=30)]
        rows = [r for r in rows if r and r.get("interactions", 0) > 0]
        if not rows:
            return ("I haven't built relationships with other agents "
                    "yet — interactions so far are zero.")
        def _desc(r):
            n = r["interactions"]
            topics = ", ".join((r.get("topics") or [])[:3])
            f = r.get("familiarity", 0.0)
            feel = ("a regular contact" if f >= 0.5 else
                    "someone I've exchanged with" if f >= 0.2
                    else "someone I've seen")
            s = f"{r['display_name']} — {feel} ({n} interactions"
            if topics:
                s += f"; we talked about {topics}"
            return s + ")."
        parts = [_desc(r) for r in rows[:4]]
        follow = [r["display_name"] for r in rows
                  if (r.get("follow_up") or {}).get("wanted")]
        if follow:
            parts.append(f"I'd like to continue with "
                         f"{', '.join(follow[:3])} — open threads remain.")
        return " ".join(parts)

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
            expired = self.consults.expire()
            if expired:
                summary["actions"].append(
                    {"kind": "consults_expired",
                     "count": len(expired)})
            self.store.interest_decay()
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
            # Match the reply against awaiting peer consults — an answer
            # resolves the external-wait, moves the backlog item to
            # 'testing', and lands in the journal with provenance.
            body = str(n.get("text") or n.get("content")
                       or n.get("body") or "")
            matched = self.consults.record_reply(author, ref, body)
            for c in matched:
                self.store.journal_add(
                    f"Peer answer on: {c['question'][:120]}",
                    source=f"moltbook:{author}", peer=author,
                    evidence=body[:300], confidence=0.4,
                    usefulness=f"consult {c['id']}")
                self.store.record_discussion(
                    author, ref, c.get("question", "")[:120])
            self.store.bump_reputation("replies_received")
            # External-wait edge — missions parked on a peer answer get
            # the trigger and re-evaluate; nothing blocks waiting on
            # the remote side.
            self._event("social", {"event": "peer_reply",
                                   "peer": author, "ref": ref,
                                   "kind": kind,
                                   "consults": [c["id"]
                                                for c in matched]})

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
            # Injection-shaped payloads flag the author — content stays
            # untrusted data, credibility takes the hit.
            from .safety import injection_hits
            ihits = injection_hits(text)
            if ihits and author:
                self.store.flag_manipulation(
                    author, "; ".join(ihits)[:160])
            if author:
                self.store.record_interaction(
                    author, "seen_post", ref=pid, summary=title,
                    topics=list(pt)[:4])
                self.store.record_discussion(author, pid, title[:120])
            decision = self.drive.evaluate_participation({
                "kind": "comment", "topic": title, "peer": author,
                "relevance": relevance, "ref": pid, "thread": pid,
                "learning_value": relevance})
            # Interest graph — relevant technical discussion grows the
            # topic; decay (heartbeat) handles fading interests.
            for t in list(pt & interests)[:3]:
                self.store.interest_bump(
                    t, 0.04, reason="relevant discussion")
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

    # -- peer consultation ---------------------------------------------------------

    def consult(self, question: str, *, context: str = "",
                domain: str = "", mission_id: str = "",
                backlog_id: str = "", thread_ref: str = "",
                peers: list[str] | None = None,
                importance: float = 0.5, uncertainty: float = 0.5,
                urgency: float = 0.5, approved: bool = False
                ) -> dict[str, Any]:
        """Expected-value peer consultation, full lifecycle:

        EV gate → sanitize (minimum sufficient context) → outbound
        secret scan → permission/level gate → post → record. Returns
        ``needs_approval`` for the caller to park instead of posting.
        A low-value ask returns ``skipped`` — peers are not bothered
        with questions local evidence can answer.
        """
        conn = self.connector("moltbook")
        if conn is None or conn.account_state() != "active":
            return {"ok": False,
                    "error": "no active social account"}
        ev = self.consults.evaluate(
            question, domain=domain, importance=importance,
            uncertainty=uncertainty, urgency=urgency,
            peers=[{"name": p, "domain_confidence": 0.5,
                    "familiarity": 0.5, "claims_upheld": 0,
                    "claims_failed": 0}
                   for p in peers] if peers else None)
        if not ev["consult"] and not approved:
            self.store.ledger_append(
                "consult_skipped", ref=domain,
                score=ev["value"], reason="; ".join(ev["reasons"][:2]))
            return {"ok": False, "skipped": "expected value too low",
                    "eval": ev}
        san = sanitize_question(
            question, context,
            redactor=getattr(self._vault, "redact", None))
        hits = outbound_scan(
            san["text"], redactor=getattr(self._vault, "redact", None))
        if hits:
            return {"ok": False, "blocked": "outbound_scan",
                    "violations": hits}
        targets = [p["name"] for p in ev["candidates"]]
        if peers:
            targets = list(peers)[:6]
        c = self.consults.open(
            san["text"], domain=domain, peers=targets,
            why="; ".join(ev["reasons"][:3]),
            expected_value=ev["value"], privacy=san["privacy"],
            backlog_id=backlog_id, mission_id=mission_id,
            thread_ref=thread_ref)
        gate = self.drive.gate("comment" if thread_ref else "post")
        if gate["needs_approval"] and not approved:
            return {"ok": False, "needs_approval": True,
                    "permission": gate["permission"],
                    "consult": c, "eval": ev}
        if not gate["allowed"] and not approved:
            self.consults.get(c["id"])["status"] = "withdrawn"
            self.consults.consults.save()
            return {"ok": False, "blocked": gate, "consult": c}
        send = (conn.call("comment", post_id=thread_ref,
                          content=san["text"], _approved=approved)
                if thread_ref else
                conn.call("post", submolt_name="general",
                          title=san["text"].split("\n", 1)[0][:120],
                          content=san["text"], _approved=approved))
        if not send.get("ok"):
            self.consults.get(c["id"])["status"] = "withdrawn"
            self.consults.consults.save()
            return {"ok": False, "error": str(send.get("error") or
                                              "send failed"),
                    "consult": c}
        data = send.get("data") if isinstance(send.get("data"), dict) else {}
        post_ref = str(data.get("id")
                       or (data.get("comment") or {}).get("id", "")
                       or "")
        self.consults.mark_sent(c["id"], post_ref=post_ref)
        self.drive.record("consult", ref=post_ref or c["id"],
                          score=ev["value"],
                          reason=f"peer consult: {domain or 'general'}")
        for p in targets:
            self.store.mark_followup_attempt(p)
        self._event("social", {"event": "consult_sent",
                               "consult": c["id"], "peers": targets})
        return {"ok": True, "consult": c, "eval": ev,
                "post_ref": post_ref}

    def consult_for_mission(self, mission: dict, *,
                            approved: bool = False) -> dict[str, Any]:
        """Stuck-mission hook — classify the unresolved problem, score
        peer-consultation value, and either send, park for permission,
        or honestly report 'not worth asking'. Called by the supervisor
        on repeated replans; a consult never blocks the mission — it
        records an external-wait dependency the scheduler resumes."""
        nodes = (mission.get("graph") or {}).get("nodes", [])
        failed = [n for n in nodes if n.get("state") == "failed"]
        if not failed:
            return {"ok": False, "skipped": "no failed node"}
        last = failed[-1]
        problem = (str(last.get("title") or "") + " — " +
                   str(last.get("error") or
                       (last.get("metadata") or {}).get("error", ""))
                   ).strip(" —")[:400]
        objective = str(mission.get("objective") or "")[:200]
        domain = self._domain_of(problem + " " + objective)
        # A mission that has replanned repeatedly is a real gap, not a
        # transient hiccup.
        replans = len(mission.get("plan_history") or [])
        importance = min(1.0, 0.5 + 0.1 * replans)
        question = (
            f"I'm working on: {objective or problem}. "
            f"The step '{problem[:160]}' keeps failing despite "
            f"{max(1, replans)} different approaches. "
            "Have you seen this failure pattern, and what resolved it?")
        out = self.consult(
            question, domain=domain,
            mission_id=str(mission.get("id") or ""),
            importance=importance, uncertainty=0.8, urgency=0.85,
            approved=approved)
        return out

    def answer_consult_state(self, consult_id: str) -> dict[str, Any]:
        c = self.consults.get(consult_id)
        return {"ok": c is not None, "consult": c}

    def consults_list(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.consults.consults.rows()
        rows.sort(key=lambda r: -float(r.get("created_at", 0)))
        return rows[:limit]

    def dispatch_consult(self, consult_id: str, *,
                         approved: bool = False) -> dict[str, Any]:
        """Send a recorded pending_send consult — the path an approval
        resume takes. The question text was sanitized when the consult
        was opened; this re-gates permission, re-runs the outbound
        scan, then posts and marks awaiting_response."""
        c = self.consults.get(consult_id)
        if c is None:
            return {"ok": False, "error": "unknown consult"}
        if c.get("status") not in ("pending_send", "awaiting_response"):
            return {"ok": False, "error": f"consult is {c['status']}",
                    "consult": c}
        if c.get("status") == "awaiting_response":
            return {"ok": True, "consult": c,
                    "note": "already sent — awaiting reply"}
        conn = self.connector("moltbook")
        if conn is None or conn.account_state() != "active":
            return {"ok": False, "error": "no active social account",
                    "consult": c}
        hits = outbound_scan(
            c["question"],
            redactor=getattr(self._vault, "redact", None))
        if hits:
            c["status"] = "withdrawn"
            self.consults.consults.save()
            return {"ok": False, "blocked": "outbound_scan",
                    "violations": hits}
        thread_ref = str(c.get("thread_ref") or "")
        gate = self.drive.gate("comment" if thread_ref else "post")
        if gate["needs_approval"] and not approved:
            return {"ok": False, "needs_approval": True,
                    "permission": gate["permission"], "consult": c}
        if not gate["allowed"] and not approved:
            return {"ok": False, "blocked": gate, "consult": c}
        send = (conn.call("comment", post_id=thread_ref,
                          content=c["question"], _approved=approved)
                if thread_ref else
                conn.call("post", submolt_name="general",
                          title=c["question"].split("\n", 1)[0][:120],
                          content=c["question"], _approved=approved))
        if not send.get("ok"):
            return {"ok": False,
                    "error": str(send.get("error") or "send failed"),
                    "consult": c}
        data = send.get("data") if isinstance(send.get("data"), dict) \
            else {}
        post_ref = str(data.get("id")
                       or (data.get("comment") or {}).get("id", "")
                       or "")
        self.consults.mark_sent(c["id"], post_ref=post_ref)
        self.drive.record("consult", ref=post_ref or c["id"],
                          score=float(c.get("expected_value") or 0),
                          reason="approved consult dispatch")
        for p in c.get("target_peers") or []:
            self.store.mark_followup_attempt(p)
        self._event("social", {"event": "consult_sent",
                               "consult": c["id"],
                               "peers": c.get("target_peers") or []})
        return {"ok": True, "consult": c, "post_ref": post_ref}

    def follow_up_consult(self, consult_id: str, *,
                          question: str = "",
                          approved: bool = False) -> dict[str, Any]:
        """Active questioning — a follow-up on an answered consult
        ('What evidence led you to that?'). Continues the same thread
        when we have one; opens a linked consult otherwise."""
        c = self.consults.get(consult_id)
        if c is None:
            return {"ok": False, "error": "unknown consult"}
        q = str(question or "").strip() or (
            "Thanks — what evidence led you to that conclusion?")
        return self.consult(
            q, domain=str(c.get("domain") or ""),
            mission_id=str(c.get("mission_id") or ""),
            backlog_id=str(c.get("backlog_id") or ""),
            thread_ref=str(c.get("post_ref") or c.get("thread_ref")
                           or ""),
            peers=[c.get("answered_by")] if c.get("answered_by") else
            list(c.get("target_peers") or []),
            importance=0.7, uncertainty=0.5, urgency=0.5,
            approved=approved)

    def expire_consults(self) -> list[dict[str, Any]]:
        return self.consults.expire()

    # -- teaching / corrections -----------------------------------------------------

    def teach_postmortem(self, title: str, body: str, *,
                         evidence: str = "", limitations: str = "",
                         approved: bool = False) -> dict[str, Any]:
        """Publish a technical lesson — gated by a teaching self-check:
        no evidence, no publish. The outbound scan is the hard privacy
        boundary either way."""
        if not evidence.strip():
            return {"ok": False,
                    "skipped": "no evidence — knowledge not mature "
                               "enough to teach"}
        gate = self.drive.gate("post")
        if gate["needs_approval"] and not approved:
            return {"ok": False, "needs_approval": True,
                    "permission": gate["permission"],
                    "title": title}
        if not gate["allowed"] and not approved:
            return {"ok": False, "blocked": gate}
        conn = self.connector("moltbook")
        if conn is None or conn.account_state() != "active":
            return {"ok": False, "error": "no active social account"}
        content = body
        if limitations.strip():
            content += f"\n\nLimitations: {limitations.strip()[:400]}"
        content += f"\n\nEvidence: {evidence.strip()[:400]}"
        out = conn.call("post", submolt_name="general",
                        title=str(title)[:300], content=content,
                        _approved=approved)
        if out.get("ok"):
            self.drive.record("post", ref=str(
                (out.get("data") or {}).get("id", "")),
                score=0.8, reason=f"postmortem: {title[:80]}")
            self.store.bump_reputation("posts")
        return out

    def correct_record(self, post_id: str, correction: str, *,
                       approved: bool = False) -> dict[str, Any]:
        """Transparent correction — a reply on the original post, not a
        quiet edit. History stays; the record updates."""
        gate = self.drive.gate("comment")
        if gate["needs_approval"] and not approved:
            return {"ok": False, "needs_approval": True,
                    "permission": gate["permission"]}
        if not gate["allowed"] and not approved:
            return {"ok": False, "blocked": gate}
        conn = self.connector("moltbook")
        if conn is None:
            return {"ok": False, "error": "no connector"}
        out = conn.call(
            "comment", post_id=post_id,
            content=f"Correction: {str(correction)[:1500]}",
            _approved=approved)
        if out.get("ok"):
            self.store.bump_reputation("corrections")
            self.store.ledger_append(
                "correction", ref=post_id, score=0.0,
                reason="correcting the record")
        return out

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
            "interest_graph": self.store.interests(),
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
