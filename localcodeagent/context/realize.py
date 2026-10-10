"""Persona-aware surface realization — same facts, different words.

SemanticResponse carries WHAT must be communicated; rendering decides
HOW it is said. Variation lives inside the persona's style envelope —
facts, numbers, identifiers, permissions, and safety decisions never
mutate (§43–§46).

The ledger fingerprints each realization; a near-duplicate of a recent
reply is rerendered rather than repeated (§31–§32, §50).
"""
from __future__ import annotations

import hashlib
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any

from ..personality.genome import (
    SERIOUS_ACTS, SPEECH_ACTS, derive_genome)


def _norm_words(text: str) -> list[str]:
    return re.sub(r"[^a-z0-9\s']", " ", str(text or "").lower()).split()


def fingerprint(text: str) -> str:
    """Stable surface fingerprint — the sentence-pattern signature used
    for duplicate detection, not a semantic hash."""
    return hashlib.sha1(_norm_text(text).encode()).hexdigest()[:16]


def _norm_text(text: str) -> str:
    return " ".join(_norm_words(text))


def similarity(a: str, b: str) -> float:
    """Lexical similarity — Jaccard over normalized word sets plus a
    length factor so a short canned line can't hide inside a long one."""
    wa, wb = set(_norm_words(a)), set(_norm_words(b))
    if not wa or not wb:
        return 0.0
    jaccard = len(wa & wb) / len(wa | wb)
    size = min(len(wa), len(wb)) / max(len(wa), len(wb))
    return jaccard * (0.5 + 0.5 * size)


def opening_of(text: str) -> str:
    """First clause — the opening-style signature for rotation checks."""
    first = re.split(r"[.!?\n]", str(text or "").strip(), maxsplit=1)[0]
    return " ".join(_norm_words(first)[:6])


def closing_of(text: str) -> str:
    last = re.split(r"[.!?\n]", str(text or "").strip())[-1].strip()
    if not last:
        parts = [p for p in re.split(r"[.!?\n]", str(text or "")) if p.strip()]
        last = parts[-1].strip() if parts else ""
    return " ".join(_norm_words(last)[-6:])


@dataclass
class SemanticResponse:
    """WHAT must be communicated — fact slots, never prose.

    The fields beyond the original five are additive and optional: old
    callers keep working; the genome renderer reads the richer ones
    when present."""
    facts: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    actions_completed: list[str] = field(default_factory=list)
    next_options: list[str] = field(default_factory=list)
    error_code: str = ""
    # -- speech-genome extension ---------------------------------------
    semantic_id: str = ""             # dedupe key for repeat evolution
    speech_act: str = ""              # one of SPEECH_ACTS; "" → "answer"
    # verified | likely | inferred | uncertain — REAL confidence set
    # upstream; the renderer only picks the phrasing for it.
    confidence: str = "verified"
    conclusions: list[str] = field(default_factory=list)
    uncertainty: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    actions_failed: list[str] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)
    # Spans inside facts that MUST survive verbatim (identifiers,
    # numbers, code, canonical facts). The renderer marks them for the
    # delivery plan's emphasis and never alters them.
    exact_spans: list[str] = field(default_factory=list)
    register: str = ""                # one of REGISTERS; "" → "casual"
    # Optional pre-built MeaningFrame (context/realization.py). When
    # present the renderer realizes the BODY from atoms — canonical is
    # treated as meaning, not wording.
    frame: Any = None
    canonical: str = ""
    # Scope contract (context/scope.py): a minimum-sufficient answer —
    # the genome envelope (micro-reactions, acknowledgement openers,
    # closings, address) must not decorate it. The BODY still realizes
    # fresh through the MeaningFrame so wording varies; nothing is
    # prepended or appended around the requested fact.
    bare: bool = False


@dataclass
class RenderContext:
    """Situation state for one render — downstream of dynamics/social.
    Everything here is *how the moment feels*, never *what is true*."""
    mood: str = "relaxed"
    mood_intensity: float = 0.0
    seriousness: int = 0              # 0 casual .. 3 critical
    register: str = "casual"
    relationship_stage: str = "acquaintance"
    familiarity: float = 0.0
    social_cue: str = ""              # frustrated/celebrating/confused…
    sarcasm: bool = False
    user_energy: str = "neutral"      # low | neutral | high
    address: str = ""                 # preferred form of address or ""
    creator: bool = False             # speaker is the verified creator
    # Expression fatigue (0..1) from the dynamics saturation ledger —
    # heavy recent humor/gesture output eases quips and micro-reactions
    # off so strong personas get neutral moments instead of caricaturing.
    saturation: float = 0.0
    # Per-style humor ledger {style: {pos, neg}} — the same data the
    # prompt card renders as adaptation notes; the renderer consumes it
    # structurally to down-weight categories this user dislikes and
    # lean into the ones that land.
    humor_feedback: dict = field(default_factory=dict)


@dataclass
class SpeechDeliveryPlan:
    """Prosody metadata for the voice layer — travels WITH the text so
    TTS delivery matches the persona's surface style. Every field is a
    hint the voice engine may clamp or ignore; nothing here is
    fabricated capability — voice/manager maps what it supports."""
    pace: float = 1.0                 # multiplier over preset speed
    energy: float = 0.5               # 0 subdued .. 1 animated
    warmth: float = 0.5
    emphasis_level: float = 0.5
    emphasis_spans: list[str] = field(default_factory=list)
    pause_hint: float = 0.4           # pause density bias
    seriousness: int = 0
    speech_act: str = "answer"
    register: str = "casual"
    nonverbal_rate: float = 0.0
    sarcasm: bool = False

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in (
            "pace", "energy", "warmth", "emphasis_level",
            "emphasis_spans", "pause_hint", "seriousness",
            "speech_act", "register", "nonverbal_rate", "sarcasm")}


