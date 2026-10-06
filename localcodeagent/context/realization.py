"""Full-response surface realization — same meaning, new words.

The pipeline below is the answer to wrapper-only variation: canonical
truth stays immutable, canonical *wording* does not.

    SemanticResponse / canonical text
        → MeaningFrame      (WHAT is true — atoms, spans, negations)
        → SurfacePlan       (HOW to say it — structure, order, density)
        → realize()         (Tier A combinatorial surface)
        → similarity gate   (reject lookalikes vs recent same-id renders)

Protected material (dates, numbers, identifiers, code, commands,
addresses) is masked to ``{E0}``-style placeholders before any wording
work and restored byte-for-byte afterward — variation can never touch a
fact.
"""
from __future__ import annotations

import hashlib
import random
import re
from dataclasses import dataclass, field
from typing import Any

_WORD_RE = re.compile(r"[a-z0-9']+")


def norm_words(text: str) -> list[str]:
    return _WORD_RE.findall(str(text or "").lower())


def text_fingerprint(text: str) -> str:
    return hashlib.sha1(" ".join(norm_words(text)).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# MeaningFrame — WHAT must be communicated. Never prose layout.
# ---------------------------------------------------------------------------

@dataclass
class MeaningFrame:
    """Structured semantic content for one response.

    ``facts`` are complete factual clauses; ``atoms`` are shorter
    recombinable units (capability items, greeting clause meanings) that
    the realizer may reorder/regroup. ``exact_spans`` are byte-exact
    protected material; ``negations`` are phrases whose negative polarity
    must survive; ``required_terms`` must appear somewhere in the output.
    """
    semantic_id: str = ""
    speech_act: str = ""
    intent: str = ""
    facts: list[str] = field(default_factory=list)
    atoms: list[list[str]] = field(default_factory=list)
    conclusions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    uncertainty: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    actions_completed: list[str] = field(default_factory=list)
    actions_failed: list[str] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    exact_spans: list[str] = field(default_factory=list)
    required_terms: list[str] = field(default_factory=list)
    negations: list[str] = field(default_factory=list)
    forbidden_additions: list[str] = field(default_factory=list)
    confidence: str = "verified"
    register: str = ""
    importance: float = 0.5
    # Ordered clause slots for staged surfaces (greetings, farewells):
    # each slot is a list of alternate clause phrasings; "" entries mark
    # the slot optional. The realizer picks one phrasing per slot and
    # varies how the clauses join into sentences.
    slots: list[list[str]] = field(default_factory=list)
    # Alternate phrasings for the lead clause ("I can", "Nexus Brain
    # handles") when the canonical decomposes into subject + list.
    lead: list[str] = field(default_factory=list)
    # Canonical fallback wording — kept for validation/short replies,
    # never replayed verbatim when realizable content exists.
    canonical: str = ""


def frame_from_semantic(sem: Any) -> MeaningFrame:
    """SemanticResponse → MeaningFrame. The richer fields pass through;
    callers may also attach a frame directly on ``sem.frame``."""
    existing = getattr(sem, "frame", None)
    if isinstance(existing, MeaningFrame):
        return existing
    return MeaningFrame(
        semantic_id=getattr(sem, "semantic_id", "") or "",
        speech_act=getattr(sem, "speech_act", "") or "",
        facts=list(getattr(sem, "facts", []) or []),
        conclusions=list(getattr(sem, "conclusions", []) or []),
        warnings=list(getattr(sem, "warnings", []) or []),
        uncertainty=list(getattr(sem, "uncertainty", []) or []),
        evidence=list(getattr(sem, "evidence", []) or []),
        actions_completed=list(getattr(sem, "actions_completed", []) or []),
        actions_failed=list(getattr(sem, "actions_failed", []) or []),
        next_steps=list(getattr(sem, "next_steps", []) or []),
        questions=list(getattr(sem, "questions", []) or []),
        exact_spans=list(getattr(sem, "exact_spans", []) or []),
        confidence=getattr(sem, "confidence", "verified") or "verified",
        register=getattr(sem, "register", "") or "",
        canonical=str(getattr(sem, "canonical", "") or ""))


# Subject/verb prefix that can carry a comma-list payload — "I can A, B,
# and C" decomposes to lead "I can" + atoms [A, B, C].
_LEAD_RX = re.compile(
    r"^((?:\w[\w']*\s+){0,4}?(?:can|could|am|is|are|was|were|will|would|"
    r"do|does|did|handle|handles|support|supports|offer|offers|provide|"
    r"provides|let|lets|allow|allows|work|works|know|knows|run|runs|"
    r"cover|covers|manage|manages|learn|learns|keep|keeps|remain|remains)"
    r"\b)\s+(.+)$", re.I)


def _decompose_list(sentence: str) -> tuple[list[str], str]:
    """Split "I can A, B, C, and D" → (atoms, lead). Only real lists
    (>=3 comma-separated items) decompose; "A and B" stays one clause
    so "inspect and edit code" is never shredded."""
    s = sentence.strip().rstrip(".")
    parts = [p.strip() for p in re.split(r",\s*", s) if p.strip()]
    if len(parts) < 3:
        return [], ""
    # A comma segment that opens with a binder (with/when/which/that/…)
    # is a modifier of the previous item, not a list member — fold it
    # back so "run tests, with permission gates" can't reorder into
    # "with permission gates, manage models".
    _BIND = re.compile(
        r"^(?:with|without|when|while|where|which|that|for|by|through|"
        r"via|under|behind|because|since|if|unless|once|after|before|"
        r"until|as|including|like)\b", re.I)
    merged: list[str] = []
    for p in parts:
        if merged and _BIND.match(p):
            merged[-1] = merged[-1] + ", " + p
        else:
            merged.append(p)
    parts = merged
    if len(parts) < 3:
        return [], ""
    last = re.sub(r"^(?:and|or)\s+", "", parts[-1])
    m = _LEAD_RX.match(parts[0])
    if m:
        lead, first = m.group(1), m.group(2)
    else:
        lead, first = "", parts[0]
    atoms = [first] + parts[1:-1] + [last]
    atoms = [a for a in (x.strip() for x in atoms) if a]
    if len(atoms) < 3 or not atoms[0]:
        return [], ""
    return atoms, lead


def frame_from_canonical(canonical: str, *, semantic_id: str = "",
                         speech_act: str = "answer",
                         intent: str = "") -> MeaningFrame:
    """Extract a MeaningFrame from free canonical prose. Sentences become
    facts; a comma-list sentence decomposes into recombinable atoms +
    lead so a canned enumeration can actually be rephrased — not just
    re-wrapped. Exact spans auto-mask; negation clauses recorded so
    polarity survives realization."""
    text = str(canonical or "").strip()
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text)
                 if s.strip()]
    spans = detect_exact_spans(text)
    negs = [s for s in sentences
            if re.search(r"\b(?:not|n't|never|no|without|cannot|can't)\b",
                         s, re.I)]
    frame = MeaningFrame(
        semantic_id=semantic_id, speech_act=speech_act, intent=intent,
        facts=sentences, exact_spans=spans, negations=negs,
        canonical=text)
    # A list-shaped canonical becomes atoms — the realizer can reorder,
    # regroup, and re-lead it instead of echoing it.
    if len(sentences) == 1:
        atoms, lead = _decompose_list(sentences[0])
        if atoms:
            frame.atoms = [[a] for a in atoms]
            frame.lead = [lead] if lead else []
            frame.facts = []
    elif len(sentences) > 1:
        # Multi-sentence canonical: list-shaped LAST sentences can still
        # contribute atoms after the prose lead-ins.
        atoms, lead = _decompose_list(sentences[-1])
        if atoms and lead:
            frame.atoms = [[a] for a in atoms]
            frame.lead = [lead]
            frame.facts = sentences[:-1]
    return frame


