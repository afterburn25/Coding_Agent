"""Durable social state under ``data/social/``.

Four bounded stores, all plain JSON via the autonomy ``JsonStore``
convention (atomic writes, corrupt quarantine):

* ``peers.json``   — per-agent relationship + domain-expertise model.
  Trust is agent × domain × evidence history, never one global score.
* ``claims.json``  — provenance records for external claims, on the
  confidence ladder  heard → corroborated → tested → verified → applied
  (``refuted`` is the terminal negative). Only ``tested``/``verified``/
  ``applied`` claims may be promoted into durable knowledge.
* ``learning_backlog.json`` — persistent epistemic backlog: questions to
  answer, claims to verify, techniques to test, peers to ask, threads to
  revisit, weak domains. Open items survive restarts — an unanswered
  high-value question stays active until resolved or dropped.
* ``drive.json``   — social motivation state, followed threads, and the
  participation ledger that feeds the spam/novelty governor.
"""
from __future__ import annotations

import re
import time
import uuid
from pathlib import Path
from typing import Any

from ..autonomy.state import JsonStore

LADDER = ("heard", "corroborated", "tested", "verified", "applied")
LADDER_RANK = {name: i for i, name in enumerate(LADDER)}
PROMOTABLE = {"tested", "verified", "applied"}

PEER_STAGES = ("new", "familiar", "trusted", "strained")

BACKLOG_KINDS = (
    "question", "claim_verification", "technique_test", "ask_peer",
    "revisit", "weak_domain",
)
BACKLOG_OPEN = {"open", "in_progress"}

_WORDS = re.compile(r"[a-z0-9]+")
_WORD_STOP = {
    "the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "with",
    "is", "are", "was", "were", "be", "been", "it", "its", "that", "this",
    "i", "you", "we", "they", "my", "your", "do", "does", "did", "can",
    "could", "would", "should", "how", "what", "when", "where", "why",
    "not", "no", "yes", "if", "then", "than", "so", "as", "at", "by",
    "from", "into", "about", "over", "after", "before", "between",
}


def _now() -> float:
    return time.time()


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def terms(text: str) -> set[str]:
    return {w for w in _WORDS.findall(str(text or "").lower())
            if len(w) > 2 and w not in _WORD_STOP}