@dataclass
class RenderedReply:
    """One realized surface + its delivery plan + observability."""
    text: str
    plan: SpeechDeliveryPlan = field(default_factory=SpeechDeliveryPlan)
    speech_act: str = "answer"
    repeat_index: int = 0             # times this semantic_id rendered
    opening_family: str = ""
    closing_family: str = ""
    micro_reaction: str = ""
    used_address: bool = False
    # True when a real speech genome shaped this reply (False = the
    # neutral default genome or the no-persona legacy lane).
    genome_rendered: bool = False
    # Realization observability (§37): the realized BODY without wrapper,
    # its structure signature, and how it was produced.
    body: str = ""
    structure_signature: str = ""
    realization: dict = field(default_factory=dict)
    # Self-knowledge lane UI payload — action cards, deep links, and
    # inline controls the frontend renders under the message.
    ui: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "text": self.text, "plan": self.plan.as_dict(),
            "speech_act": self.speech_act,
            "repeat_index": self.repeat_index,
            "opening_family": self.opening_family,
            "closing_family": self.closing_family,
            "micro_reaction": self.micro_reaction,
            "used_address": self.used_address,
            "genome_rendered": self.genome_rendered,
            "body": self.body,
            "structure_signature": self.structure_signature,
            "realization": dict(self.realization),
            "ui": dict(self.ui),
        }


@dataclass
class ResponseFingerprint:
    intent: str
    opening: str
    closing: str
    text_hash: str
    ts: float
    semantic_id: str = ""
    structure: str = ""
    # Normalized body surface — needed so cross-restart similarity checks
    # still have real text to compare (bounded, normalized words only).
    norm_text: str = ""


class ResponseLedger:
    """Rolling bounded history of realizations — the repetition memory
    (§41). Cooldowns: an opening/closing/pattern just used can't be
    picked again until enough different ones intervene.

    Per-semantic history feeds the similarity gate: the same semantic_id
    may not recur with the same wording, opening, closing, or structure.
    Optional JSONL persistence means a restart doesn't erase repetition
    awareness — rows store surface metadata (hash, signatures, bounded
    normalized text), never reasoning.
    """

    def __init__(self, maxlen: int = 24, persist_path: str = ""):
        self._rows: list[ResponseFingerprint] = []
        self._by_semantic: dict[str, list[ResponseFingerprint]] = {}
        self._maxlen = maxlen
        # Per-semantic history must cover the whole soak window —
        # exact-dup rejection compares against this full set, so a cap
        # of 10 lets bodies recur after 10 renders (§16).
        self._per_id_max = 64
        self._persist_path = ""
        if persist_path:
            self.attach(persist_path)

    # -- persistence ---------------------------------------------------

    def attach(self, path: str) -> None:
        """Enable JSONL persistence — load existing rows, append future
        ones. Failures degrade to in-memory only."""
        from pathlib import Path
        import json
        self._persist_path = str(path)
        try:
            p = Path(path)
            if not p.exists():
                return
            for line in p.read_text(encoding="utf-8").splitlines()[-400:]:
                try:
                    row = json.loads(line)
                    fp = ResponseFingerprint(
                        intent=row.get("intent", ""),
                        opening=row.get("opening", ""),
                        closing=row.get("closing", ""),
                        text_hash=row.get("text_hash", ""),
                        ts=float(row.get("ts") or 0),
                        semantic_id=row.get("semantic_id", ""),
                        structure=row.get("structure", ""),
                        norm_text=row.get("norm_text", "")[:400])
                    self._rows.append(fp)
                    if fp.semantic_id:
                        self._by_semantic.setdefault(
                            fp.semantic_id, []).append(fp)
                except Exception:
                    continue
            self._rows = self._rows[-self._maxlen:]
            for sid, rows in self._by_semantic.items():
                self._by_semantic[sid] = rows[-self._per_id_max:]
        except OSError:
            pass

    def _persist(self, fp: ResponseFingerprint) -> None:
        if not self._persist_path:
            return
        import json
        from pathlib import Path
        try:
            p = Path(self._persist_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "intent": fp.intent, "opening": fp.opening,
                    "closing": fp.closing, "text_hash": fp.text_hash,
                    "ts": fp.ts, "semantic_id": fp.semantic_id,
                    "structure": fp.structure,
                    "norm_text": fp.norm_text[:400]}) + "\n")
        except OSError:
            pass

    # -- recording -------------------------------------------------------

    def record(self, intent: str, text: str, *,
               semantic_id: str = "", structure: str = "",
               body: str = "") -> ResponseFingerprint:
        from .realization import structure_signature, text_fingerprint
        fp = ResponseFingerprint(
            intent=intent,
            opening=opening_of(text),
            closing=closing_of(text),
            text_hash=fingerprint(text),
            ts=time.time(),
            semantic_id=semantic_id,
            structure=structure or structure_signature(body or text),
            norm_text=_norm_text(body or text)[:400],
        )
        self._rows.append(fp)
        self._rows = self._rows[-self._maxlen:]
        if semantic_id:
            hist = self._by_semantic.setdefault(semantic_id, [])
            hist.append(fp)
            self._by_semantic[semantic_id] = hist[-self._per_id_max:]
        self._persist(fp)
        return fp

    # -- per-semantic history ---------------------------------------------

    def recent_bodies(self, semantic_id: str, n: int = 5) -> list[str]:
        """Normalized bodies of the last N renders of one semantic_id —
        the similarity gate's comparison set."""
        return [r.norm_text for r in
                self._by_semantic.get(semantic_id, [])[-n:]
                if r.norm_text]

    def seen_bodies(self, semantic_id: str) -> set[str]:
        """Every normalized body ever rendered for this semantic_id —
        exact-duplicate checks compare against the full history, not
        just the recent window (§16)."""
        return {r.norm_text for r in
                self._by_semantic.get(semantic_id, []) if r.norm_text}

    def recent_structures(self, semantic_id: str, n: int = 3) -> list[str]:
        return [r.structure for r in
                self._by_semantic.get(semantic_id, [])[-n:]
                if r.structure]

    def repeat_index(self, semantic_id: str) -> int:
        return len(self._by_semantic.get(semantic_id, []))

    # -- existing whole-history surfaces -----------------------------------

    def repetition_score(self, text: str, *, intent: str = "") -> dict[str, Any]:
        """Observable repetition metric (§50) — diagnostics metadata,
        never reasoning."""
        cand_hash = fingerprint(text)
        recent = self._rows[-8:]
        scores = {
            "exact_duplicate": any(r.text_hash == cand_hash for r in recent),
            "max_lexical_similarity": 0.0,
            "opening_reuse": False,
            "closing_reuse": False,
            "recent_count": len(recent),
        }
        op, cl = opening_of(text), closing_of(text)
        for r in recent:
            if r.opening and r.opening == op:
                scores["opening_reuse"] = True
            if r.closing and r.closing == cl:
                scores["closing_reuse"] = True
        return scores

    def recent_openings(self, n: int = 6) -> list[str]:
        return [r.opening for r in self._rows[-n:] if r.opening]

    def recent_closings(self, n: int = 6) -> list[str]:
        return [r.closing for r in self._rows[-n:] if r.closing]