# ---------------------------------------------------------------------------
# Protected spans — mask before wording work, restore byte-exact after.
# ---------------------------------------------------------------------------

_EXACT_CANDIDATES = [
    re.compile(p) for p in (
        r"\b\d{1,2}:\d{2}\b",                                   # times
        r"\b[A-Z][a-z]+ \d{1,2}(?:st|nd|rd|th)?,\s*\d{4}\b",    # dates
        r"\b\d{4}-\d{2}-\d{2}\b",                               # ISO dates
        r"\bv?\d+\.\d+(?:\.\d+)*\b",                            # versions
        r"\b[0-9a-f]{7,40}\b(?=\s|$)",                          # SHAs-ish
        r"https?://\S+",                                        # URLs
        r"(?:[A-Za-z]:\\|/)[\w./\\-]+",                         # paths
        r"\b[\w.-]+\.[a-z]{2,5}\b(?=\s|$|[.,!?])",              # filenames
        r"`[^`]+`",                                             # code spans
        r"\b\d+(?:\.\d+)?\s*(?:mb|gb|tb|ms|s|hz|%)\b",          # measures
    )
]


def detect_exact_spans(text: str) -> list[str]:
    found: list[str] = []
    for rx in _EXACT_CANDIDATES:
        for m in rx.finditer(str(text or "")):
            span = m.group(0)
            if span not in found:
                found.append(span)
    return found


