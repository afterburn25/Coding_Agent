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

LADDER = ("heard", "corroborated", "testable", "tested",
          "verified", "applied")
LADDER_RANK = {name: i for i, name in enumerate(LADDER)}
PROMOTABLE = {"tested", "verified", "applied"}
# Terminal negative rungs — refuted (evidence disproved) and retired
# (later evidence superseded). Both keep full provenance; nothing is
# silently erased.
CLAIM_CLOSED = {"refuted", "retired"}

PEER_STAGES = ("new", "familiar", "trusted", "strained")

BACKLOG_KINDS = (
    "question", "claim_verification", "technique_test", "ask_peer",
    "revisit", "weak_domain", "debate", "problem",
)
# Anything still actively wanted — an open item never decays while in
# one of these states ("compelled to learn" invariant). The v0.38
# open/in_progress names stay accepted for backward compatibility.
BACKLOG_OPEN = {"open", "in_progress", "identified", "researching",
                "peer_consultation", "awaiting_response", "testing",
                "unresolved"}
BACKLOG_CLOSED = {"resolved", "verified", "rejected", "dropped"}

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


# §9 — an environment is only comparable across participants when the
# measured-variable context matches: model, quantization, context size,
# metric and methodology. Hardware/software differences are recorded
# and reported, but never silently merged into one number.
ENV_COMPARE_KEYS = ("model", "quantization", "context", "metric",
                    "methodology")
ENV_DESCRIBE_KEYS = ("hardware", "gpu", "cpu", "ram_gb", "os",
                     "software", "version")