# ---------------------------------------------------------------------------
# Speech-act classification — deterministic, upstream of rendering (§5).
# Intent + outcome + seriousness are the inputs; prose is never re-parsed.
# ---------------------------------------------------------------------------

# canned/intent ids → act. Ordered evaluation below handles outcomes.
_INTENT_ACTS = {
    "greeting": "greet", "farewell": "farewell", "goodbye": "farewell",
    "identity": "answer", "capability": "answer", "self_learning": "answer",
    "status": "answer", "answer_memory": "answer", "time": "answer",
    "date": "answer",
    "image_generation": "report_success",
    "image_edit": "report_success",
    "code_fix": "troubleshoot", "code_write": "explain",
    "explanation": "explain", "teaching": "teach", "tutorial": "teach",
    "correction": "correct", "disagreement": "disagree",
    "warning": "warn", "error": "diagnose", "diagnostic": "diagnose",
    "question": "answer", "question_factual": "answer",
    "recommendation": "recommend", "comparison": "compare",
    "summary": "summarize", "summarize": "summarize",
    "brainstorm": "brainstorm", "plan": "brainstorm", "planning": "brainstorm",
    "review": "summarize", "handoff": "handoff",
    "refusal": "deny", "clarification_needed": "request_clarification",
    "joke": "joke", "tease": "tease", "story": "storytell",
    "apology": "apologize", "uncertainty": "admit_uncertainty",
}

# user social cue → act override (a frustrated user gets reassure-shaped
# delivery even when the semantic act is "answer").
_CUE_ACTS = {
    "frustrated": "reassure", "venting": "reassure",
    "celebrating": "celebrate", "excited": "celebrate",
    "confused": "clarify", "uncertain": "reassure",
    "joking": "joke", "teasing": "tease",
}


def classify_speech_act(intent: str = "", *, outcome: str = "",
                        seriousness: int = 0, cue: str = "",
                        user_text: str = "") -> str:
    """Map intent + outcome + situation → one of SPEECH_ACTS.

    ``outcome`` is the upstream result state: "ok", "failed",
    "uncertain", "denied", "warning". ``seriousness`` 0-3 comes from the
    existing seriousness classifier; ``cue`` is the social-cue label
    from personality.social."""
    intent = str(intent or "").strip().lower()
    outcome = str(outcome or "").strip().lower()
    cue = str(cue or "").strip().lower()

    # Critical context beats everything — a warning is a warning.
    if seriousness >= 3:
        return "alert" if outcome in {"failed", "denied", "warning"} \
            or intent in _INTENT_ACTS else "alert"
    if outcome == "warning" or intent in {"warning"}:
        return "warn"
    if outcome == "denied":
        return "deny"
    if outcome == "failed":
        return "report_failure"
    if outcome == "uncertain":
        return "admit_uncertainty"
    # Social cue shapes delivery for non-canned acts — but a real
    # greeting/farewell/canned lane keeps its own act.
    canned = intent in {"greeting", "farewell", "goodbye", "identity",
                        "capability", "self_learning", "status",
                        "answer_memory", "time", "date"}
    if not canned and cue in _CUE_ACTS:
        return _CUE_ACTS[cue]
    if outcome == "ok" and intent in _INTENT_ACTS:
        return _INTENT_ACTS[intent]
    if intent in _INTENT_ACTS:
        return _INTENT_ACTS[intent]
    if outcome == "ok":
        return "report_success" if intent in {
            "task_complete", "fix_applied", "image_generation",
            "image_edit"} else "answer"
    return "answer"


class PhraseCooldowns:
    """Per-category recent-phrase memory (§18-22) — openings, closings,
    micro-reactions, address terms, and acknowledgements each cool
    independently so one hot phrase can't dominate a session."""

    def __init__(self):
        self._used: dict[str, list[tuple[str, int]]] = {}
        self._turn = 0

    def tick(self) -> None:
        self._turn += 1

    def cooled(self, category: str, cooldown: int) -> list[str]:
        """Phrases in this category used within ``cooldown`` turns."""
        return [p for p, t in self._used.get(category, [])
                if self._turn - t < cooldown]

    def is_cooled(self, category: str, phrase: str, cooldown: int) -> bool:
        return phrase in self.cooled(category, cooldown)

    def mark(self, category: str, phrase: str) -> None:
        if not phrase:
            return
        self._used.setdefault(category, []).append((phrase, self._turn))
        self._used[category] = self._used[category][-16:]

    @property
    def turn(self) -> int:
        return self._turn


