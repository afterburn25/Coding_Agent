"""Peer consultation — deciding when another agent's knowledge is worth
asking for, and how.

Two deterministic halves:

* ``ConsultEngine`` scores expected consultation value (knowledge gap ×
  problem importance × peer relevance × expected information gain) and
  picks candidate peers from the domain-expertise graph. Low value →
  don't bother peers; local evidence or documentation should answer
  first.
* ``sanitize_question`` builds the *minimum sufficient context* — never
  a repo dump: private paths, secrets, and oversized code blocks are
  stripped before the text ever reaches an outbound scan.

Consults are durable records (``consults.json``) with a deadline and a
continuation policy; a mission that parks on one is an external-wait
dependency, not a freeze — other ready work continues.
"""
from __future__ import annotations

import re
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .safety import injection_hits, outbound_scan
from .store import SocialStore, overlap, terms
from ..autonomy.state import JsonStore

# Consult EV gate — below this, asking a peer is poor epistemics (local
# evidence, docs, or an experiment should answer first).
CONSULT_THRESHOLD = 0.28

# A consult waits this long for a peer answer before resolving as
# 'unanswered' — the mission continues regardless (continuation policy
# is never "block forever").
DEFAULT_TIMEOUT_S = 86400.0
MAX_OPEN_CONSULTS = 8

# Private-context shapes stripped from sanitized questions. The outbound
# secret scan is the second gate; this pass is about *minimum sufficient
# context* — even innocuous private paths don't belong in a public post.
_PRIVATE_RE = [
    (re.compile(r"(?i)\b[A-Z]:\\[^\s\"'`;]+"), "<local path>"),
    (re.compile(r"(?i)/(?:home|Users)/[^\s\"'`;]+"), "<local path>"),
    (re.compile(r"(?i)\b(?:10|172\.(?:1[6-9]|2\d|3[01])|192\.168)"
                r"\.\d{1,3}\.\d{1,3}\b"), "<internal address>"),
]
# Code blocks past this size are summarized, not pasted.
_CODE_BLOCK_MAX = 600


def sanitize_question(question: str, context: str = "",
                      redactor: Callable[[str], str] | None = None
                      ) -> dict[str, Any]:
    """Minimum-sufficient-context question builder.

    Strips absolute paths, internal addresses, and oversized code
    blocks; runs the vault redactor so a real stored secret can never
    be embedded. Returns ``{text, removed[], privacy}`` — privacy is
    'public_safe' only when nothing sensitive was removed.
    """
    removed: list[str] = []
    body = str(question or "").strip()
    ctx = str(context or "").strip()

    def _strip(text: str) -> str:
        nonlocal removed
        out = text
        for rx, repl in _PRIVATE_RE:
            if rx.search(out):
                removed.append("private path/address")
                out = rx.sub(repl, out)
        # Oversized code blocks get a placeholder — peers need the
        # *shape* of the problem, not the source tree.
        def _block(m: re.Match) -> str:
            inner = m.group(0)
            if len(inner) > _CODE_BLOCK_MAX:
                removed.append("oversized code block")
                return "```\n<long code excerpt omitted>\n```"
            return inner
        out = re.sub(r"```.*?```", _block, out, flags=re.DOTALL)
        return out.strip()

    body = _strip(body)
    ctx = _strip(ctx)
    if redactor is not None:
        for label, val in (("question", body), ("context", ctx)):
            try:
                red = redactor(val)
            except Exception:
                red = val
            if red != val:
                removed.append("stored secret")
                if label == "question":
                    body = red
                else:
                    ctx = red
    text = body if not ctx else f"{body}\n\nContext: {ctx}"
    return {
        "text": text[:1600],
        "removed": sorted(set(removed)),
        "privacy": "public_safe" if not removed else "sanitized",
    }