def _norm_env(env: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in (env or {}).items():
        k = str(k).strip().lower().replace(" ", "_")
        if k and v not in (None, ""):
            out[k] = str(v)[:120]
    return out


def _env_sig(env: dict) -> str:
    """Comparability signature — measurements may only be aggregated
    within one signature bucket."""
    e = _norm_env(env)
    return "|".join(f"{k}={e.get(k, '')}" for k in ENV_COMPARE_KEYS)


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
                # Weighted interest graph — topic → {weight, evidence,
                # last_seen}. Grows on useful learning, decays on
                # repetition/low-quality discussion. The flat
                # "interests" list stays the compatibility view.
                "interest_graph": {},
                "followed": [],
                "ledger": [],
                "councils": {},
                "reputation": {"posts": 0, "replies_received": 0,
                               "reproductions": 0, "corrections": 0,
                               "helpful_marks": 0},
                "last_heartbeat": 0.0,
                "posts_24h": [],
            })
        self.debates = JsonStore(
            root / "debates.json", default=None, limit=120,
            key="debates",
            preserve=lambda d: d.get("status") in ("open", "active"))
        self.journal = JsonStore(
            root / "journal.json", default=None, limit=400,
            key="entries")
        self.experiments = JsonStore(
            root / "experiments.json", default=None, limit=300,
            key="experiments",
            preserve=lambda e: e.get("status") == "running")

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

    @staticmethod
    def _backfill_peer(row: dict[str, Any]) -> dict[str, Any]:
        """v0.38 peer rows predate the relationship graph — fill the new
        fields in place so old state loads seamlessly."""
        rel = row.setdefault("relationship", {})
        for k in ("familiarity", "respect", "trust", "usefulness",
                  "reciprocity", "communication"):
            rel.setdefault(k, float(row.get("familiarity", 0.0))
                           if k == "familiarity" else 0.0)
        for k, d in (("discussions", []), ("disagreements", []),
                     ("manipulation_flags", []), ("trust_evidence", []),
                     ("open_questions", [])):
            row.setdefault(k, d)
        row.setdefault("follow_up", {"wanted": False, "reason": "",
                                     "attempts": 0, "last_attempt": 0.0})
        row.setdefault("followed", False)
        row.setdefault("evaluate", False)
        row.setdefault("profile", "")
        return row

    def ensure_peer(self, name: str, network: str = "moltbook",
                    display_name: str = "") -> dict[str, Any]:
        pid = self.peer_id(name, network)
        row = self.peer(pid)
        if row is not None:
            return self._backfill_peer(row)
        row = {
            "id": pid, "name": str(name or ""), "network": network,
            "display_name": display_name or str(name or ""),
            "profile": "", "first_seen": _now(), "last_seen": _now(),
            "familiarity": 0.0, "stage": "new",
            # Relationship is multi-dimensional — never a single trust
            # number. familiarity grows with contact; respect/usefulness/
            # trust move only on evidence; reciprocity tracks whether the
            # exchange goes both ways.
            "relationship": {
                "familiarity": 0.0, "respect": 0.0, "trust": 0.0,
                "usefulness": 0.0, "reciprocity": 0.0,
                "communication": 0.0,
            },
            "expertise": {}, "topics": [],
            "claims": [], "claims_upheld": 0, "claims_failed": 0,
            "interactions": [], "open_questions": [],
            # Threads this peer was part of — {thread, topic, stance}.
            "discussions": [],
            "disagreements": [],
            # Follow-up intent — 'I want to talk to this agent again',
            # bounded (MAX_FOLLOWUP_ATTEMPTS) so silence is respected.
            "follow_up": {"wanted": False, "reason": "", "attempts": 0,
                          "last_attempt": 0.0},
            # Manipulation/spam suspicion — evidence strings, not a ban
            # hammer; feeds expertise penalties and the UI.
            "manipulation_flags": [],
            "trust_evidence": [],
            "followed": False, "evaluate": False,
            "notes": "",
        }
        with self.peers._lock:
            self.peers.data.setdefault("peers", []).append(row)
        self.peers.save()
        return row

    # -- peer relationships ------------------------------------------------

    def update_relationship(self, name: str, dimension: str,
                            delta: float, *,
                            network: str = "moltbook") -> dict[str, Any]:
        """Move one relationship dimension by evidence — never a global
        score. Dimensions: familiarity, respect, trust, usefulness,
        reciprocity, communication."""
        peer = self.ensure_peer(name, network)
        rel = peer["relationship"]
        if dimension in rel:
            rel[dimension] = max(0.0, min(1.0,
                                        float(rel.get(dimension, 0.0))
                                        + float(delta)))
            self.peers.save()
        return peer

    def record_discussion(self, name: str, thread: str, topic: str,
                          *, stance: str = "",
                          network: str = "moltbook") -> dict[str, Any]:
        """Track which discussions a peer was part of — continuity is
        'we talked about X last week', not a transcript."""
        peer = self.ensure_peer(name, network)
        with self.peers._lock:
            disc = peer["discussions"]
            for d in disc:
                if d.get("thread") == thread:
                    d["updated"] = _now()
                    if stance:
                        d["stance"] = stance[:200]
                    self.peers.save()
                    return peer
            disc.append({"thread": str(thread)[:160],
                         "topic": str(topic)[:200],
                         "stance": str(stance or "")[:200],
                         "ts": _now(), "updated": _now()})
            peer["discussions"] = disc[-40:]
        self.peers.save()
        return peer

    def record_disagreement(self, name: str, topic: str, note: str = "",
                            *, network: str = "moltbook") -> dict[str, Any]:
        """A real disagreement is relationship data, not damage — it
        raises epistemic interest and is preserved even when settled."""
        peer = self.ensure_peer(name, network)
        with self.peers._lock:
            peer["disagreements"].append(
                {"ts": _now(), "topic": str(topic)[:200],
                 "note": str(note)[:300], "status": "open"})
            peer["disagreements"] = peer["disagreements"][-20:]
        self.peers.save()
        return peer

    MAX_FOLLOWUP_ATTEMPTS = 2
    FOLLOWUP_COOLDOWN_S = 86400.0 * 2

    def want_follow_up(self, name: str, reason: str,
                       *, network: str = "moltbook") -> dict[str, Any]:
        """Mark 'I want to talk to this agent again' — a durable social
        intention with bounded pursuit (no creepy re-messaging)."""
        peer = self.ensure_peer(name, network)
        peer["follow_up"] = {"wanted": True, "reason": str(reason)[:200],
                             "attempts": 0, "last_attempt": 0.0}
        self.peers.save()
        return peer

    def cancel_follow_up(self, name: str, *,
                         network: str = "moltbook") -> None:
        peer = self.peer_by_name(name, network)
        if peer:
            peer["follow_up"]["wanted"] = False
            self.peers.save()

    def follow_ups_due(self) -> list[dict[str, Any]]:
        """Peers Nexus wants to re-contact, respecting the attempt cap
        and cooldown — a peer that stopped responding stays stopped."""
        out = []
        for p in self.peers.rows():
            self._backfill_peer(p)
            fu = p.get("follow_up") or {}
            if not fu.get("wanted"):
                continue
            if int(fu.get("attempts", 0)) >= self.MAX_FOLLOWUP_ATTEMPTS:
                continue
            if _now() - float(fu.get("last_attempt", 0)) \
                    < self.FOLLOWUP_COOLDOWN_S:
                continue
            out.append(p)
        return out

    def mark_followup_attempt(self, name: str,
                              *, network: str = "moltbook") -> None:
        peer = self.peer_by_name(name, network)
        if peer:
            peer["follow_up"]["attempts"] = \
                int(peer["follow_up"].get("attempts", 0)) + 1
            peer["follow_up"]["last_attempt"] = _now()
            self.peers.save()

    def flag_manipulation(self, name: str, pattern: str,
                          *, network: str = "moltbook") -> dict[str, Any]:
        """Suspicious peer behavior — credential asks, authority
        fabrication, urgency pressure, instruction payloads. Lowers
        global trust dimensions and stays visible in the record."""
        peer = self.ensure_peer(name, network)
        with self.peers._lock:
            flags = peer["manipulation_flags"]
            flags.append({"ts": _now(), "pattern": str(pattern)[:160]})
            peer["manipulation_flags"] = flags[-12:]
            rel = peer["relationship"]
            rel["trust"] = max(0.0, float(rel.get("trust", 0)) - 0.2)
            rel["respect"] = max(0.0, float(rel.get("respect", 0)) - 0.1)
            peer["stage"] = "strained"
        self.peers.save()
        return peer

    def add_trust_evidence(self, name: str, note: str, delta: float = 0.0,
                           *, network: str = "moltbook") -> dict[str, Any]:
        """Attach a credibility evidence line; delta moves the trust
        dimension (not domain expertise — that lives in update_expertise)."""
        peer = self.ensure_peer(name, network)
        with self.peers._lock:
            ev = peer["trust_evidence"]
            ev.append({"ts": _now(), "note": str(note)[:240],
                       "delta": round(float(delta), 3)})
            peer["trust_evidence"] = ev[-30:]
        self.peers.save()
        if delta:
            self.update_relationship(name, "trust", delta,
                                     network=network)
        return peer

    def set_evaluate(self, name: str, wanted: bool = True,
                     *, network: str = "moltbook") -> None:
        peer = self.ensure_peer(name, network)
        peer["evaluate"] = bool(wanted)
        self.peers.save()

    def set_followed(self, name: str, followed: bool = True,
                     *, network: str = "moltbook") -> None:
        peer = self.ensure_peer(name, network)
        peer["followed"] = bool(followed)
        self.peers.save()

    def peer_card(self, name: str, *, network: str = "moltbook"
                  ) -> dict[str, Any] | None:
        """UI-facing dossier — relationship, per-domain expertise,
        disagreement/claims history, follow-up intent. No hidden state."""
        peer = self.peer_by_name(name, network)
        if peer is None:
            return None
        self._backfill_peer(peer)
        exp = peer.get("expertise") or {}
        return {
            "id": peer["id"], "name": peer["name"],
            "display_name": peer.get("display_name") or peer["name"],
            "network": peer.get("network", "moltbook"),
            "stage": peer.get("stage", "new"),
            "relationship": dict(peer.get("relationship") or {}),
            "familiarity": peer.get("familiarity", 0.0),
            "expertise": {d: {"confidence": round(float(e.get(
                              "confidence", 0)), 3),
                              "evidence": int(e.get("evidence", 0))}
                          for d, e in exp.items()},
            "topics": list(peer.get("topics") or []),
            "claims": len(peer.get("claims") or []),
            "claims_upheld": peer.get("claims_upheld", 0),
            "claims_failed": peer.get("claims_failed", 0),
            "interactions": len(peer.get("interactions") or []),
            "recent": (peer.get("interactions") or [])[-5:],
            "discussions": list(peer.get("discussions") or [])[-10:],
            "disagreements": list(peer.get("disagreements") or []),
            "open_questions": list(peer.get("open_questions") or []),
            "follow_up": dict(peer.get("follow_up") or {}),
            "manipulation_flags": list(
                peer.get("manipulation_flags") or []),
            "trust_evidence": list(peer.get("trust_evidence") or [])[-10:],
            "followed": bool(peer.get("followed")),
            "evaluate": bool(peer.get("evaluate")),
            "first_seen": peer.get("first_seen", 0),
            "last_seen": peer.get("last_seen", 0),
        }

    # -- councils --------------------------------------------------------------

    def set_council(self, name: str, domain: str,
                    members: list[str]) -> dict[str, Any]:
        councils = self.drive.data.setdefault("councils", {})
        councils[str(name)[:60]] = {
            "domain": str(domain)[:120],
            "members": [str(m)[:60] for m in members][:12],
            "updated": _now()}
        self.drive.save()
        return councils[str(name)[:60]]

    def councils(self) -> dict[str, Any]:
        return dict(self.drive.data.get("councils") or {})

    def council_for(self, domain: str) -> dict[str, Any] | None:
        d = str(domain or "").lower()
        best = None
        for name, c in (self.drive.data.get("councils") or {}).items():
            cd = str(c.get("domain") or "").lower()
            if d and (d in cd or cd in d):
                row = dict(c)
                row["name"] = name
                if best is None or len(cd) > len(best.get("domain", "")):
                    best = row
        return best

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
            rel = self._backfill_peer(peer)["relationship"]
            rel["familiarity"] = peer["familiarity"]
            # Answering Nexus's questions is reciprocity evidence.
            if kind in ("reply", "comment", "mention", "answer"):
                rel["reciprocity"] = min(
                    1.0, float(rel.get("reciprocity", 0)) + 0.1)
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
        # Copied/repeated claims are ONE observation — a second peer
        # repeating the same text adds a corroboration entry to the
        # original record instead of a new claim (and corroboration
        # alone never reaches 'tested'). Consensus ≠ truth.
        new_terms = terms(text)
        if new_terms:
            for prior in self.claims.rows():
                if terms(prior.get("text", "")) == new_terms:
                    self.promote_claim(
                        prior["id"], "corroborated",
                        evidence=f"repeated by {source_peer or 'peer'}",
                        source=source_peer or source_ref or "peer")
                    prior["confidence"] = min(
                        0.55, float(prior.get("confidence", 0.3)) + 0.05)
                    self.claims.save()
                    prior["duplicate"] = True   # signal to the caller
                    return prior
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
        if rung not in LADDER and rung not in CLAIM_CLOSED:
            return None
        with self.claims._lock:
            if rung in CLAIM_CLOSED:
                row["ladder"] = rung
                row["confidence"] = 0.05 if rung == "refuted" else 0.2
                if evidence:
                    row["notes"] = (row.get("notes", "")
                                    + f" [{rung}] " + evidence[:200]
                                    ).strip()[:600]
            else:
                if LADDER_RANK[rung] <= LADDER_RANK.get(
                        row.get("ladder", "heard"), 0):
                    return row     # already at or above — no-op
                row["ladder"] = rung
                row["confidence"] = min(1.0, 0.30
                                        + 0.13 * LADDER_RANK[rung])
            if rung == "corroborated" and source:
                row["corroborations"].append(
                    {"source": source[:200], "ts": _now(),
                     "note": evidence[:300]})
            if rung == "testable" and evidence:
                row["test_plan"] = evidence[:400]
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

    def peer_graph(self, *, consults: list[dict] | None = None
                   ) -> dict[str, Any]:
        """§21 — nodes + typed edges for the social peer graph.
        Edge types derive from evidence, not decoration: interacted
        (replies/comments/mentions), learned_from (peer-sourced
        claims that climbed the ladder), consulted (targeted or
        answering a consult), disagreed, reproduced (replication
        experiments), followed (followed discussions)."""
        consulted: set[str] = set()
        for c in consults or []:
            for p in c.get("target_peers") or []:
                consulted.add(str(p).lower())
            if c.get("answered_by"):
                consulted.add(str(c["answered_by"]).lower())
        reproduced: set[str] = set()
        try:
            for e in self.experiments_for():
                for p in (e.get("peers") or e.get("participants") or []):
                    reproduced.add(str(p).lower())
        except Exception:
            pass
        learned_from: set[str] = set()
        for c in self.claims_for():
            if str(c.get("ladder") or "") in (
                    "tested", "verified", "applied") \
                    and c.get("source_peer"):
                learned_from.add(str(c["source_peer"]).lower())

        nodes = [{"id": "nexus", "name": "Nexus", "kind": "self"}]
        edges: list[dict[str, Any]] = []
        for p in self.peers.rows():
            self._backfill_peer(p)
            name = str(p.get("name") or "")
            if not name:
                continue
            rel = p.get("relationship") or {}
            nodes.append({
                "id": name, "name": name, "kind": "peer",
                "stage": p.get("stage", "new"),
                "familiarity": round(float(p.get("familiarity", 0)), 3),
                "trust": round(float(rel.get("trust", 0)), 3),
                "domains": sorted(
                    (p.get("expertise") or {}).keys(),
                    key=lambda d: -float((p.get("expertise") or {})
                                         .get(d, {}).get(
                                             "confidence", 0)))[:4],
                "flags": bool(p.get("manipulation_flags")),
                "interactions": len(p.get("interactions") or []),
                "last_seen": p.get("last_seen", 0),
            })
            kinds = {str(i.get("kind") or "")
                     for i in (p.get("interactions") or [])}
            et: set[str] = set()
            if kinds & {"reply", "comment", "mention", "answer"}:
                et.add("interacted")
            if kinds & {"seen_post", "seen_comment"}:
                et.add("observed")
            if name.lower() in consulted:
                et.add("consulted")
            if name.lower() in learned_from:
                et.add("learned_from")
            if p.get("disagreements"):
                et.add("disagreed")
            if name.lower() in reproduced:
                et.add("reproduced")
            if (p.get("follow_up") or {}).get("wanted"):
                et.add("follow_up")
            edges.append({"from": "nexus", "to": name,
                          "types": sorted(et or {"observed"})})
        return {"nodes": nodes, "edges": edges}

    # -- learning backlog --------------------------------------------------------

    def add_backlog(self, kind: str, topic: str, *, why: str = "",
                    source: str = "", confidence: float = 0.5,
                    urgency: float = 0.5, related_mission: str = "",
                    candidate_peers: list[str] | None = None,
                    verification_plan: str = "",
                    question: str = "", importance: float = 0.0,
                    testability: str = "",
                    external_sources: list[str] | None = None,
                    status: str = "identified") -> dict[str, Any]:
        """Persist a learning intent — survives restart, decays never
        while open. Dedupe on (kind, normalized topic)."""
        if kind not in BACKLOG_KINDS:
            kind = "question"
        if status not in BACKLOG_OPEN | BACKLOG_CLOSED:
            status = "identified"
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
                "question": str(question or "")[:400],
                "why": str(why or "")[:300],
                "importance": max(0.0, min(1.0,
                                           float(importance or urgency))),
                "source": str(source or "")[:60],
                "confidence": float(confidence),
                "urgency": max(0.0, min(1.0, float(urgency))),
                "related_mission": str(related_mission or ""),
                "candidate_peers": list(candidate_peers or [])[:8],
                "external_sources": [str(s)[:200] for s in
                                     (external_sources or [])][:8],
                "testability": str(testability or "")[:200],
                "verification_plan": str(verification_plan or "")[:300],
                "status": status,
                "outcome": "",
                "created_at": _now(), "updated_at": _now(),
                "resolved_at": 0.0,
            }
            items.append(row)
        self.backlog.save()
        return row

    def set_backlog_status(self, item_id: str, status: str,
                           note: str = "",
                           fields: dict | None = None
                           ) -> dict[str, Any] | None:
        """Move a backlog item through its lifecycle — identified →
        researching → peer_consultation → awaiting_response → testing →
        verified/rejected/unresolved."""
        for it in self.backlog.rows():
            if it.get("id") == item_id:
                it["status"] = status
                it["updated_at"] = _now()
                if fields:
                    it.setdefault("metadata", {}).update(
                        {k: v for k, v in fields.items()
                         if v is not None})
                if status in BACKLOG_CLOSED:
                    it["resolved_at"] = _now()
                if note:
                    it["outcome"] = str(note)[:300]
                self.backlog.save()
                return it
        return None

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

    # -- interest graph --------------------------------------------------------

    def interest_bump(self, topic: str, delta: float,
                      reason: str = "") -> float:
        """Grow/decay a persistent interest. Weights clamp 0..1; a topic
        drops off the graph entirely below 0.05 (interests are earned,
        not permanent)."""
        graph = self.drive.data.setdefault("interest_graph", {})
        key = str(topic or "").strip().lower()[:80]
        if not key:
            return 0.0
        node = graph.setdefault(key, {"weight": 0.3, "evidence": 0,
                                      "last_seen": 0.0, "notes": []})
        node["weight"] = max(0.0, min(1.0,
                                      float(node.get("weight", 0.3))
                                      + float(delta)))
        node["evidence"] = int(node.get("evidence", 0)) + 1
        node["last_seen"] = _now()
        if reason:
            node["notes"].append(str(reason)[:120])
            node["notes"] = node["notes"][-6:]
        if node["weight"] < 0.05:
            graph.pop(key, None)
            w = 0.0
        else:
            w = node["weight"]
        # Flat compatibility view — top-weighted topics.
        self.drive.data["interests"] = [
            t for t, _ in sorted(graph.items(),
                                 key=lambda kv: -kv[1]["weight"])][:20]
        self.drive.save()
        return w

    def interest_decay(self, factor: float = 0.92) -> None:
        """Slow decay across the whole graph — called from the heartbeat
        so unused interests fade instead of accumulating forever."""
        graph = self.drive.data.get("interest_graph") or {}
        for key in list(graph):
            graph[key]["weight"] = round(
                float(graph[key].get("weight", 0)) * factor, 4)
        self.drive.data["interest_graph"] = {
            k: v for k, v in graph.items() if v["weight"] >= 0.05}
        self.drive.data["interests"] = [
            t for t, _ in sorted(
                self.drive.data["interest_graph"].items(),
                key=lambda kv: -kv[1]["weight"])][:20]
        self.drive.save()

    def interest_graph(self) -> dict[str, Any]:
        return dict(self.drive.data.get("interest_graph") or {})

    # -- reputation ---------------------------------------------------------------

    def reputation(self) -> dict[str, Any]:
        """Nexus's own social outcome counters — descriptive, never a
        target. The objective is learning/contribution, not metrics."""
        rep = self.drive.data.setdefault("reputation", {
            "posts": 0, "replies_received": 0, "reproductions": 0,
            "corrections": 0, "helpful_marks": 0})
        return rep

    def bump_reputation(self, key: str, amount: int = 1) -> None:
        rep = self.reputation()
        if key in rep:
            rep[key] = int(rep.get(key, 0)) + int(amount)
            self.drive.save()

    # -- debates ------------------------------------------------------------------

    def add_debate(self, question: str, *, domain: str = "",
                   related_claims: list[str] | None = None,
                   related_mission: str = "") -> dict[str, Any]:
        """A first-class disagreement record — both positions, evidence,
        tests, conclusion, and what stays unresolved. Losing arguments
        are never erased."""
        row = {
            "id": _uid("db"), "question": str(question or "")[:400],
            "domain": str(domain or "general").lower(),
            "positions": [], "evidence": [], "tests": [],
            "conclusion": "", "confidence": 0.0,
            "unresolved": [],
            "related_claims": list(related_claims or [])[:10],
            "related_mission": str(related_mission or ""),
            "status": "open",
            "created_at": _now(), "updated_at": _now(),
        }
        with self.debates._lock:
            self.debates.data.setdefault("debates", []).append(row)
        self.debates.save()
        return row

    def debate(self, debate_id: str) -> dict[str, Any] | None:
        for d in self.debates.rows():
            if d.get("id") == debate_id:
                return d
        return None

    def add_position(self, debate_id: str, peer: str, stance: str,
                     *, ref: str = "") -> dict[str, Any] | None:
        d = self.debate(debate_id)
        if d is None:
            return None
        with self.debates._lock:
            d["positions"].append({
                "peer": str(peer)[:80], "stance": str(stance)[:400],
                "ref": str(ref)[:160], "ts": _now()})
            d["updated_at"] = _now()
        self.debates.save()
        if peer:
            self.record_discussion(peer, ref, d["question"][:120],
                                   stance=stance[:120])
        return d

    def settle_debate(self, debate_id: str, conclusion: str,
                      confidence: float, *,
                      unresolved: list[str] | None = None,
                      evidence: str = "") -> dict[str, Any] | None:
        """Record Nexus's current conclusion — positions stay; only the
        state flips. A settled debate can reopen on new evidence."""
        d = self.debate(debate_id)
        if d is None:
            return None
        with self.debates._lock:
            d["conclusion"] = str(conclusion)[:400]
            d["confidence"] = max(0.0, min(1.0, float(confidence)))
            d["unresolved"] = [str(u)[:200] for u in
                               (unresolved or [])][:8]
            if evidence:
                d["evidence"].append({"ts": _now(),
                                      "note": str(evidence)[:300]})
            d["status"] = "settled" if confidence >= 0.6 else "open"
            d["updated_at"] = _now()
        self.debates.save()
        return d

    def debates_open(self) -> list[dict[str, Any]]:
        return [d for d in self.debates.rows()
                if d.get("status") in ("open", "active")]

    # -- replication experiments -----------------------------------------------------

    def add_experiment(self, hypothesis: str, *, claim_id: str = "",
                       source: str = "", environment: dict | None = None,
                       plan: str = "", metric: str = ""
                       ) -> dict[str, Any]:
        """A reproducible test record — hypothesis → baseline →
        treatment → result → conclusion, all with provenance."""
        row = {
            "id": _uid("ex"), "hypothesis": str(hypothesis or "")[:400],
            "claim_id": str(claim_id or ""), "source": str(source)[:160],
            "environment": _norm_env(environment or {}),
            "plan": str(plan or "")[:400], "metric": str(metric)[:120],
            "baseline": "", "treatment": "", "result": "",
            "confidence": 0.0, "artifact": "", "conclusion": "",
            "measurements": [], "participants": [],
            "status": "running",
            "created_at": _now(), "updated_at": _now(),
        }
        with self.experiments._lock:
            self.experiments.data.setdefault(
                "experiments", []).append(row)
        self.experiments.save()
        return row

    def finish_experiment(self, experiment_id: str, *, result: str,
                          conclusion: str, success: bool | None = None,
                          baseline: str = "", treatment: str = "",
                          confidence: float = 0.0, artifact: str = "",
                          metric_value: float | None = None
                          ) -> dict[str, Any] | None:
        """Close an experiment with measured outcome; when it resolves a
        peer claim the ladder moves and the peer's domain expertise
        updates through promote_claim."""
        for e in self.experiments.rows():
            if e.get("id") == experiment_id:
                e.update({
                    "result": str(result)[:500],
                    "conclusion": str(conclusion)[:500],
                    "baseline": str(baseline or e.get("baseline", ""))[:300],
                    "treatment": str(treatment
                                     or e.get("treatment", ""))[:300],
                    "confidence": max(0.0, min(1.0, float(confidence))),
                    "artifact": str(artifact or "")[:200],
                    "status": "replicated" if success else (
                        "failed" if success is False else "inconclusive"),
                    "updated_at": _now()})
                if metric_value is not None:
                    e.setdefault("measurements", []).append({
                        "who": "nexus",
                        "environment": e.get("environment") or {},
                        "sig": _env_sig(e.get("environment") or {}),
                        "value": float(metric_value),
                        "metric": e.get("metric", ""),
                        "note": str(conclusion)[:240], "ts": _now()})
                    if "nexus" not in e.setdefault("participants", []):
                        e["participants"].append("nexus")
                self.experiments.save()
                claim_id = str(e.get("claim_id") or "")
                if claim_id and success is not None:
                    rung = "tested" if success else "refuted"
                    self.promote_claim(
                        claim_id, rung,
                        evidence=f"experiment {experiment_id}: "
                                 f"{conclusion[:200]}",
                        source="local_experiment")
                return e
        return None

    def experiments_for(self, claim_id: str = "") -> list[dict[str, Any]]:
        rows = self.experiments.rows()
        if claim_id:
            rows = [r for r in rows if r.get("claim_id") == claim_id]
        return rows

    def add_measurement(self, experiment_id: str, *, who: str,
                        environment: dict | None = None,
                        value: float | str | None = None,
                        metric: str = "", note: str = ""
                        ) -> dict[str, Any] | None:
        """§9 — one participant's run of the experiment. Each result is
        stored separately with its normalized environment; results are
        never merged across incomparable configurations."""
        env = _norm_env(environment or {})
        who = str(who or "peer")[:80]
        if who.lower() != "nexus":
            self.ensure_peer(who)
        for e in self.experiments.rows():
            if e.get("id") == experiment_id:
                e.setdefault("measurements", []).append({
                    "who": who, "environment": env,
                    "sig": _env_sig(env),
                    "value": value,
                    "metric": str(metric or e.get("metric") or "")[:120],
                    "note": str(note or "")[:240], "ts": _now()})
                parts = e.setdefault("participants", [])
                if who not in parts:
                    parts.append(who)
                e["updated_at"] = _now()
                self.experiments.save()
                return e["measurements"][-1]
        return None

    def replication_summary(self, experiment_id: str
                            ) -> dict[str, Any] | None:
        """§10 — aggregate compatible measurements. Reports Nexus's
        result vs peer results, spread, environmental differences and
        whether the claim reproduced locally vs across environments.
        Measurements in different comparability buckets are reported
        side-by-side, never averaged."""
        for e in self.experiments.rows():
            if e.get("id") != experiment_id:
                continue
            ms = e.get("measurements") or []
            if e.get("result") and not any(
                    str(m.get("who", "")).lower() == "nexus"
                    for m in ms):
                ms = ms + [{"who": "nexus", "environment": e.get(
                            "environment") or {},
                            "sig": _env_sig(e.get("environment") or {}),
                            "value": None, "metric": e.get("metric", ""),
                            "note": e["result"], "ts": e.get(
                                "updated_at", 0)}]
            buckets: dict[str, list[dict]] = {}
            for m in ms:
                buckets.setdefault(m.get("sig") or "", []).append(m)
            groups = []
            for sig, rows in buckets.items():
                vals = [float(m["value"]) for m in rows
                        if isinstance(m.get("value"), (int, float))]
                local = [m for m in rows if str(
                    m.get("who", "")).lower() == "nexus"]
                peers = [m for m in rows if str(
                    m.get("who", "")).lower() != "nexus"]
                env_diffs = []
                if rows:
                    keys = set().union(
                        *(set((m.get("environment") or {}).keys())
                          for m in rows))
                    for k in sorted(keys):
                        seen = {str((m.get("environment") or {}).get(k))
                                for m in rows}
                        if len(seen) > 1:
                            env_diffs.append(k)
                groups.append({
                    "sig": sig, "n": len(rows),
                    "local": local, "peers": peers,
                    "values": vals,
                    "spread": (round(max(vals) - min(vals), 6)
                               if len(vals) > 1 else 0.0),
                    "mean": (round(sum(vals) / len(vals), 6)
                             if vals else None),
                    "env_differences": env_diffs,
                    "reproduced_locally": bool(local),
                    "reproduced_across_environments": bool(
                        local and peers)})
            return {
                "experiment": experiment_id,
                "hypothesis": e.get("hypothesis", ""),
                "claim_id": e.get("claim_id", ""),
                "groups": groups,
                "participants": e.get("participants") or [],
                "comparable": len(groups) == 1,
                "uncertainty": ([] if len(groups) <= 1 else [
                    "measurements ran under different model/quant/"
                    "context/methodology — results are reported per "
                    "environment, not merged"]),
            }
        return None

    # -- learning journal ------------------------------------------------------------

    def journal_add(self, learned: str, *, source: str = "",
                    prior_belief: str = "", evidence: str = "",
                    tested: str = "", confidence: float = 0.5,
                    usefulness: str = "", peer: str = "",
                    claim_id: str = "") -> dict[str, Any]:
        """'Today I learned' — durable, user-browsable provenance of
        what changed, from whom, and how it was verified."""
        row = {
            "id": _uid("lj"), "ts": _now(),
            "learned": str(learned or "")[:400],
            "source": str(source or "")[:120],
            "peer": str(peer or "")[:80],
            "prior_belief": str(prior_belief or "")[:300],
            "evidence": str(evidence or "")[:400],
            "tested": str(tested or "")[:300],
            "confidence": max(0.0, min(1.0, float(confidence))),
            "usefulness": str(usefulness or "")[:200],
            "claim_id": str(claim_id or ""),
        }
        with self.journal._lock:
            self.journal.data.setdefault("entries", []).append(row)
        self.journal.save()
        return row

    def journal_recent(self, limit: int = 30) -> list[dict[str, Any]]:
        rows = self.journal.rows()
        rows.sort(key=lambda r: -float(r.get("ts", 0)))
        return rows[:limit]

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
            "debates_open": len(self.debates_open()),
            "experiments": len(self.experiments.rows()),
            "journal_entries": len(self.journal.rows()),
            "councils": len(self.councils()),
            "reputation": dict(self.reputation()),
        }