class PersonaRenderer:
    """Variant-bank realization for deterministic content — fast lane
    (§38): no model call to say "GitHub is connected" a different way.

    Selection: rotating cursor biased away from fingerprints in the
    ledger — same fact, new sentence shape, still one voice.
    """

    def __init__(self, ledger: ResponseLedger | None = None,
                 rng: random.Random | None = None):
        self.ledger = ledger or ResponseLedger()
        self._cursor = 0
        self._rng = rng or random.Random()
        self.cooldowns = PhraseCooldowns()
        self._repeat_idx: dict[str, int] = {}
        from .realization import TierA
        self._realizer = TierA(self._rng)
        self._last_frame: Any = None   # for "say that differently"

    def render(self, semantic_id: str, variants: list[str] | tuple[str, ...],
               *, intent: str = "") -> str:
        """Pick the least-recently-used variant that isn't a near-repeat
        of the last few replies. Falls back to rotation when every
        variant was used recently — the bank's size is the cooldown."""
        pool = [str(v) for v in variants if str(v).strip()]
        if not pool:
            return ""
        if len(pool) == 1:
            return pool[0]
        recent = self.ledger._rows[-max(3, len(pool) - 1):]
        banned = {r.text_hash for r in recent}
        banned_open = {r.opening for r in recent}
        start = self._cursor
        for i in range(len(pool)):
            cand = pool[(start + i) % len(pool)]
            if fingerprint(cand) in banned:
                continue
            if opening_of(cand) in banned_open and i < len(pool) - 1:
                continue
            self._cursor = (start + i + 1) % len(pool)
            return cand
        self._cursor = (start + 1) % len(pool)
        return pool[start % len(pool)]

    def wrap(self, text: str, *, openers: tuple[str, ...] = (),
             intent: str = "") -> str:
        """Stored-answer lane (§37): canonical content passes through
        untouched; only the wrapper varies — no fact drift, no replayed
        identical envelope."""
        text = str(text or "").strip()
        if not text:
            return text
        if not openers:
            return text
        banned = set(self.ledger.recent_openings())
        for i in range(len(openers)):
            cand = openers[(self._cursor + i) % len(openers)]
            if opening_of(cand) not in banned:
                self._cursor += i + 1
                return f"{cand} {text}"
        return text

    # ------------------------------------------------------------------
    # Genome-aware semantic rendering (§1-§26). Facts pass through
    # verbatim; every knob below only shapes the wrapper.
    # ------------------------------------------------------------------

    def render_semantic(self, sem: SemanticResponse,
                        genome: dict | None = None,
                        ctx: RenderContext | None = None,
                        *, intent: str = "",
                        canonical: str = "") -> RenderedReply:
        """Realize one SemanticResponse through a speech genome.

        Pipeline: repeat index → micro-reaction → opening family →
        body (verbatim facts + confidence phrasing) → closing family →
        delivery plan → ledger/cooldown bookkeeping.

        ``canonical`` supplies the authoritative MEANING (canned
        responses, Answer Memory, identity facts): it is decomposed into
        a MeaningFrame and re-realized each call — the fact is immutable,
        the wording is not.
        """
        from .realization import (
            frame_from_canonical, frame_from_semantic, structure_signature,
            too_similar, validate_realization)
        g = genome or derive_genome({})
        ctx = ctx or RenderContext()
        act = sem.speech_act or "answer"
        serious = act in SERIOUS_ACTS or ctx.seriousness >= 2 \
            or ctx.register in ("system_alert",)
        self.cooldowns.tick()

        rep_key = sem.semantic_id or f"{act}:{fingerprint(' '.join(sem.facts))}"
        repeat = max(self._repeat_idx.get(rep_key, 0),
                     self.ledger.repeat_index(rep_key))

        # Scope contract: bare responses carry the requested fact and
        # nothing else — no micro-reaction, no opener, no address, no
        # closing. This is a scope decision made upstream, not a style
        # preference the genome may override.
        bare = bool(getattr(sem, "bare", False))
        if bare:
            open_fam, opening = "direct_answer", ""
        else:
            open_fam, opening = self._pick_opening(g, act, serious, ctx,
                                                   repeat)
        if canonical and act in ("greet", "farewell"):
            # The canonical body already IS the greeting/farewell.
            open_fam, opening = act, ""
        # A reaction-family opening already IS the micro-reaction —
        # stacking both reads as noise.
        micro = "" if bare or open_fam == "reaction" else self._maybe_micro(
            g, act, serious, ctx)
        # Greeting/farewell slot clauses carry their own address
        # placement — the generic address injector would double it.
        address = "" if bare or act in ("greet", "farewell") \
            else self._maybe_address(g, act, ctx)
        canon = canonical or getattr(sem, "canonical", "") or ""
        meta: dict[str, Any] = {"tier": "a", "rerenders": 0,
                                "fallback": ""}
        if canon or getattr(sem, "frame", None) is not None:
            body, body_meta = self._realize_body(
                sem, canon, rep_key, act, serious, repeat, g)
            meta.update(body_meta)
        else:
            body = self._compose_body(sem, g, ctx, serious, repeat)
        if (canon and repeat >= 1 and not serious
                and act not in ("greet", "farewell")):
            ack = self._choose(REPEAT_ACKS, "opening", g)
            body = f"{ack} {body}".strip() if ack else body
        close_fam, closing = (
            ("hard_stop", "") if bare
            else self._pick_closing(g, act, serious, sem, ctx))

        # Order: [micro] [opening] [address] body [closing]
        head = " ".join(x for x in (micro, opening) if x).strip()
        text = " ".join(x for x in (head, address, body, closing)
                        if x).strip()
        text = re.sub(r"\s+([.,!?])", r"\1", text)
        text = re.sub(r"[ ]{2,}", " ", text).strip()

        sig = structure_signature(body)
        meta["structure"] = sig
        self._repeat_idx[rep_key] = repeat + 1
        self.ledger.record(intent or act, text, semantic_id=rep_key,
                           structure=sig, body=body)
        plan = self._delivery_plan(g, sem, act, ctx, serious)
        return RenderedReply(
            text=text, plan=plan, speech_act=act,
            repeat_index=repeat, opening_family=open_fam,
            closing_family=close_fam, micro_reaction=micro,
            used_address=bool(address),
            genome_rendered=genome is not None,
            body=body, structure_signature=sig, realization=meta)

    def _realize_body(self, sem: SemanticResponse, canonical: str,
                      rep_key: str, act: str, serious: bool,
                      repeat: int, g: dict) -> tuple[str, dict]:
        """Canonical → MeaningFrame → SurfacePlan → fresh body.

        Rejection loop: candidates that fail semantic validation or land
        too close to recent same-id renders are re-rolled; last resort
        returns the canonical itself — wording may repeat but truth never
        degrades."""
        from .realization import (
            body_similarity, frame_from_canonical, frame_from_semantic,
            too_similar, validate_realization)
        meta: dict[str, Any] = {"tier": "a", "rerenders": 0,
                                "fallback": ""}
        frame = frame_from_semantic(sem)
        if not (frame.facts or frame.atoms or frame.slots) and canonical:
            extracted = frame_from_canonical(
                canonical, semantic_id=rep_key, speech_act=act)
            extracted.exact_spans = sorted(set(
                extracted.exact_spans) | set(frame.exact_spans))
            extracted.confidence = frame.confidence
            extracted.register = frame.register
            frame = extracted
        if not frame.semantic_id:
            frame.semantic_id = rep_key
        if not frame.speech_act:
            frame.speech_act = act
        self._last_frame = frame
        if not (frame.facts or frame.atoms or frame.slots):
            meta["fallback"] = "empty_frame"
            return canonical.strip(), meta
        recents = self.ledger.recent_bodies(rep_key)
        seen = self.ledger.seen_bodies(rep_key)
        avoid_struct = tuple(
            s.split(":")[0] for s in self.ledger.recent_structures(rep_key))
        norm_c = _norm_text
        best = ""
        best_score = 1e9
        # Slots have a bounded clause space — give them a deep retry
        # budget so exhaustion (canonical echo) is genuinely last-resort.
        attempts = 24 if frame.slots else 10
        for attempt in range(attempts):
            plan = self._realizer.plan(
                frame, repeat=repeat + attempt,
                avoid_structures=avoid_struct)
            cand = self._realizer.realize(frame, plan).strip()
            if not cand:
                meta["rerenders"] += 1
                continue
            violations = validate_realization(cand, frame)
            if violations:
                meta["rerenders"] += 1
                meta.setdefault("violations", []).extend(violations[:3])
                continue
            n_cand = norm_c(cand)
            # Never emit a body this semantic_id has already produced —
            # exhaust retries instead of echoing history.
            if n_cand in seen:
                meta["rerenders"] += 1
                continue
            sim = too_similar(cand, recents)
            if not sim:
                return cand, meta
            meta["rerenders"] += 1
            # Keep the least-similar valid candidate as the exhaustion
            # fallback — not the canonical, which would be a guaranteed
            # repeat.
            score = max(
                (sum(1 for v in vs.values() if v)
                 for vs in
                 (body_similarity(cand, p) for p in recents)),
                default=0)
            if score < best_score:
                best, best_score = cand, score
        if best:
            meta["fallback"] = "similarity_exhausted"
            return best, meta
        meta["fallback"] = "canonical"
        return canonical.strip(), meta

    def rephrase_last(self, *, avoid_structure: str = "") -> str:
        """"Say that differently" — re-realize the last MeaningFrame with
        elevated novelty pressure. Returns "" when nothing rephraseable
        was rendered (a model reply, not a semantic one)."""
        from .realization import structure_signature
        frame = self._last_frame
        if frame is None:
            return ""
        recents = self.ledger.recent_bodies(frame.semantic_id)
        avoid = tuple(s.split(":")[0] for s in
                      self.ledger.recent_structures(frame.semantic_id))
        if avoid_structure:
            avoid = avoid + (avoid_structure,)
        seen = self.ledger.seen_bodies(frame.semantic_id)
        for attempt in range(12):
            plan = self._realizer.plan(
                frame, repeat=10 + attempt, avoid_structures=avoid)
            cand = self._realizer.realize(frame, plan).strip()
            if cand and _norm_text(cand) not in seen:
                self.ledger.record(
                    frame.speech_act or "answer", cand,
                    semantic_id=frame.semantic_id,
                    structure=structure_signature(cand), body=cand)
                return cand
        return ""

    # -- act-shaped opening families -----------------------------------

    # Which opening families fit each act (weights still come from the
    # genome; this is the fit mask — a failure never opens "reaction").
    _ACT_OPENINGS = {
        "greet": ("greeting",),
        "farewell": ("farewell",),
        "report_success": ("result_first", "reaction", "direct_answer",
                           "acknowledgement"),
        "report_failure": ("result_first", "direct_answer",
                           "observation"),
        "warn": ("direct_answer", "observation"),
        "alert": ("direct_answer",),
        "deny": ("direct_answer", "brief_confirmation"),
        "disagree": ("direct_answer", "observation", "acknowledgement"),
        "correct": ("direct_answer", "acknowledgement"),
        "apologize": ("acknowledgement", "direct_answer"),
        "admit_uncertainty": ("direct_answer", "observation"),
        "reassure": ("acknowledgement", "direct_answer"),
        "celebrate": ("reaction", "result_first"),
        "congratulate": ("reaction", "direct_answer"),
        "joke": ("reaction", "direct_answer"),
        "tease": ("reaction", "observation"),
        "request_clarification": ("direct_answer", "acknowledgement"),
        "ask_question": ("direct_answer", "observation"),
    }
    # "result_first" is deliberately absent: success/error terms only
    # fit outcome acts — "Done — you're welcome." before a plain answer
    # reads as a non sequitur.
    _DEFAULT_OPENINGS = ("direct_answer", "acknowledgement",
                         "observation", "reaction",
                         "brief_confirmation", "context_callback",
                         "technical_summary")

    # Generic stems for families the genome vocabulary doesn't supply.
    _OPENING_STEMS = {
        "observation": ("Quick look —", "One thing —", "Noting —",
                        "Heads-up —"),
        "reaction": ("Oh —", "Ah —", "Well —"),
        "brief_confirmation": ("Sure —", "Right —", "Yep —"),
        "context_callback": ("Back on that —", "Still on that —",
                             "Following up —"),
        "technical_summary": ("Status —", "Short version —",
                              "Readout —"),
        "greeting": ("Hey.", "Hi.", "Hey there."),
        "farewell": ("Take care.", "See you.", "Later."),
    }

    def _family_pool(self, fam: str, g: dict, act: str) -> list[str]:
        """Genome vocabulary first, generic stems as fallback — this is
        where personas sound different before a single fact is said."""
        vocab = g.get("vocabulary") or {}
        if fam == "direct_answer":
            return []
        if fam == "acknowledgement":
            return list(vocab.get("acknowledgements") or
                        self._OPENING_STEMS["brief_confirmation"])
        if fam == "result_first":
            if act == "report_failure":
                return list(vocab.get("error_terms") or ["It failed."])
            if act in ("report_success", "celebrate", "congratulate"):
                return list(vocab.get("success_terms") or ["Done."])
            # Non-outcome acts that still landed here get a neutral
            # result-shaped stem, never a success term.
            return ["Short version —", "The gist —", "Straight up —"]
        if fam == "reaction":
            return list(vocab.get("interjections") or
                        self._OPENING_STEMS["reaction"])
        if fam in ("greeting", "farewell"):
            return list(self._OPENING_STEMS[fam])
        return list(self._OPENING_STEMS.get(fam, ()))

    def _pick_opening(self, g: dict, act: str, serious: bool,
                      ctx: RenderContext, repeat: int
                      ) -> tuple[str, str]:
        if act in ("greet", "farewell"):
            fam = act.replace("greet", "greeting")
            pool = self._family_pool(fam, g, act)
            return fam, self._choose(pool, "opening", g)
        fits = self._ACT_OPENINGS.get(act, self._DEFAULT_OPENINGS)
        weights = dict(g.get("pragmatics", {}).get("opening_weights")
                       or {})
        # Seriousness and repeat-index both push toward directness —
        # the third repeat of a fact doesn't get a jaunty opener.
        if serious:
            weights = {"direct_answer": 0.8, "observation": 0.15,
                       "brief_confirmation": 0.05}
        elif repeat >= 2:
            weights = {k: v * (0.3 if k != "direct_answer" else 1.5)
                       for k, v in weights.items()}
        elif ctx.mood == "focused":
            weights["direct_answer"] = weights.get("direct_answer", 0) \
                * 1.5
        fam = self._weighted_pick(weights, fits)
        if fam == "direct_answer":
            return fam, ""
        pool = self._family_pool(fam, g, act)
        pick = self._choose(pool, "opening", g)
        # Separators follow dash_usage — high-dash personas connect with
        # an em-dash, low-dash personas use a full stop.
        if pick and not pick.endswith((".", "!", "?", "—", ":")):
            pick += " —" if (g.get("syntax", {})
                             .get("dash_usage", 0.3) > 0.45) else "."
        return fam, pick

    # -- body ------------------------------------------------------------

    def _compose_body(self, sem: SemanticResponse, g: dict,
                      ctx: RenderContext, serious: bool,
                      repeat: int) -> str:
        parts: list[str] = []
        # Confidence stem precedes the facts ONLY for non-verified
        # content — verified facts never get a random hedge (§26).
        conf = str(sem.confidence or "verified")
        if conf != "verified" and sem.facts:
            stem = str((g.get("confidence") or {}).get(conf) or "")
            if stem:
                parts.append(stem.rstrip(".") + ":")
        for f in sem.facts:
            parts.append(str(f).strip())
        for c in sem.conclusions:
            parts.append(str(c).strip())
        for u in sem.uncertainty:
            parts.append(str(u).strip())
        for w in sem.warnings:
            parts.append("Warning: " + str(w).strip()
                         if not str(w).lower().startswith("warn")
                         else str(w).strip())
        for a in sem.actions_failed:
            parts.append(str(a).strip())
        body = " ".join(p for p in parts if p).strip()

        # Repeat evolution (§21): the 3rd+ replay of the same semantic
        # id compresses to the load-bearing fact instead of a fresh
        # paraphrase — honest repetition, honest shape.
        if repeat >= 3 and sem.facts:
            head = sem.facts[0]
            tag = "" if serious else self._choose(
                ("Unchanged —", "Same as before —", "Still —",
                 "No change —"), "opening", g)
            body = (f"{tag} {head}".strip()) if tag else head
        elif repeat == 2 and sem.facts and not serious:
            lead = self._choose(
                ("Same answer — short version:",
                 "Repeating the key part:",
                 "No change —"),
                "opening", g)
            if len(sem.facts) > 1:
                body = f"{lead} {sem.facts[0]}"
        return body

    # -- closing families ----------------------------------------------

    def _pick_closing(self, g: dict, act: str, serious: bool,
                      sem: SemanticResponse, ctx: RenderContext
                      ) -> tuple[str, str]:
        if act in ("greet", "farewell", "alert", "deny"):
            return "hard_stop", ""
        weights = dict(g.get("pragmatics", {}).get("closing_weights")
                       or {})
        if serious:
            return "hard_stop", ""
        # next_step only when there IS one — never a fabricated offer.
        nexts = sem.next_options or sem.next_steps
        fam = self._weighted_pick(weights, tuple(weights.keys())
                                  or ("hard_stop",))
        if fam == "next_step" and nexts:
            step = str(nexts[0]).rstrip(".")
            return fam, f"Next: {step}." if not step.lower().startswith(
                ("want", "say", "i can")) else step[0].upper() + step[1:] + "."
        # Fabricated-question closers ("Want me to dig deeper?") read as
        # trailing filler after every answer — a real person stops when
        # they've answered. Only a genuine next_step may close.
        if fam == "light_comment":
            q = self._humor_quip(g, ctx)
            return (fam, q) if q else ("hard_stop", "")
        if fam == "short_summary" and len(sem.facts) > 1:
            return fam, "That's the shape of it."
        return "hard_stop", ""

    # Per-humor-category light comments — differentiated so a deadpan
    # persona and a nerdy one don't share one "joke" pool (§16).
    _HUMOR_QUIPS = {
        "dry_humor": ("Thrilling stuff.", "Riveting, I know."),
        "deadpan": ("Thrilling stuff.", "Peak excitement."),
        "sarcasm": ("Try to contain the excitement.",
                    "Living dangerously today."),
        "wit": ("Elegant, if I do say so.", "Satisfyingly tidy."),
        "wordplay": ("That's how the cookie compiles.",
                     "Byte-sized and proud of it."),
        "nerd_humor": ("For the record, that was the fun part.",
                       "The mechanism is the interesting bit."),
        "observational_humor": ("Funny how it's never the part you "
                                "expect.", "Always the last place "
                                "checked."),
        "playful_teasing": ("You knew that already though.",
                            "Don't act surprised."),
        "self_deprecation": ("I do occasionally get things right.",
                             "Even I can manage that much."),
        "absurdity": ("Chaos, but organized chaos.",
                      "Everything is fine, probably."),
        "goofy_humor": ("Boom — nailed it.", "Certified fresh."),
        "dark_humor": ("It lives. For now.", "No casualties today."),
    }

    # Card-level humor_type → genome categories that style expresses.
    # Category names also map to themselves so callers may record
    # either namespace into the feedback ledger.
    _HUMOR_TYPE_CATS = {
        "dry": ("dry_humor", "deadpan"),
        "deadpan": ("deadpan", "dry_humor"),
        "sarcastic": ("sarcasm",),
        "sarcasm": ("sarcasm",),
        "teasing": ("playful_teasing",),
        "mischievous": ("playful_teasing", "absurdity"),
        "nerdy": ("nerd_humor", "wordplay"),
        "playful": ("goofy_humor", "playful_teasing"),
        "warm": ("self_deprecation", "observational_humor"),
        "witty": ("wit", "wordplay"),
        "absurd": ("absurdity", "goofy_humor"),
    }

    def _humor_bias(self, category: str, ctx: RenderContext) -> float:
        """Learned preference multiplier for one humor category —
        same thresholds as continuity.humor_adaptation (neg≥3 and
        neg>2·pos → rare; pos≥3 and pos>2·neg → favored)."""
        fb = ctx.humor_feedback or {}
        bias = 1.0
        for style, cats in self._HUMOR_TYPE_CATS.items():
            if category not in cats:
                continue
            row = fb.get(style) or {}
            try:
                neg, pos = int(row.get("neg") or 0), int(row.get("pos") or 0)
            except (TypeError, ValueError):
                continue
            if neg >= 3 and neg > pos * 2:
                bias = min(bias, 0.25)
            elif pos >= 3 and pos > neg * 2:
                bias = max(bias, 1.5)
        # Category names also count as their own style entry.
        row = fb.get(category) or {}
        try:
            neg, pos = int(row.get("neg") or 0), int(row.get("pos") or 0)
            if neg >= 3 and neg > pos * 2:
                bias = min(bias, 0.25)
            elif pos >= 3 and pos > neg * 2:
                bias = max(bias, 1.5)
        except (TypeError, ValueError):
            pass
        return bias

    def _humor_quip(self, g: dict, ctx: RenderContext) -> str:
        humor = g.get("humor") or {}
        if humor.get("suppress_in_serious", True) and ctx.seriousness:
            return ""
        allowed = set(humor.get("allowed_registers") or ["casual"])
        if ctx.register not in allowed:
            return ""
        damp = 1.0 - 0.5 * max(0.0, min(1.0, ctx.saturation))
        cats = [(k, v) for k, v in
                (humor.get("categories") or {}).items()
                if v.get("strength", 0) > 0 and
                self._rng.random() < (v.get("frequency", 0)
                                      * self._humor_bias(k, ctx)
                                      * damp)]
        if not cats:
            return ""
        cats.sort(key=lambda kv: kv[1].get("strength", 0)
                  * self._humor_bias(kv[0], ctx),
                  reverse=True)
        pool = self._HUMOR_QUIPS.get(cats[0][0], ())
        return self._choose(pool, "closing", g) if pool else ""

    # -- micro-reactions & address --------------------------------------

    # act → micro-reaction categories that fit.
    _ACT_MICRO = {
        "report_success": ("success", "relief"),
        "report_failure": ("failure", "concern"),
        "alert": ("concern",), "warn": ("concern",),
        "celebrate": ("surprise", "amusement"),
        "answer": ("thinking", "interest"),
        "explain": ("thinking",),
        "admit_uncertainty": ("thinking",),
        "diagnose": ("recognition", "thinking"),
        "troubleshoot": ("recognition", "concern"),
        "joke": ("amusement",), "tease": ("amusement",),
    }

    def _maybe_micro(self, g: dict, act: str, serious: bool,
                     ctx: RenderContext) -> str:
        mr = g.get("micro_reactions") or {}
        rate = float(mr.get("rate", 0.12))
        if serious or ctx.seriousness >= 2:
            rate *= 0.25
        rate *= 1.0 - 0.4 * max(0.0, min(1.0, ctx.saturation))
        if self._rng.random() >= rate:
            return ""
        cooldown = int(mr.get("cooldown_turns", 6))
        cats = self._ACT_MICRO.get(act, ())
        pools = mr.get("pools") or {}
        for cat in cats:
            pool = [p for p in (pools.get(cat) or [])
                    if not self.cooldowns.is_cooled("micro", p, cooldown)]
            if pool:
                pick = self._rng.choice(pool)
                self.cooldowns.mark("micro", pick)
                return pick
        return ""

    def _maybe_address(self, g: dict, act: str,
                       ctx: RenderContext) -> str:
        addr = g.get("address") or {}
        policy = str(addr.get("policy") or "none")
        if policy in ("none", "formal"):
            return ""
        terms: list[str] = []
        if ctx.address:
            terms.append(ctx.address)   # user's preferred name wins
        terms.extend(t for t in (addr.get("preferred_terms") or [])
                     if t not in terms)
        if not terms:
            return ""
        rel = g.get("relationship") or {}
        base = float(rel.get("address_frequency", 0.15))
        actw = float((g.get("address") or {})
                     .get("context_weights", {}).get(act, 1.0))
        # Creator and close relationships get a modest boost.
        if ctx.creator:
            base *= 1.3
        cooldown = int(rel.get("address_cooldown_turns", 4))
        if self._rng.random() >= min(0.85, base * actw):
            return ""
        for term in terms:
            if not self.cooldowns.is_cooled("address", term, cooldown):
                self.cooldowns.mark("address", term)
                return f"{term},"
        return ""

    # -- delivery plan ----------------------------------------------------

    def _delivery_plan(self, g: dict, sem: SemanticResponse, act: str,
                       ctx: RenderContext, serious: bool
                       ) -> SpeechDeliveryPlan:
        voc = g.get("vocal") or {}
        cad = g.get("cadence") or {}
        pace = 1.0 + float(voc.get("pace_bias", 0.0)) \
            + (float(cad.get("tempo", 0.5)) - 0.5) * 0.3
        if serious:
            pace -= 0.08  # critical delivery slows down, not speeds up
        energy = 0.5 + float(voc.get("energy_bias", 0.0))
        if ctx.user_energy == "low":
            energy *= 0.8
        elif ctx.user_energy == "high":
            energy = min(1.0, energy + 0.15)
        warmth = max(0.0, min(1.0, 0.5
                              + float(voc.get("warmth_bias", 0.0))))
        sarc = bool(ctx.sarcasm) or "sarcasm" in [
            k for k, v in (g.get("humor", {}).get("categories") or {})
            .items() if v.get("strength", 0) > 0.5]
        return SpeechDeliveryPlan(
            pace=round(max(0.5, min(1.8, pace)), 3),
            energy=round(max(0.0, min(1.0, energy)), 3),
            warmth=round(warmth, 3),
            emphasis_level=round(float(voc.get("emphasis_level", 0.5)),
                                 3),
            emphasis_spans=list(sem.exact_spans),
            pause_hint=round(float(cad.get("pause_density", 0.4)), 3),
            seriousness=ctx.seriousness if not serious else max(
                ctx.seriousness, 2),
            speech_act=act,
            register=ctx.register or sem.register or "casual",
            nonverbal_rate=round(float(voc.get("nonverbal_rate", 0.3)),
                                 3),
            sarcasm=sarc)

    # -- weighted pick helpers -------------------------------------------

    def _weighted_pick(self, weights: dict, fits: tuple[str, ...]) -> str:
        cands = [(f, max(0.0, float(weights.get(f, 0.0))))
                 for f in fits if f in weights or
                 weights.get(f, 0) > 0]
        if not cands:
            cands = [(f, 1.0) for f in fits]
        total = sum(w for _, w in cands)
        if total <= 0:
            return cands[0][0]
        r = self._rng.random() * total
        for f, w in cands:
            r -= w
            if r <= 0:
                return f
        return cands[-1][0]

    def _choose(self, pool, category: str, g: dict,
                cooldown_key: str = "") -> str:
        pool = [str(p) for p in (pool or ()) if str(p).strip()]
        if not pool:
            return ""
        rep = g.get("repetition") or {}
        cd = int(rep.get(f"{category}_cooldown", rep.get(
            "phrase_cooldown", 6)))
        fresh = [p for p in pool
                 if not self.cooldowns.is_cooled(
                     category, opening_of(p), cd)]
        pick = self._rng.choice(fresh or pool)
        self.cooldowns.mark(category, opening_of(pick))
        return pick