def mask_exact(text: str, spans: list[str]) -> tuple[str, dict[str, str]]:
    """Replace each protected span with a {E#} placeholder. Longest
    first so a path doesn't eat a filename inside it."""
    mapping: dict[str, str] = {}
    out = str(text or "")
    for i, span in enumerate(sorted(spans, key=len, reverse=True)):
        key = "{E%d}" % i
        if span in out:
            out = out.replace(span, key)
            mapping[key] = span
    return out, mapping


def restore_exact(text: str, mapping: dict[str, str]) -> str:
    out = str(text or "")
    for key, span in mapping.items():
        out = out.replace(key, span)
    return out


# ---------------------------------------------------------------------------
# SurfacePlan — randomized, persona-bounded discourse decision.
# ---------------------------------------------------------------------------

# Structure signatures — the response SHAPE, tracked for diversity.
STRUCTURES = (
    "single_flow",        # one flowing sentence
    "lead_list",          # framing clause + enumeration
    "answer_support",     # direct answer sentence + support sentences
    "context_answer",     # situational clause first, answer second
    "split_pair",         # two compact sentences
    "grouped",            # atoms grouped into theme sentences
    "compact_direct",     # shortest honest form
)

_CONNECTORS = {
    "oxford": lambda items: ", ".join(items[:-1]) + f", and {items[-1]}"
                if len(items) > 2 else " and ".join(items),
    "plain": lambda items: ", ".join(items[:-1]) + f" and {items[-1]}"
                if len(items) > 2 else " and ".join(items),
    "dash": lambda items: " — ".join(items),
    "plus": lambda items: ", ".join(items[:-1]) + f", plus {items[-1]}"
                if len(items) > 2 else f"{items[0]}, plus {items[1]}",
}


@dataclass
class SurfacePlan:
    structure: str = "answer_support"
    sentence_target: int = 0          # 0 = natural for content
    atom_order: list[int] = field(default_factory=list)
    connector: str = "oxford"
    lead_style: str = "none"          # none|direct|framing|summary
    contractions: bool = True
    question_tail: bool = False
    compression: float = 0.5          # 0 expansive .. 1 terse

    def signature(self) -> str:
        return f"{self.structure}:{self.connector}:{self.lead_style}"


# ---------------------------------------------------------------------------
# Tier A — structured fast realizer. No model call.
# ---------------------------------------------------------------------------

# Clause lead-ins per structure — fragments, not canned sentences.
_LEADS = {
    # Leads must agree grammatically with bare-verb atoms — "I handle
    # use Git" is the failure mode, so noun-taking leads stay out.
    "list": ("I can", "I'm able to", "I'm set up to", "I'm here to",
             "I'm built to"),
    "frame": ("Think of me as", "Picture me as", "See me as"),
    "summary": ("Short version:", "The gist:", "In one line:"),
    "state": ("Right now,", "At the moment,", "As it stands,"),
}

_CONTRACT = (
    (r"\bI am\b", "I'm"), (r"\byou are\b", "you're"),
    (r"\bit is\b", "it's"), (r"\bthat is\b", "that's"),
    (r"\bdo not\b", "don't"), (r"\bcannot\b", "can't"),
    (r"\bis not\b", "isn't"), (r"\bare not\b", "aren't"),
)
# Inverse map for non-contracting renderers — the contracted form becomes
# the (escaped) match pattern, the expanded form the replacement with \b
# stripped, since \b inside a re.sub replacement is a literal backspace.
_EXPAND = tuple((re.escape(b), a.replace(r"\b", ""))
                for a, b in _CONTRACT)


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:] if s else s


def _declause(s: str) -> str:
    """Strip a trailing period/capital-fix so a clause can embed."""
    return str(s or "").strip().rstrip(".")