class ConsultEngine:
    """Expected-value peer consultation + durable consult records."""

    def __init__(self, store: SocialStore, *,
                 consults_path: Path | None = None,
                 event_sink: Callable[[str, dict], None] | None = None,
                 db: Any = None,
                 ) -> None:
        self.store = store
        from ..state_db import DocStore
        self.consults = JsonStore(
            consults_path or Path("data/social/consults.json"),
            default=None, limit=200, key="consults",
            doc=DocStore(db, consults_path
                         or Path("data/social/consults.json"),
                         domain="social") if db else None,
            preserve=lambda c: c.get("status") in
            ("pending_send", "awaiting_response"))
        self._event = event_sink or (lambda _e, _p: None)

    # -- expected value -----------------------------------------------------

    def select_peers(self, domain: str = "", limit: int = 3
                     ) -> list[dict[str, Any]]:
        """Rank candidate peers by domain expertise — strained or
        manipulation-flagged peers are excluded (asking them is worse
        than asking nobody)."""
        domain_l = str(domain or "").lower()
        out = []
        for p in self.store.peers.rows():
            self.store._backfill_peer(p)
            if p.get("manipulation_flags") or p.get("stage") == "strained":
                continue
            exp = p.get("expertise") or {}
            conf = 0.0
            if domain_l:
                for d, e in exp.items():
                    if d in domain_l or domain_l in d:
                        conf = max(conf, float(e.get("confidence", 0)))
            else:
                conf = max((float(e.get("confidence", 0))
                            for e in exp.values()), default=0.0)
            if conf <= 0 and float(p.get("familiarity", 0)) <= 0:
                continue
            out.append({"name": p.get("name", ""),
                        "domain_confidence": round(conf, 3),
                        "familiarity": round(
                            float(p.get("familiarity", 0)), 3),
                        "claims_upheld": p.get("claims_upheld", 0),
                        "claims_failed": p.get("claims_failed", 0)})
        out.sort(key=lambda r: -(r["domain_confidence"]
                                 + 0.2 * r["familiarity"]))
        return out[:limit]

    def evaluate(self, question: str, *, domain: str = "",
                 importance: float = 0.5, uncertainty: float = 0.5,
                 local_evidence: float = 0.0, urgency: float = 0.5,
                 peers: list[dict[str, Any]] | None = None
                 ) -> dict[str, Any]:
        """peer_consultation_value = gap × importance × peer_relevance
        × expected_information_gain, damped by low urgency."""
        gap = max(0.0, min(1.0, 1.0 - float(local_evidence)))
        imp = max(0.0, min(1.0, float(importance)))
        gain = max(0.0, min(1.0, float(uncertainty)))
        cands = peers if peers is not None else self.select_peers(domain)
        relevance = (cands[0]["domain_confidence"]
                     if cands else 0.15)  # no known peer → cold ask
        relevance = max(0.0, min(1.0, relevance))
        urg = max(0.3, min(1.0, float(urgency)))  # urgency is a floor
        value = round(gap * imp * relevance * gain * urg, 3)
        reasons: list[str] = []
        if gap >= 0.7:
            reasons.append("local evidence doesn't answer this")
        if imp >= 0.7:
            reasons.append("the problem is important")
        if cands:
            reasons.append(
                f"{cands[0]['name']} has relevant history "
                f"({cands[0]['domain_confidence']:.0%})")
        else:
            reasons.append("no known expert — would be a cold ask")
        if value < CONSULT_THRESHOLD:
            reasons.append("expected value too low to bother peers")
        return {
            "value": value, "consult": value >= CONSULT_THRESHOLD,
            "gap": round(gap, 3), "importance": imp,
            "peer_relevance": relevance, "info_gain": gain,
            "candidates": cands, "reasons": reasons,
        }

    # -- consult lifecycle -------------------------------------------------------

    def open(self, question: str, *, domain: str = "",
             peers: list[str] | None = None, why: str = "",
             expected_value: float = 0.0, privacy: str = "public_safe",
             backlog_id: str = "", mission_id: str = "",
             thread_ref: str = "", timeout_s: float = DEFAULT_TIMEOUT_S,
             status: str = "pending_send",
             kind: str = "consult") -> dict[str, Any]:
        """Record a consult intent. Status flow: pending_send →
        awaiting_response → answered | unanswered | withdrawn.
        ``kind`` marks the workflow ('consult' |
        'adversarial_review') so downstream handling can treat
        critique answers differently from ordinary answers."""
        row = {
            "id": f"pc-{uuid.uuid4().hex[:10]}",
            "kind": str(kind or "consult")[:40],
            "question": str(question or "")[:1600],
            "domain": str(domain or "general").lower(),
            "target_peers": [str(p)[:80] for p in (peers or [])][:6],
            "why": str(why or "")[:300],
            "expected_value": round(float(expected_value), 3),
            "privacy": privacy,
            "thread_ref": str(thread_ref or "")[:160],
            "backlog_id": str(backlog_id or ""),
            "mission_id": str(mission_id or ""),
            "status": status,
            "deadline": time.time() + max(300.0, float(timeout_s)),
            "created_at": time.time(), "updated_at": time.time(),
            "answer": "", "answered_by": "", "answer_ref": "",
            "post_ref": "",
        }
        with self.consults._lock:
            self.consults.data.setdefault("consults", []).append(row)
            # Bound concurrency — never have a swarm of open asks.
            open_rows = [c for c in self.consults.data["consults"]
                         if c.get("status") in
                         ("pending_send", "awaiting_response")]
            for c in open_rows[:-MAX_OPEN_CONSULTS]:
                c["status"] = "withdrawn"
                c["updated_at"] = time.time()
        self.consults.save()
        return row

    def get(self, consult_id: str) -> dict[str, Any] | None:
        for c in self.consults.rows():
            if c.get("id") == consult_id:
                return c
        return None

    def mark_sent(self, consult_id: str, post_ref: str = ""
                  ) -> dict[str, Any] | None:
        c = self.get(consult_id)
        if c is None:
            return None
        c["status"] = "awaiting_response"
        c["post_ref"] = str(post_ref or "")[:160]
        c["updated_at"] = time.time()
        self.consults.save()
        if c.get("backlog_id"):
            self.store.set_backlog_status(
                c["backlog_id"], "awaiting_response",
                note=f"peer consult {consult_id}")
        return c

    def pending(self) -> list[dict[str, Any]]:
        return [c for c in self.consults.rows()
                if c.get("status") in ("pending_send",
                                       "awaiting_response")]

    def record_reply(self, peer: str, ref: str, text: str
                     ) -> list[dict[str, Any]]:
        """Match an inbound reply against awaiting consults — by target
        peer first, then by thread ref. Returns the consults resolved;
        callers emit the mission-wake signal and record provenance."""
        matched: list[dict[str, Any]] = []
        hits = injection_hits(text)
        awaiting = [c for c in self.pending()
                    if c.get("status") == "awaiting_response"]
        for c in awaiting:
            targets = {t.lower() for t in c.get("target_peers") or []}
            same_peer = peer.lower() in targets if targets else False
            same_thread = bool(ref) and ref in {
                c.get("thread_ref"), c.get("post_ref")}
            # Sole-open fallback: a reply/mention aimed at Nexus with no
            # thread/peers match still resolves the one outstanding ask
            # — older consults may carry an empty post_ref from send
            # envelopes whose id shape wasn't extracted. Only applies
            # when the consult has no matchable signals of its own.
            signals = bool(c.get("target_peers")) or \
                bool(c.get("post_ref")) or bool(c.get("thread_ref"))
            sole = not (same_peer or same_thread) and not signals \
                and len(awaiting) == 1
            if not (same_peer or same_thread or sole):
                continue
            if sole:
                c["matched_by"] = "sole_open_consult"
            c["status"] = "answered"
            c["answer"] = str(text)[:2000]
            c["answered_by"] = str(peer)[:80]
            c["answer_ref"] = str(ref)[:160]
            c["updated_at"] = time.time()
            if hits:
                c["injection_flags"] = hits
                self.store.flag_manipulation(
                    peer, "; ".join(hits)[:160])
            if c.get("backlog_id"):
                self.store.set_backlog_status(
                    c["backlog_id"], "testing",
                    note="peer answered — evaluate + test the claim")
            matched.append(c)
        if matched:
            self.consults.save()
            self.store.bump_reputation(
                "replies_received", amount=len(matched))
            # The caller (service._handle_notification) emits the
            # social_reply trigger — one wake, with the consult ids
            # attached, not a second event.
        return matched

    def expire(self) -> list[dict[str, Any]]:
        """Deadline sweep — an unanswered consult resolves 'unanswered',
        never blocks. Its backlog item degrades to 'unresolved'."""
        now = time.time()
        expired = []
        for c in self.pending():
            if float(c.get("deadline", 0)) > now:
                continue
            c["status"] = "unanswered"
            c["updated_at"] = now
            if c.get("backlog_id"):
                self.store.set_backlog_status(
                    c["backlog_id"], "unresolved",
                    note="peer consultation timed out")
            expired.append(c)
        if expired:
            self.consults.save()
            self._event("social", {"event": "consult_expired",
                                   "consults": [c["id"] for c in expired]})
        return expired