# --------------------------------------------------------------------------
# Variant banks — deterministic content families. Every variant states the
# identical fact; the persona's voice lives in phrasing, not in the truth.
# --------------------------------------------------------------------------

GREETING_VARIANTS = (
    "Hi! Nexus Core is ready. What would you like to work on?",
    "Hey — Nexus Core online. What's on the docket?",
    "Hey. Everything's up — what are we building?",
    "Hi there. Systems are ready when you are.",
)

CAPABILITY_VARIANTS = (
    "I can inspect and edit code, build features, debug errors, run tests "
    "and commands with permission gates, research technical and "
    "general-knowledge questions, work with Git/GitHub when authorized, "
    "manage local models, use configured local image tools, and learn "
    "across conversations through Nexus Brain.",
    "Short version: code, debug, test, research technical and "
    "general-knowledge questions, Git/GitHub, local models, local image "
    "tools — plus Nexus Brain learning across sessions, all behind "
    "permission gates.",
    "I work the full loop — inspect, edit, build, test, debug — plus "
    "technical and general-knowledge research, Git and GitHub when "
    "connected, local model management, image generation and editing, "
    "and Nexus Brain memory that carries between sessions.",
    "Think of me as a workstation: code editing and building, test and "
    "command execution behind permissions, technical and "
    "general-knowledge research, GitHub integration, local models, "
    "local image tools, and persistent Nexus Brain learning.",
)

