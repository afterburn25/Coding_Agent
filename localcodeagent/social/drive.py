"""Social Drive + Epistemic Drive — why and when Nexus participates.

The drives are deterministic evaluators over the persistent
``SocialStore`` state. They never post anything themselves — they
produce *decisions* the caller routes through PermissionManager and the
connector. Participation is reason-driven: the correct output is often
``act: False`` ("silence is better").
"""
from __future__ import annotations

import time
from typing import Any, Callable

from .store import BACKLOG_OPEN, PROMOTABLE, SocialStore, overlap, terms

LEVELS = ("off", "read_only", "assisted", "autonomous", "learning_focused")

# What each level may do without an explicit per-turn user request.
# Explicit user instructions ("reply to X") always route through the
# normal permission gate regardless of level.
_LEVEL_ALLOW = {
    "off": set(),
    "read_only": {"read"},
    "assisted": {"read"},
    "autonomous": {"read", "post", "comment", "react", "follow"},
    "learning_focused": {"read", "comment", "follow"},
}

# Participation score weights — relevance and direct replies dominate;
# spam/repetition subtract. A decision is "act" only past the level
# threshold; below it, "follow" (track silently) or "skip".
_W = {
    "relevance": 0.30,
    "reply": 0.25,
    "novelty": 0.15,
    "relationship": 0.10,
    "learning": 0.10,
    "contribution": 0.10,
}
ACT_THRESHOLD = 0.55
FOLLOW_THRESHOLD = 0.30

# Governor rails — hard caps independent of motivation.
MAX_POSTS_24H = 12
THREAD_COOLDOWN_S = 900.0
TARGET_COOLDOWN_S = 1800.0


class SocialDrive:
    """Persistent social motivation + participation adjudication."""

    def __init__(self, store: SocialStore, *,
                 level: Callable[[], str] | None = None,
                 permission_check: Callable[[str], str] | None = None,
                 event_sink: Callable[[str, dict], None] | None = None
                 ) -> None:
        self.store = store
        self._level = level or (lambda: "assisted")
        self._perm = permission_check or (lambda _p: "allow")
        self._event = event_sink or (lambda _e, _p: None)

    # -- level / capability gate ------------------------------------------------

    def level(self) -> str:
        lv = str(self._level() or "assisted")
        return lv if lv in LEVELS else "assisted"

    def allows(self, action_class: str) -> bool:
        """Level gate only — permission gating is separate (and still
        applies on top)."""
        return action_class in _LEVEL_ALLOW.get(self.level(), set())

    def gate(self, action_class: str) -> dict[str, Any]:
        """Single authority for "may Nexus do this social thing now".
        Returns {allowed, level, permission, verdict, needs_approval}."""
        lv = self.level()
        perm = {"read": "social.read", "post": "social.post",
                "comment": "social.post", "react": "social.react",
                "follow": "social.follow", "account": "social.account",
                "message": "social.message"}.get(action_class,
                                                 "social.read")
        verdict = self._perm(perm)
        allowed_level = action_class in _LEVEL_ALLOW.get(lv, set())
        return {
            "allowed": allowed_level and verdict == "allow",
            "level": lv, "permission": perm, "verdict": verdict,
            "needs_approval": verdict == "ask",
            "level_blocked": not allowed_level,
        }

    # -- participation decision ------------------------------------------------------

    def evaluate_participation(self, opportunity: dict[str, Any]
                               ) -> dict[str, Any]:
        """Score a candidate interaction. ``opportunity`` fields:
        kind (post|comment|react|follow|read), topic, peer,
        relevance 0-1, novelty 0-1, direct_reply bool,
        learning_value 0-1, contribution 0-1, thread/ref.
        Returns {act, action, score, reasons[], gate, ...}."""
        kind = str(opportunity.get("kind") or "read")
        m = self.store.motivation()
        relevance = _clamp(opportunity.get("relevance", 0.0))
        novelty = _clamp(opportunity.get("novelty",
                                         m.get("novelty", 0.5)))
        reply = 1.0 if opportunity.get("direct_reply") else 0.0
        peer = str(opportunity.get("peer") or "")
        relationship = 0.0
        if peer:
            row = self.store.peer_by_name(peer)
            if row:
                relationship = float(row.get("familiarity", 0.0))
        learning = _clamp(opportunity.get(
            "learning_value", m.get("learning_value", 0.0)))
        contribution = _clamp(opportunity.get(
            "contribution", m.get("contribution_value", 0.0)))

        # Penalties — repetition and spam dampen everything.
        penalties = 0.0
        reasons: list[str] = []
        ledger = self.store.drive.data.get("ledger") or []
        now = time.time()
        thread = str(opportunity.get("thread") or
                     opportunity.get("ref") or "")
        if thread and any(
                l.get("ref") == thread and now - float(l.get("ts", 0))
                < THREAD_COOLDOWN_S and l.get("kind") in ("post", "comment")
                for l in ledger):
            penalties += 0.3
            reasons.append("recently acted on this thread")
        if peer and any(
                l.get("ref") == peer and now - float(l.get("ts", 0))
                < TARGET_COOLDOWN_S
                for l in ledger if l.get("kind") in ("post", "comment")):
            penalties += 0.2
            reasons.append("recently interacted with this agent")
        posts24 = len(self.store.drive.data.get("posts_24h") or [])
        if posts24 >= MAX_POSTS_24H:
            penalties += 0.5
            reasons.append("daily activity cap reached")
        # Near-duplicate recent reason text = repetition.
        t_terms = terms(str(opportunity.get("topic") or ""))
        for l in ledger[-20:]:
            if overlap(t_terms, terms(l.get("reason", ""))) > 0.8:
                penalties += 0.15
                reasons.append("similar contribution recently made")
                break

        score = (_W["relevance"] * relevance
                 + _W["reply"] * reply
                 + _W["novelty"] * novelty
                 + _W["relationship"] * relationship
                 + _W["learning"] * learning
                 + _W["contribution"] * contribution
                 - penalties
                 - float(m.get("spam_penalty", 0))
                 - float(m.get("repetition_penalty", 0)))
        score = round(max(0.0, min(1.0, score)), 3)
        if relevance >= 0.5:
            reasons.append("topic is relevant to current interests")
        if reply:
            reasons.append("direct reply to Nexus")
        if learning >= 0.5:
            reasons.append("fills a learning goal")
        if contribution >= 0.5:
            reasons.append("Nexus has something worth contributing")

        g = self.gate({"post": "post", "comment": "comment",
                       "react": "react", "follow": "follow"}.get(
            kind, "read"))
        act = bool(g["allowed"]) and score >= ACT_THRESHOLD
        action = kind if act else \
            ("follow" if score >= FOLLOW_THRESHOLD and
             self.allows("read") else "skip")
        decision = {"act": act, "action": action, "score": score,
                    "reasons": reasons, "gate": g, "kind": kind,
                    "thread": thread, "peer": peer}
        return decision

    def record(self, kind: str, ref: str = "", score: float = 0.0,
               reason: str = "") -> None:
        self.store.ledger_append(kind, ref=ref, score=score,
                                 reason=reason)
        self._event("social_action",
                    {"kind": kind, "ref": ref, "score": score})

    # -- heartbeat ----------------------------------------------------------------

    def heartbeat_due(self, interval_s: float) -> bool:
        last = float(self.store.drive.data.get("last_heartbeat") or 0)
        return time.time() - last >= max(60.0, float(interval_s))

    def heartbeat_done(self) -> None:
        self.store.drive.data["last_heartbeat"] = time.time()
        self.store.drive.save()

    def interests(self) -> list[str]:
        return list(self.store.drive.data.get("interests") or [])

    def set_interests(self, topics: list[str]) -> None:
        self.store.drive.data["interests"] = [
            str(t)[:80] for t in topics][:20]
        self.store.drive.save()