class TierA:
    """Combinatorial realizer — atoms/facts × structure × connectors ×
    order. Deterministic per rng; the caller owns retry policy."""

    def __init__(self, rng: random.Random | None = None):
        self._rng = rng or random.Random()

    # -- planning ----------------------------------------------------

    def plan(self, frame: MeaningFrame, *, repeat: int = 0,
             avoid_structures: tuple[str, ...] = (),
             avoid_connectors: tuple[str, ...] = ()) -> SurfacePlan:
        """Roll a surface plan — novelty pressure rises with repeat
        count: later renders are pushed into unused structures."""
        n_atoms = len(frame.atoms)
        n_facts = len(frame.facts)
        n_slots = len(frame.slots)
        fits = [s for s in STRUCTURES
                if self._structure_fits(s, n_atoms, n_facts, n_slots)]
        # Later repeats may not reuse the structures just seen.
        fresh = [s for s in fits if s not in avoid_structures] or fits
        if repeat >= 2:
            fresh = [s for s in fresh
                     if s not in ("compact_direct",)] or fresh
        structure = self._rng.choice(fresh)
        connectors = [c for c in _CONNECTORS
                      if c not in avoid_connectors] or list(_CONNECTORS)
        order = list(range(n_atoms))
        self._rng.shuffle(order)
        return SurfacePlan(
            structure=structure,
            sentence_target=1 if structure in ("single_flow",
                                               "compact_direct") else
            (2 if structure == "split_pair" else 0),
            atom_order=order,
            connector=self._rng.choice(connectors),
            lead_style=self._rng.choice(
                ("none", "direct", "framing", "summary")),
            contractions=self._rng.random() < 0.75,
            question_tail=(self._rng.random() < 0.3
                           and frame.speech_act in ("greet", "answer")),
            compression=self._rng.random())

    @staticmethod
    def _structure_fits(s: str, n_atoms: int, n_facts: int,
                        n_slots: int = 0) -> bool:
        if n_slots:
            # Staged surfaces behave like multi-fact bodies.
            if n_slots >= 3:
                return s in ("answer_support", "context_answer",
                             "split_pair", "single_flow",
                             "compact_direct")
            return s in ("split_pair", "single_flow", "compact_direct")
        if n_atoms >= 3:
            return s in ("single_flow", "lead_list", "grouped",
                         "split_pair", "answer_support")
        if n_atoms == 2:
            return s in ("split_pair", "single_flow", "answer_support")
        if n_atoms == 1:
            return s in ("compact_direct", "answer_support",
                         "context_answer")
        # fact-sentence path
        if n_facts >= 3:
            return s in ("answer_support", "context_answer",
                         "split_pair", "grouped", "single_flow")
        if n_facts == 2:
            return s in ("split_pair", "answer_support",
                         "context_answer", "single_flow")
        if n_facts == 1:
            return s in ("compact_direct", "answer_support")
        return s == "compact_direct"

    # -- realization ---------------------------------------------------

    def realize(self, frame: MeaningFrame, plan: SurfacePlan,
                *, leads: dict[str, tuple[str, ...]] | None = None
                ) -> str:
        """Build the body from the frame + plan. Atom lanes get full
        recombination; free-fact lanes get structure-level variation."""
        leads = leads or _LEADS
        if frame.slots:
            text = self._realize_slots(frame, plan)
        elif frame.atoms:
            text = self._realize_atoms(frame, plan, leads)
        else:
            text = self._realize_facts(frame, plan)
        if not plan.contractions:
            for pat, rep in _EXPAND:
                text = re.sub(pat, rep, text)
        if frame.next_steps and plan.question_tail:
            step = _declause(frame.next_steps[0])
            text = f"{text} Want me to {step}?" if not step.lower(
            ).startswith(("want", "shall")) else f"{text} {_cap(step)}?"
        return text.strip()

    def _atom_text(self, frame: MeaningFrame, i: int) -> str:
        """One atom → one of its phrasings (banks store alternates)."""
        options = frame.atoms[i]
        if isinstance(options, str):
            return options
        return str(self._rng.choice(list(options)))

    def _realize_atoms(self, frame: MeaningFrame, plan: SurfacePlan,
                       leads: dict) -> str:
        atoms = [self._atom_text(frame, i)
                 for i in (plan.atom_order or range(len(frame.atoms)))]
        join = _CONNECTORS.get(plan.connector, _CONNECTORS["oxford"])
        lead_pool = list(frame.lead) or list(
            leads.get("list", _LEADS["list"]))
        lead = self._rng.choice(lead_pool)

        if plan.structure == "single_flow":
            return f"{_cap(lead)} {join(atoms)}."
        if plan.structure == "lead_list":
            style = self._rng.choice(
                leads.get("summary", _LEADS["summary"]))
            return f"{style} {lead} {join(atoms)}."
        if plan.structure == "grouped" and len(atoms) >= 4:
            mid = max(1, len(atoms) // 2)
            a, b = atoms[:mid], atoms[mid:]
            second_lead = self._rng.choice(
                ("I also", "I can also", "on top of that, I",
                 "and beyond that, I"))
            if second_lead.startswith("and"):
                return f"{_cap(lead)} {join(a)}, {second_lead} {join(b)}."
            return (f"{_cap(lead)} {join(a)}. "
                    f"{_cap(second_lead)} {join(b)}.")
        if plan.structure == "split_pair" and len(atoms) >= 2:
            mid = max(1, len(atoms) // 2)
            a, b = atoms[:mid], atoms[mid:]
            second = self._rng.choice(
                (f"I also {join(b)}", f"I can also {join(b)}",
                 f"and {join(b)}", f"plus {join(b)}"))
            if second.startswith(("and ", "plus ")):
                return f"{_cap(lead)} {join(a)}, {second}."
            return f"{_cap(lead)} {join(a)}. {_cap(second)}."
        # answer_support / fallback: lead + list, facts after
        body = f"{_cap(lead)} {join(atoms)}."
        extras = [str(f).strip() for f in frame.facts if str(f).strip()]
        if extras:
            body += " " + " ".join(extras)
        return body

    # -- staged slots (greetings, farewells, announcements) --------------

    def _realize_slots(self, frame: MeaningFrame, plan: SurfacePlan
                       ) -> str:
        """Pick one clause per slot, then vary how they join — merge,
        split, reorder ack/state. The slot set carries the meaning; the
        join carries the variation."""
        picked: list[str] = []
        for slot in frame.slots:
            options = [o for o in slot if str(o).strip()]
            if not options:
                continue
            # "" entries in the bank mean the slot is optional — roll
            # against inclusion rather than silently always emitting.
            if "" in slot and self._rng.random() < 0.35:
                continue
            picked.append(self._rng.choice(options))
        if not picked:
            return ""

        def _end(p: str) -> str:
            return "" if p.endswith(("?", "!")) else "."

        # Per-boundary join styles — the same clause set yields many
        # distinct surfaces depending on where the sentence breaks fall.
        # Weights favor sentence breaks; merging is the spice, not the
        # base. plan.structure biases: single_flow prefers merges,
        # split_pair prefers one clean break in the middle.
        _JOINS = (".|", ", ", " — ", "; ")
        boundaries: list[str] = []
        for i in range(len(picked) - 1):
            if plan.structure in ("single_flow", "compact_direct"):
                boundaries.append(self._rng.choice(
                    (", ", " — ", "; ", ", ", " — ")))
            elif plan.structure == "split_pair" \
                    and i == (len(picked) - 1) // 2 - 1 \
                    + (1 if len(picked) % 2 == 0 else 0):
                boundaries.append(".|")
            else:
                boundaries.append(self._rng.choice(_JOINS))
        out = _cap(_declause(picked[0])) + _end(picked[0])
        for clause, bound in zip(picked[1:], boundaries):
            if bound == ".|" or out.endswith(("?", "!")):
                # ? and ! are never followed by a merge — always break
                out += " " + _cap(_declause(clause)) + _end(clause)
            else:
                # merged — strip the previous clause's terminator
                out = out.rstrip(".") + bound + _declause(clause) \
                    + _end(clause)
        return out

    def _realize_facts(self, frame: MeaningFrame, plan: SurfacePlan
                       ) -> str:
        """Free-fact lane — vary sentence structure without touching
        clause truth: reorder independent sentences, split/join, vary
        connectors. Facts stay verbatim so no drift is possible."""
        facts = [_declause(f) for f in frame.facts if str(f).strip()]
        if not facts:
            return ""
        if len(facts) == 1 or plan.structure == "compact_direct":
            return _cap(facts[0]) + "." + (
                " " + " ".join(_cap(f) + "." for f in facts[1:]))[:0]
        order = list(range(len(facts)))
        # Reorder only when safe — a sentence that opens with a pronoun/
        # deictic depends on the one before it.
        if plan.structure in ("context_answer", "grouped"):
            head = [i for i, f in enumerate(facts) if not re.match(
                r"(?:it|that|this|they|those|he|she|then|so|but)\b",
                f, re.I)]
            if len(head) == len(facts) and len(facts) > 2:
                self._rng.shuffle(order)
        if plan.structure == "single_flow" and len(facts) <= 3:
            joiner = self._rng.choice(
                (" — ", "; ", ", and ", " — and ", ", then "))
            joined = joiner.join(facts[i] for i in order)
            return _cap(joined) + "."
        if plan.structure == "split_pair" and len(facts) >= 2:
            return f"{_cap(facts[order[0]])}. " + " ".join(
                _cap(facts[i]) + "." for i in order[1:])
        # answer_support / grouped: lead sentence, then support
        return " ".join(_cap(facts[i]) + "." for i in order)


# ---------------------------------------------------------------------------
# Similarity — the gate that makes "same meaning" ≠ "same wording".
# ---------------------------------------------------------------------------

def _ngrams(words: list[str], n: int) -> set[tuple[str, ...]]:
    return {tuple(words[i:i + n]) for i in range(len(words) - n + 1)}


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def structure_signature(text: str) -> str:
    """Response-shape fingerprint — sentence count + length class +
    opening word class. Two replies sharing it are the same shape."""
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", str(text or ""))
                 if s.strip()]
    n = len(sentences)
    lengths = []
    for s in sentences:
        w = len(norm_words(s))
        lengths.append("S" if w < 7 else "M" if w < 16 else "L")
    return f"{n}-{'_'.join(lengths) or 'empty'}"


def body_similarity(a: str, b: str) -> dict[str, float]:
    """Multi-axis surface similarity for the SAME semantic_id —
    token, bigram, trigram, opening, closing, structure."""
    wa, wb = norm_words(a), norm_words(b)
    return {
        "exact": text_fingerprint(a) == text_fingerprint(b),
        "token": _jaccard(set(wa), set(wb)),
        "bigram": _jaccard(_ngrams(wa, 2), _ngrams(wb, 2)),
        "trigram": _jaccard(_ngrams(wa, 3), _ngrams(wb, 3)),
        "opening": " ".join(wa[:6]) == " ".join(wb[:6]),
        "closing": " ".join(wa[-6:]) == " ".join(wb[-6:]),
        "structure": structure_signature(a) == structure_signature(b),
    }


def too_similar(candidate: str, recents: list[str], *,
                max_token: float = 0.70) -> bool:
    """Reject rule — exact/near duplicates, identical opening or closing
    6-gram, same structure as the immediately previous render, or high
    n-gram overlap. Scales down for short answers (bounded surface)."""
    c = str(candidate or "").strip()
    if not c:
        return False
    short = len(norm_words(c)) < 12
    for prev in recents:
        rep = body_similarity(c, prev)
        if rep["exact"]:
            return True
        if rep["opening"] or rep["closing"]:
            return True
        if not short and rep["token"] > max_token and rep["bigram"] > 0.5:
            return True
    if recents and structure_signature(c) == structure_signature(
            recents[-1]) and not short:
        return True
    return False


# ---------------------------------------------------------------------------
# Semantic validation — paraphrase must not drift.
# ---------------------------------------------------------------------------

def validate_realization(text: str, frame: MeaningFrame,
                         mapping: dict[str, str] | None = None) -> list[str]:
    """Post-realization checks. Returns a list of violations (empty =
    safe). Exact spans restored verbatim, required terms present,
    negations preserved, forbidden additions absent."""
    violations: list[str] = []
    t = str(text or "")
    low = t.lower()
    for span in frame.exact_spans:
        if span and span not in t:
            violations.append(f"exact_span_lost:{span[:40]}")
    for term in frame.required_terms:
        if term and term.lower() not in low:
            violations.append(f"required_term_missing:{term[:40]}")
    for clause in frame.negations:
        # A negated fact must keep a negative marker nearby — checking
        # the sentence containing its key noun-ish word.
        words = [w for w in norm_words(clause) if len(w) > 3]
        for sent in re.split(r"(?<=[.!?])\s+", t):
            sw = set(norm_words(sent))
            if words and sum(1 for w in words if w in sw) >= max(
                    1, len(words) // 2):
                if not re.search(r"\b(?:not|n't|never|no|without|cannot"
                                 r"|can't)\b", sent, re.I):
                    violations.append("negation_flipped")
                    break
    conf = frame.confidence
    if conf in ("uncertain", "inferred", "likely"):
        # Hedge must survive — a verified-tone rewrite is drift.
        if not re.search(r"\b(?:likely|probably|might|may|looks like|"
                         r"seems|inferred|uncertain|not sure|"
                         r"unconfirmed|guess)\b", low):
            violations.append("confidence_escalated")
    for bad in frame.forbidden_additions:
        if bad and bad.lower() in low:
            violations.append(f"forbidden_addition:{bad[:40]}")
    return violations