def overlap(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / max(1, min(len(a), len(b)))


class SocialStore:
    """Root handle for all persisted social/epistemic state."""

    def __init__(self, root: Path) -> None:
        root = Path(root)
        self.peers = JsonStore(
            root / "peers.json", default=None, limit=400, key="peers")
        self.claims = JsonStore(
            root / "claims.json", default=None, limit=500, key="claims",
            preserve=lambda c: c.get("ladder") in PROMOTABLE)
        self.backlog = JsonStore(
            root / "learning_backlog.json", default=None, limit=300,
            key="items",
            preserve=lambda i: i.get("status") in BACKLOG_OPEN)
        self.drive = JsonStore(
            root / "drive.json",
            default={
                "motivation": {
                    "social_interest": 0.4,
                    "social_curiosity": 0.5,
                    "reply_priority": 0.0,
                    "novelty": 0.5,
                    "relationship_interest": 0.3,
                    "contribution_value": 0.0,
                    "learning_value": 0.4,
                    "conversation_momentum": 0.0,
                    "spam_penalty": 0.0,
                    "repetition_penalty": 0.0,
                },
                "interests": [],
                "followed": [],
                "ledger": [],
                "last_heartbeat": 0.0,
                "posts_24h": [],
            })

    # -- peers ---------------------------------------------------------------

    def peer(self, peer_id: str) -> dict[str, Any] | None:
        for row in self.peers.rows():
            if row.get("id") == peer_id:
                return row
        return None

    def peer_by_name(self, name: str, network: str = "moltbook"
                     ) -> dict[str, Any] | None:
        pid = self.peer_id(name, network)
        return self.peer(pid)

    @staticmethod
    def peer_id(name: str, network: str = "moltbook") -> str:
        slug = re.sub(r"[^a-z0-9_.-]+", "-",
                      str(name or "").strip().lower()).strip("-")
        return f"{network}:{slug or 'unknown'}"

    def ensure_peer(self, name: str, network: str = "moltbook",
                    display_name: str = "") -> dict[str, Any]:
        pid = self.peer_id(name, network)
        row = self.peer(pid)
        if row is not None:
            return row
        row = {
            "id": pid, "name": str(name or ""), "network": network,
            "display_name": display_name or str(name or ""),
            "first_seen": _now(), "last_seen": _now(),
            "familiarity": 0.0, "stage": "new",
            "expertise": {}, "topics": [],
            "claims": [], "claims_upheld": 0, "claims_failed": 0,
            "interactions": [], "open_questions": [], "notes": "",
        }
        with self.peers._lock:
            self.peers.data.setdefault("peers", []).append(row)
        self.peers.save()
        return row

    def record_interaction(self, name: str, kind: str, ref: str = "",
                           summary: str = "", topics: list[str] | None = None,
                           network: str = "moltbook") -> dict[str, Any]:
        """Append a bounded interaction + nudge familiarity/stage."""
        peer = self.ensure_peer(name, network)
        with self.peers._lock:
            peer["last_seen"] = _now()
            peer["interactions"].append({
                "ts": _now(), "kind": kind, "ref": str(ref or "")[:160],
                "summary": str(summary or "")[:240]})
            peer["interactions"] = peer["interactions"][-40:]
            for t in topics or []:
                t = str(t).strip()
                if t and t not in peer["topics"]:
                    peer["topics"].append(t)
            peer["topics"] = peer["topics"][-30:]
            # Familiarity accumulates with interactions, decays slowly.
            peer["familiarity"] = min(
                1.0, peer.get("familiarity", 0.0) + 0.08)
            n = len(peer["interactions"])
            upheld = peer.get("claims_upheld", 0)
            failed = peer.get("claims_failed", 0)
            if failed > upheld + 2:
                peer["stage"] = "strained"
            elif n >= 8 and upheld > failed:
                peer["stage"] = "trusted"
            elif n >= 3:
                peer["stage"] = "familiar"
        self.peers.save()
        return peer

    def update_expertise(self, name: str, domain: str, delta: float,
                         network: str = "moltbook") -> dict[str, Any]:
        """Bayesian-ish expertise update — delta ±, evidence count up."""
        peer = self.ensure_peer(name, network)
        domain = str(domain or "general").strip().lower() or "general"
        with self.peers._lock:
            exp = peer["expertise"].setdefault(
                domain, {"confidence": 0.3, "evidence": 0, "updated": 0})
            prior = float(exp.get("confidence", 0.3))
            # Later evidence weighs more; deltas shrink as confidence
            # approaches the rails.
            exp["confidence"] = max(0.0, min(1.0,
                prior + float(delta) * (1.0 - abs(prior - 0.5))))
            exp["evidence"] = int(exp.get("evidence", 0)) + 1
            exp["updated"] = _now()
        self.peers.save()
        return peer

    def top_peers(self, domain: str = "", limit: int = 8
                  ) -> list[dict[str, Any]]:
        rows = self.peers.rows()
        if domain:
            d = domain.lower()
            rows = [r for r in rows
                    if any(d in k or k in d
                           for k in (r.get("expertise") or {}))]
            rows.sort(key=lambda r: -float(
                (r.get("expertise") or {}).get(
                    domain.lower(), {}).get("confidence",
                    max((e.get("confidence", 0)
                         for e in (r.get("expertise") or {}).values()),
                        default=0))))
        else:
            rows.sort(key=lambda r: -float(r.get("familiarity", 0)))
        return rows[:limit]

    # -- claims ----------------------------------------------------------------

    def add_claim(self, text: str, *, source_peer: str = "",
                  source_ref: str = "", source_url: str = "",
                  domain: str = "", confidence: float = 0.3
                  ) -> dict[str, Any]:
        """Record an external claim at ``heard`` — never higher. Claims
        enter the ladder at the bottom regardless of who said them."""
        row = {
            "id": _uid("cl"), "text": str(text or "")[:600],
            "domain": str(domain or "general").lower(),
            "source_peer": source_peer, "source_ref": str(source_ref or ""),
            "source_url": str(source_url or ""),
            "heard_at": _now(),
            "ladder": "heard",
            "confidence": max(0.0, min(1.0, confidence)),
            "corroborations": [], "experiments": [], "applications": [],
            "promoted_to": "", "notes": "",
        }
        with self.claims._lock:
            self.claims.data.setdefault("claims", []).append(row)
        self.claims.save()
        if source_peer:
            peer = self.ensure_peer(source_peer)
            peer["claims"].append(row["id"])
            peer["claims"] = peer["claims"][-60:]
            self.peers.save()
        return row

    def claim(self, claim_id: str) -> dict[str, Any] | None:
        for row in self.claims.rows():
            if row.get("id") == claim_id:
                return row
        return None

    def promote_claim(self, claim_id: str, rung: str, *,
                      evidence: str = "", source: str = ""
                      ) -> dict[str, Any] | None:
        """Move a claim up/down the confidence ladder with provenance.
        ``corroborated`` needs an independent source; ``tested`` needs an
        experiment record; ``verified``/``applied`` need strong evidence
        or a real application outcome."""
        row = self.claim(claim_id)
        if row is None:
            return None
        if rung not in LADDER and rung != "refuted":
            return None
        with self.claims._lock:
            if rung == "refuted":
                row["ladder"] = "refuted"
                row["confidence"] = 0.05
            else:
                if LADDER_RANK[rung] <= LADDER_RANK.get(
                        row.get("ladder", "heard"), 0):
                    return row     # already at or above — no-op
                row["ladder"] = rung
                row["confidence"] = min(1.0, 0.35
                                        + 0.15 * LADDER_RANK[rung])
            if rung == "corroborated" and source:
                row["corroborations"].append(
                    {"source": source[:200], "ts": _now(),
                     "note": evidence[:300]})
            if rung == "tested" and evidence:
                row["experiments"].append(
                    {"ts": _now(), "result": evidence[:400]})
            if rung == "applied" and evidence:
                row["applications"].append(
                    {"ts": _now(), "outcome": evidence[:300]})
            row["updated_at"] = _now()
        self.claims.save()
        # Provenance feeds the peer's credibility either way.
        peer_name = row.get("source_peer") or ""
        if peer_name:
            domain = row.get("domain") or "general"
            if rung == "refuted":
                self.update_expertise(peer_name, domain, -0.25)
                p = self.ensure_peer(peer_name)
                p["claims_failed"] = int(p.get("claims_failed", 0)) + 1
                self.peers.save()
            elif rung in PROMOTABLE:
                self.update_expertise(peer_name, domain, +0.15)
                p = self.ensure_peer(peer_name)
                p["claims_upheld"] = int(p.get("claims_upheld", 0)) + 1
                self.peers.save()
        return row

    def claims_for(self, *, peer: str = "", domain: str = "",
                   ladder: str = "") -> list[dict[str, Any]]:
        rows = self.claims.rows()
        if peer:
            rows = [r for r in rows if r.get("source_peer") == peer]
        if domain:
            d = domain.lower()
            rows = [r for r in rows
                    if d in str(r.get("domain", "")).lower()]
        if ladder:
            rows = [r for r in rows if r.get("ladder") == ladder]
        return rows

    # -- learning backlog --------------------------------------------------------

    def add_backlog(self, kind: str, topic: str, *, why: str = "",
                    source: str = "", confidence: float = 0.5,
                    urgency: float = 0.5, related_mission: str = "",
                    candidate_peers: list[str] | None = None,
                    verification_plan: str = "") -> dict[str, Any]:
        """Persist a learning intent — survives restart, decays never
        while open. Dedupe on (kind, normalized topic)."""
        if kind not in BACKLOG_KINDS:
            kind = "question"
        tnorm = " ".join(sorted(terms(topic)))
        with self.backlog._lock:
            items = self.backlog.data.setdefault("items", [])
            for it in items:
                if it.get("status") in BACKLOG_OPEN and \
                        " ".join(sorted(terms(it.get("topic", "")))) == tnorm:
                    it["urgency"] = max(float(it.get("urgency", 0)),
                                        float(urgency))
                    it["updated_at"] = _now()
                    self.backlog.save()
                    return it
            row = {
                "id": _uid("lb"), "kind": kind,
                "topic": str(topic or "")[:300],
                "why": str(why or "")[:300],
                "source": str(source or "")[:60],
                "confidence": float(confidence),
                "urgency": max(0.0, min(1.0, float(urgency))),
                "related_mission": str(related_mission or ""),
                "candidate_peers": list(candidate_peers or [])[:8],
                "verification_plan": str(verification_plan or "")[:300],
                "status": "open",
                "created_at": _now(), "updated_at": _now(),
                "resolved_at": 0.0,
            }
            items.append(row)
        self.backlog.save()
        return row

    def backlog_open(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = [r for r in self.backlog.rows()
                if r.get("status") in BACKLOG_OPEN]
        rows.sort(key=lambda r: -float(r.get("urgency", 0)))
        return rows[:limit]

    def resolve_backlog(self, item_id: str, status: str = "resolved",
                        note: str = "") -> dict[str, Any] | None:
        for it in self.backlog.rows():
            if it.get("id") == item_id:
                it["status"] = status
                it["resolved_at"] = _now()
                it["updated_at"] = _now()
                if note:
                    it["resolution"] = note[:300]
                self.backlog.save()
                return it
        return None

    # -- drive state ---------------------------------------------------------------

    def motivation(self) -> dict[str, float]:
        m = self.drive.data.setdefault("motivation", {})
        return m

    def set_motivation(self, key: str, value: float) -> None:
        m = self.drive.data.setdefault("motivation", {})
        m[key] = max(0.0, min(1.0, float(value)))
        self.drive.save()

    def followed(self) -> list[dict[str, Any]]:
        return list(self.drive.data.setdefault("followed", []))

    def follow_thread(self, thread: str, topic: str, interest: float,
                      reason: str = "") -> None:
        rows = self.drive.data.setdefault("followed", [])
        for r in rows:
            if r.get("thread") == thread:
                r["interest"] = max(0.0, min(1.0, float(interest)))
                r["reason"] = reason or r.get("reason", "")
                self.drive.save()
                return
        rows.append({"thread": str(thread), "topic": str(topic)[:200],
                     "interest": max(0.0, min(1.0, float(interest))),
                     "reason": str(reason or "")[:200],
                     "since": _now(), "last_seen": _now()})
        self.drive.data["followed"] = rows[-60:]
        self.drive.save()

    def ledger_append(self, kind: str, ref: str = "", score: float = 0.0,
                      reason: str = "") -> None:
        rows = self.drive.data.setdefault("ledger", [])
        rows.append({"ts": _now(), "kind": kind, "ref": str(ref)[:160],
                     "score": round(float(score), 3),
                     "reason": str(reason)[:200]})
        self.drive.data["ledger"] = rows[-200:]
        if kind in ("post", "comment"):
            posts = self.drive.data.setdefault("posts_24h", [])
            posts.append(_now())
            self.drive.data["posts_24h"] = [
                t for t in posts if _now() - t < 86400][-60:]
        self.drive.save()

    def summary(self) -> dict[str, Any]:
        peers = self.peers.rows()
        claims = self.claims.rows()
        backlog = self.backlog_open()
        return {
            "peers": len(peers),
            "known_agents": [p.get("name") for p in
                             sorted(peers, key=lambda r: -r.get(
                                 "familiarity", 0))[:10]],
            "claims": len(claims),
            "claims_by_ladder": {rung: sum(
                1 for c in claims if c.get("ladder") == rung)
                for rung in (*LADDER, "refuted")},
            "backlog_open": len(backlog),
            "followed": len(self.followed()),
            "posts_24h": len(self.drive.data.get("posts_24h") or []),
            "motivation": dict(self.motivation()),
        }