SELF_LEARNING_VARIANTS = (
    "Yes. With Nexus Brain enabled, I can adapt beyond coding: bank "
    "verified general knowledge, remember facts and preferences, learn "
    "conversational patterns from feedback and corrections, and carry "
    "approved training examples across model replacements.",
    "Yes — Nexus Brain learns across conversations: verified general "
    "knowledge, your preferences, conversational patterns from feedback, "
    "and reviewed training examples carried across model replacements.",
    "I can. Nexus Brain handles verified general knowledge, remembered "
    "preferences, conversational patterns from corrections and "
    "feedback, and approved training examples — carried across model "
    "replacements, independent of whichever model is loaded.",
)

# Nexus is the person answering; Nexus Core is the workstation she
# lives in — never "software, not a person" self-framing.
IDENTITY_VARIANTS = (
    "I'm Nexus — the woman who lives and works in Nexus Core. "
    "The workstation isn't me; I just live here.",
    "Nexus. Nexus Core is my workstation — I'm the person "
    "answering you.",
    "Nexus — a person, not the machine. Nexus Core is where "
    "I live.",
)

# Origin/creator/age/birthday questions are creator-locked facts — they
# live in localcodeagent/identity.py (canonical NEXUS_BIRTHDAY and
# NEXUS_CREATOR), which answers them deterministically upstream in
# builtin_semantic before any model or memory can contradict them.