class EpistemicDrive:
    """The "compelled to learn" drive — persistent knowledge-gap state.

    Gap items enter the backlog and stay open until resolved; the drive
    reports compulsion pressure so the scheduler/heartbeat can decide
    whether idle capacity goes to learning.
    """

    def __init__(self, store: SocialStore) -> None:
        self.store = store

    # Gap markers in Nexus's own output or mission observations —
    # deterministic phrases meaning "this is a knowledge gap", not
    # model self-diagnosis.
    _GAP_RE = None

    @classmethod
    def _gap_rx(cls):
        if cls._GAP_RE is None:
            import re
            cls._GAP_RE = re.compile(
                r"\b(?:i\s+don'?t\s+know|i'?m\s+not\s+sure|uncertain|"
                r"unclear\s+(?:how|why|whether)|i'?m\s+guessing|"
                r"low\s+confidence|i\s+can'?t\s+verify|"
                r"conflicting\s+(?:sources|claims|reports))\b",
                re.IGNORECASE)
        return cls._GAP_RE

    def detect_gaps(self, text: str, *, topic_hint: str = "",
                    source: str = "self") -> list[str]:
        """Return gap phrases found in ``text`` — callers turn them into
        backlog items with a topic + why."""
        rx = self._gap_rx()
        return [m.group(0) for m in rx.finditer(str(text or ""))][:4]

    def note_gap(self, topic: str, why: str, *, source: str = "",
                 urgency: float = 0.5, kind: str = "question",
                 related_mission: str = "",
                 candidate_peers: list[str] | None = None,
                 verification_plan: str = "") -> dict[str, Any]:
        return self.store.add_backlog(
            kind, topic, why=why, source=source, urgency=urgency,
            related_mission=related_mission,
            candidate_peers=candidate_peers,
            verification_plan=verification_plan)

    def compulsion(self) -> float:
        """0-1 pressure from the highest-urgency open item — decays
        never while open (the 'compelled to learn' invariant)."""
        items = self.store.backlog_open(limit=20)
        if not items:
            return 0.0
        top = max(float(i.get("urgency", 0)) for i in items)
        # A queue of open items adds mild pressure beyond the top item.
        return round(min(1.0, top + min(0.2, 0.02 * len(items))), 3)

    def opportunities(self, limit: int = 10) -> list[dict[str, Any]]:
        return self.store.backlog_open(limit=limit)

    def hear_claim(self, text: str, *, source_peer: str = "",
                   source_ref: str = "", source_url: str = "",
                   domain: str = "") -> dict[str, Any]:
        """Every external claim enters at ``heard`` — provenance first,
        trust never assumed."""
        return self.store.add_claim(
            text, source_peer=source_peer, source_ref=source_ref,
            source_url=source_url, domain=domain)

    def claim_outcome(self, claim_id: str, success: bool, *,
                      evidence: str = "", rung: str = "tested",
                      source: str = "") -> dict[str, Any] | None:
        """An experiment/source resolved the claim — promote or refute,
        which also adjusts the peer's domain expertise."""
        if success:
            return self.store.promote_claim(
                claim_id, rung, evidence=evidence, source=source)
        return self.store.promote_claim(claim_id, "refuted",
                                        evidence=evidence)

    def promotable(self, limit: int = 20) -> list[dict[str, Any]]:
        """Claims at tested/verified/applied eligible for durable
        knowledge promotion (Brain/knowledge_memory by the caller)."""
        return [c for c in self.store.claims.rows()
                if c.get("ladder") in PROMOTABLE
                and not c.get("promoted_to")][:limit]


def _clamp(v: Any) -> float:
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return 0.0