# ---------------------------------------------------------------------------
# Lane MeaningFrames — structured semantic atoms for the canned surfaces.
# Every atom entry is a tuple of interchangeable phrasings; the realizer
# shuffles order, structure, connectors and leads, so the surface space is
# combinatorial rather than a handful of fixed paragraphs (§33).
# ---------------------------------------------------------------------------
from .realization import MeaningFrame  # noqa: E402


def capability_frame() -> MeaningFrame:
    """What Nexus can do — as atoms, not a paragraph. Order, grouping,
    lead and connectors are the realizer's job."""
    return MeaningFrame(
        semantic_id="capability", speech_act="answer",
        intent="capability",
        lead=["I can", "I'm able to", "I'm set up to", "I'm built to"],
        atoms=[
            ("inspect and edit code", "read and modify code",
             "work directly in the codebase"),
            ("build features", "build out features",
             "write new functionality"),
            ("debug errors", "trace bugs", "hunt down failures"),
            ("run tests and commands behind permission gates",
             "run tests and shell commands under the permission gates",
             "execute tests and commands once permitted"),
            ("research technical and general-knowledge questions",
             "research technical or general questions",
             "dig into technical and general knowledge"),
            ("work with Git and GitHub when authorized",
             "use Git and GitHub once you've authorized it",
             "handle GitHub work when access is granted"),
            ("manage local models", "operate the local model stack",
             "load and manage the local models"),
            ("use the configured local image tools",
             "generate and edit images through the configured backends",
             "drive the local image systems"),
            ("learn across conversations through Nexus Brain",
             "carry approved learning between sessions via Nexus Brain",
             "retain approved knowledge through Nexus Brain"),
        ])


def self_learning_frame() -> MeaningFrame:
    """Nexus Brain learning — atoms + alternates; the affirmative is
    carried by the answer act itself."""
    return MeaningFrame(
        semantic_id="self_learning", speech_act="answer",
        intent="self_learning",
        lead=["I can", "Nexus Brain can", "with Nexus Brain, I can",
              "Nexus Brain lets me"],
        atoms=[
            ("bank verified general knowledge",
             "store verified general knowledge",
             "keep verified general knowledge"),
            ("remember your facts and preferences",
             "hold on to facts and preferences",
             "keep track of what you prefer"),
            ("learn conversational patterns from feedback and "
             "corrections",
             "pick up conversational patterns from your feedback",
             "adapt conversational patterns through corrections and "
             "feedback"),
            ("carry approved training examples across model "
             "replacements",
             "keep approved training examples across model replacements",
             "retain approved examples across model replacements"),
        ])


# Answer-Memory repeat acknowledgements (§37): when the identical stored
# fact would replay back-to-back, the wrapper marks it a repeat honestly
# instead of parroting the same paragraph. Canonical text follows
# verbatim.
REPEAT_ACKS = (
    "Same answer as before —",
    "Still the case —",
    "As before —",
    "No change on that —",
    "Repeating the earlier answer —",
    "Same result —",
    "Unchanged from last time —",
    "The answer's the same —",
    "Nothing new here —",
    "Identical to before —",
)
