from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path

from ..fsutil import atomic_write_text
from typing import Any


_OPTION_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
}

# A user reply that is nothing more than a reference to an option the
# assistant just proposed — "option 1", "the second option", "go with 3",
# "pick the first one", bare "2". Kept deliberately tight: anything longer
# or content-bearing stays a normal message.
_OPTION_SELECTION_RES = (
    re.compile(
        r"^\s*(?:go with|let'?s do|do|pick|choose|take|i'?ll take|use|run|"
        r"yes[ ,]*)?\s*(?:the\s+)?option\s*#?\s*(\d{1,2})\s*[.!?\s]*$",
        re.IGNORECASE),
    re.compile(
        r"^\s*(?:go with|let'?s do|do|pick|choose|take|i'?ll take|use|run)?"
        r"\s*(?:the\s+)?option\s+(one|two|three|four|five|six|seven|eight|"
        r"nine|ten|first|second|third|fourth|fifth|sixth|seventh|eighth|"
        r"ninth|tenth)\s*[.!?\s]*$", re.IGNORECASE),
    re.compile(
        r"^\s*(?:go with|let'?s do|do|pick|choose|take|i'?ll take|use|run)?"
        r"\s*(?:the\s+)?(first|second|third|fourth|fifth|sixth|seventh|"
        r"eighth|ninth|tenth)\s*(?:option|one)\s*[.!?\s]*$",
        re.IGNORECASE),
    re.compile(r"^\s*(\d{1,2})\s*[.!?\s]*$"),
    re.compile(
        r"^\s*(one|two|three|four|five|six|seven|eight|nine|ten)"
        r"\s*[.!?\s]*$", re.IGNORECASE),
)


RECALL_EXPRESSION_STYLES = (
    "Integrate the remembered fact naturally into the answer; avoid leading with 'you told me' unless that framing is useful.",
    "Use a concise paraphrase with a different sentence opening and sentence structure from recent replies.",
    "Frame the remembered information as a natural conversational reminder rather than reciting a stored note.",
    "Express the implication of the remembered fact in context instead of echoing its stored wording.",
    "Answer directly while changing both vocabulary and syntax from the canonical memory text.",
    "Weave the fact into the current topic and avoid phrasing used in recent assistant messages.",
    "Use a short, natural rewording; preserve factual values but not the surrounding sentence.",
    "Prefer an indirect, context-aware reference when that sounds more natural than restating the whole fact.",
)


class ConversationMemory:
    """Persistent local chat memory and conversational training notes.

    Memory changes prompts immediately. Weight training remains an explicit offline
    step so active conversations never mutate model weights unpredictably.
    """

    def __init__(
        self,
        path: Path,
        *,
        enabled: bool = True,
        history_limit: int = 200,
        rule_limit: int = 300,
        fact_limit: int = 500,
        training_limit: int = 500,
    ) -> None:
        self.path = path.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.enabled = bool(enabled)
        self.history_limit = max(20, int(history_limit))
        self.rule_limit = max(20, int(rule_limit))
        self.fact_limit = max(20, int(fact_limit))
        self.training_limit = max(20, int(training_limit))
        self._lock = threading.RLock()
        self._recall_variant_index = 0
        self._data: dict[str, Any] = {
            "version": 1,
            "messages": [],
            "facts": [],
            "behavior_rules": [],
            "training_examples": [],
            "pending_options": [],
        }
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for key in self._data:
                    value = raw.get(key)
                    if isinstance(value, list) or key == "version":
                        self._data[key] = value
                changed = False
                for key in ("facts", "behavior_rules"):
                    for row in self._data.get(key, []):
                        if isinstance(row, dict):
                            if not row.get("id"):
                                row["id"] = uuid.uuid4().hex[:12]
                                changed = True
                            if not row.get("scope"):
                                row["scope"] = "global"
                                changed = True
                if changed:
                    self._save()
        except (OSError, ValueError, TypeError):
            pass

    def _save(self) -> None:
        if not self.enabled:
            return
        atomic_write_text(self.path, json.dumps(self._data, indent=2, ensure_ascii=False))

    @staticmethod
    def _clean(text: str, limit: int = 4000) -> str:
        return " ".join(str(text or "").strip().split())[:limit]

    @staticmethod
    def _same(a: str, b: str) -> bool:
        return a.casefold().strip() == b.casefold().strip()

    _CONTENT_STOPWORDS = frozenset({
        "what", "when", "where", "which", "that", "this", "these", "those",
        "with", "from", "your", "yours", "mine", "does", "said", "tell",
        "know", "about", "would", "could", "should", "there", "their",
        "they", "them", "then", "than", "have", "has", "are", "was",
        "were", "will", "just", "like", "mean", "meant", "remember",
        "before", "earlier", "yesterday", "today", "now", "use", "uses",
        "the", "and", "for", "you", "did", "didnt", "it's", "its",
        # Generic nouns — a lone shared term like "project" must never
        # count as relevance, or every "Project X" fact rides every
        # "Project Y" question (the §4 intrusion leak the QA loop caught).
        "project", "thing", "things", "stuff", "something", "anything",
        "way", "ways", "kind", "type", "sort", "part", "parts", "case",
        "cases", "name", "names", "issue", "issues", "problem",
        "problems", "question", "questions", "answer", "answers",
    })

    @classmethod
    def _content_terms(cls, text: str) -> set[str]:
        """Content-bearing terms used for relevance-gated recall."""
        terms = {
            t for t in re.findall(r"[a-z0-9_+.#-]{3,}", str(text or "").lower())
            if t not in cls._CONTENT_STOPWORDS and not t.isdigit()
        }
        # Gentle singular fold: 'the build id ends in xq72' must answer
        # 'what did it end in' — a trailing plural/conjugated 's' adds the
        # base form alongside the original so both sides of the
        # comparison share the lemma. 'ss/us/is' tails stay ('status',
        # 'alias') so real words don't collapse.
        return terms | {
            t[:-1] for t in terms
            if len(t) >= 4 and t.endswith("s")
            and not t.endswith(("ss", "us", "is"))
        }

    _SUBJECT_ADVERBS = frozenset({
        "now", "currently", "today", "recently", "actually", "finally",
        "just", "also", "still", "really", "meanwhile", "anyway",
        "again",
    })
    # Leading position is tighter — 'still'/'already' read as genuine
    # value words in some slots ("the answer is still TBD") — keep the
    # strip set to pure temporal/discourse markers.
    _LEADING_VALUE_ADVERBS = frozenset({
        "now", "currently", "today", "actually", "just",
    })

    _CLAUSE_BOUNDARY_RE = re.compile(
        r"(?<=[.!?])\s|\s*[—;]\s*|\s+--\s+")
    _SPLICE_LEAD_RE = re.compile(
        r"^(?:(?:and|but|so|also|by\s+the\s+way|"
        r"while\s+you'?re\s+at\s+it)[,\s]+)+",
        re.IGNORECASE)
    # A head that is ONLY a scope/learning prefix ('from now on',
    # 'for this project', 'remember that') makes the comma internal —
    # 'from now on, explain errors plainly' is one learning write, not
    # a statement followed by a new clause.
    _HEAD_PREFIX_ONLY_RE = re.compile(
        r"^(?:(?:from\s+now\s+on|for\s+this\s+(?:project|conversation)|"
        r"remember(?:\s+that)?|note|fyi|btw|actually|by\s+the\s+way|"
        r"also|so|quick\s+note|for\s+the\s+record|just\s+so\s+you\s+know|"
        r"correction|i\s+meant|i\s+mean|no|i\s+want\s+you\s+to|"
        r"i'?d\s+like\s+you\s+to|you\s+should|always|never|instead|"
        r"teach(?:ing)?)[,\s]*)+$",
        re.IGNORECASE)
    # A clause tail that only manages the just-stored fact — mutation
    # verb + anaphoric object bound to it ("overwrite the old one",
    # "forget that", "use this instead") — continues the SAME teach act;
    # it is not an independent remainder the model lane must answer.
    _UPDATE_TAIL_RE = re.compile(
        r"^(?:(?:and|also|just|please|so|then)[,\s]+)*"
        r"(?:overwrite|replace|update|change|correct|fix|forget|drop|"
        r"delete|remove|discard|erase|ignore|supersede|scratch|strike|"
        r"retire|use|keep|save|store|remember|note)\s+"
        r"(?:it|that|this|them|those|"
        r"the\s+(?:old|new|previous|prior|former|earlier|last|other|"
        r"first|latest|current|same)\s+(?:one|value|version|thing|entry|"
        r"answer|setting|tag|name|port|fact)|"
        r"the\s+(?:old|new|previous|prior|former|earlier|latest|current)"
        r"\s+one|"
        r"what\s+(?:you\s+had|i\s+(?:just\s+)?said|was\s+there\s+before))"
        r"(?:[,\s]+(?:instead|now|please|thanks|thank\s+you|"
        r"going\s+forward|from\s+now\s+on))*\s*[.!?]*\s*$",
        re.IGNORECASE)

    @classmethod
    def _update_tail(cls, text: str) -> bool:
        """True when a compound turn's remainder only manages the fact
        just banked — "my tag is v10 — overwrite the old one" is one
        teach act; the supersession already satisfies the tail."""
        return bool(cls._UPDATE_TAIL_RE.match(str(text or "")))

    @classmethod
    def _first_clause(cls, text: str) -> str:
        """A learned fact is a single declarative clause. In a compound
        turn ('the port is 8080 — also, what is a mutex?') the second
        clause is a separate intent, not part of the value — banking it
        corrupts the fact and the ack swallows the question. Dots inside
        values (versions, paths) survive: the dot-boundary requires a
        following whitespace. Comma-splice counts too, but only when the
        post-comma tail opens a new interrogative/imperative clause —
        'neovim, and what's a hash map' bounds at the comma while list
        values ('hosts are a, b, c') keep theirs."""
        from ..context.semantics import _ACTION_LEAD_RE, _INTERROGATIVE_LEAD_RE
        t = cls._CLAUSE_BOUNDARY_RE.split(str(text or ""), maxsplit=1)[0]
        for m in re.finditer(r",", t):
            if cls._HEAD_PREFIX_ONLY_RE.match(t[:m.start()].strip()):
                continue
            tail = cls._SPLICE_LEAD_RE.sub("", t[m.end():].strip())
            if (_INTERROGATIVE_LEAD_RE.match(tail)
                    or _ACTION_LEAD_RE.match(tail)):
                return t[:m.start()].strip().rstrip(".!?")
        # A trailing comma/semicolon is clause-punctuation the split
        # left behind ('the standup is at 9am,') — never value content.
        return t.strip().rstrip(".!?,;:—-")

    @classmethod
    def _clean_subject(cls, raw: str) -> str:
        """Normalize a declarative-fact subject: drop trailing discourse
        adverbs ('Orion now uses X' -> 'Orion') and 'off/from X' migration
        tails so the supersession slot stays 'orion:use'."""
        subj = re.sub(r"\s+", " ", str(raw or "").strip())
        subj = re.sub(r"^(?:the|our|a|an)\s+", "", subj,
                      flags=re.IGNORECASE)
        subj = re.sub(
            r"\s+(?:off|from|away from)\s+[a-z0-9][a-z0-9 ._-]{0,38}$",
            "", subj, flags=re.IGNORECASE)
        # Strip trailing discourse adverbs only — 'Orion now' -> 'Orion'
        # while a mid-subject 'still' in a real name stays put.
        words = subj.split()
        while words and words[-1].lower() in cls._SUBJECT_ADVERBS:
            words.pop()
        return " ".join(words).strip()

    @classmethod
    def _clean_value(cls, raw: str) -> str:
        """Strip trailing punctuation and discourse adverbs from a fact
        value — 'MySQL now.' -> 'MySQL' so the stored fact stays
        canonical and dedup/supersession compare cleanly."""
        value = str(raw or "").strip().rstrip(".!?")
        words = value.split()
        while words and words[-1].lower() in cls._SUBJECT_ADVERBS:
            words.pop()
        # Leading discourse adverbs — "my editor is now vscode" banks
        # 'vscode', not 'now vscode'. Only strip when more words
        # follow — "deadline is today"/"launch is now" keep a lone
        # adverb that IS the value.
        while (len(words) > 1
               and words[0].lower() in cls._LEADING_VALUE_ADVERBS):
            words.pop(0)
        return " ".join(words).strip()

    # Preference-class facts — a restated preference ("i prefer green")
    # or a marker-only bare value ("actually no, blue") corrects a
    # preference, never a device/entity fact like "port is 8080".
    _PREFERENCE_FACT_RE = re.compile(
        r"^(?:i\s+(?:like|prefer|love)\b|my\s+favou?rite\b|"
        r"my\s+preferred\b)", re.IGNORECASE)

    # Value classes — a bare correction names its attribute's family:
    # "green" belongs with the color fact, never the food fact. New
    # classes can join here; uncovered values fall back to the
    # single-candidate rule (a bare value with several possible
    # referents is genuinely ambiguous — never guess).
    _VALUE_CLASSES: tuple[frozenset, ...] = (
        frozenset({
            "red", "orange", "yellow", "green", "blue", "purple",
            "pink", "black", "white", "gray", "grey", "brown", "teal",
            "magenta", "cyan", "violet", "maroon", "beige", "indigo",
            "turquoise", "gold", "silver",
        }),
    )

    _RETARGET_PREDICATE_RE = re.compile(
        r"^(.+?\b(?:is|are|was|were|uses?|runs on|prefers?|likes?|"
        r"loves?|should stay|should be|must be|will be|stays?|"
        r"remains?)\s+)(.+?)$", re.IGNORECASE)

    # The full declarative-predicate alternation — shared by the
    # compound `and`-split completeness check and the single-decl
    # matcher so a new predicate can't bank alone yet split-fail in a
    # compound.
    _DECL_PRED_ALT = (
        r"runs on|is built on|is written in|"
        r"depends on|ends? in|starts? with|"
        r"ends? with|(?:is|are|was|were|gets?|"
        r"is being)\s+(?:now\s+)?"
        r"(?:called|named|known as)|equals?|"
        r"lives? (?:at|in|on)|reports? to|"
        r"works? (?:on|at|for)|belongs? to|comes? from|"
        r"scheduled for|due (?:on|by|in)|"
        r"hosted (?:at|on)|"
        # Simple-verb predicates. Every one of these is also a common
        # noun, so a candidate followed by a copula is really the last
        # word of the subject ("the deploy is…", "the moves were…") —
        # the lookahead keeps the verb parse off those.
        r"(?:uses?|prefers?|equals?|expires?|ends?|starts?|runs?|"
        r"lifts?|holds?|begins?|resumes?|"
        r"pass(?:es)?|fails?|ships?|lands?|"
        r"moves?|renews?|rotates?|continues?|pauses?|"
        r"archives?|syncs?|rebuilds?|backs? up|drains?|scales?|"
        r"restarts?|resets?|clears?|purges?|cleans?|collects?|"
        r"deploys?|releases?|scans?|polls?|fires?|triggers?|"
        r"suspends?|sweeps?|prunes?|compacts?|flushes?|checks?|"
        r"wakes?|sleeps?|spins? (?:up|down)|ramps? (?:up|down)|"
        r"serves?|costs?|lasts?)"
        r"(?!\s+(?:is|are|was|were|been|being)\b)|"
        r"was|were|is|are")

    _TIME_VALUE_RE = re.compile(
        r"\b(?:today|tomorrow|tonight|tonite|noon|midnight|daily|"
        r"weekdays?|weekends?|mornings?|afternoons?|evenings?|"
        r"nights?|monday|tuesday|wednesday|thursday|friday|saturday|"
        r"sunday|january|february|march|april|may|june|july|august|"
        r"september|october|november|december|"
        r"\d{1,2}(?::\d{2})?\s*(?:am|pm))\b",
        re.IGNORECASE)

    @classmethod
    def _value_type_incompatible(cls, existing: str, new: str) -> bool:
        """Coarse value-type guard for qualified-subject retargets —
        a time-shaped value must not overwrite a non-time slot or vice
        versa. 'the deploy is at noon' is never a 'deploy region'."""
        return (bool(cls._TIME_VALUE_RE.search(str(existing or "")))
                != bool(cls._TIME_VALUE_RE.search(str(new or ""))))

    def _retarget_latest_fact(
        self, new_value: str, superseded_out: list | None = None,
        *, preference_only: bool = False,
        subject_hint: str = "",
        allow_latest_fallback: bool = True,
    ) -> str | None:
        """'make that X' / 'set it to X' — a bare re-set of the most
        recently stated value. The referent's subject+predicate is kept
        and only the value tail changes; the old row is retired
        directly so stale values never resurface (slotless facts like
        "i like tea" carry no supersession slot).

        ``preference_only`` restricts referents to preference-class
        facts ("i like X", "my favorite X is Y") — a restated
        preference must never rewrite an entity fact. Within those, a
        value in a known class (colors) picks the fact whose value is
        in the same class; otherwise exactly one preference fact must
        exist. Returns None when the referent is genuinely ambiguous —
        a bare correction must never guess."""
        self._last_retarget_ambiguous = False
        self._last_retarget_already = ""
        new = self._clean_value(new_value)
        if (not new or len(new.split()) > 4
                or new.lower() in self._RESET_MODIFIERS
                or new.lower() in self._BARE_CORRECTION_STOPWORDS
                or self._REFERENT_VALUE_RE.match(new)
                or self._IMPERATIVE_VALUE_RE.match(new)):
            return None
        pred = self._RETARGET_PREDICATE_RE
        with self._lock:
            rows = [
                row for row in self._data.get("facts", [])
                if isinstance(row, dict) and row.get("active", True)
            ]
            target = None
            m = None
            if preference_only:
                # Value-shape compatibility applies to preference
                # retargets too — "no it's 8443" can't land on 'my
                # favorite color is teal'.
                cands = [
                    row for row in reversed(rows)
                    if self._PREFERENCE_FACT_RE.search(
                        str(row.get("text") or ""))
                    and (m3 := pred.match(str(row.get("text") or "")))
                    and (bool(re.search(r"\d", m3.group(2)))
                         == bool(re.search(r"\d", new)))
                ]
                cls = next((c for c in self._VALUE_CLASSES
                            if new.lower() in c), None)
                if cls is not None:
                    cands = [
                        row for row in cands
                        if (m2 := pred.match(str(row.get("text") or "")))
                        and m2.group(2).strip().lower() in cls
                        and m2.group(2).strip().lower() != new.lower()
                    ]
                else:
                    cands = [
                        row for row in cands
                        if not str(row.get("text") or "")
                        .strip().lower().endswith(new.lower())
                    ]
                if len(cands) != 1:
                    return None
                target = cands[0]
                m = pred.match(str(target.get("text") or ""))
            else:
                # A subject hint ("the render box is now named titan")
                # binds the update to the same entity's slot — 'render
                # box' retargets 'render box name is cobalt' rather than
                # forking a parallel 'render box' fact. Word-boundary
                # containment counts. A hint with no match fails the
                # retarget — an explicit subject isn't referential, and
                # rewriting the unrelated latest fact would be a guess.
                hint = re.sub(r"^(?:my|the|our)\s+", "",
                              subject_hint.strip().lower())
                candidates = rows
                if hint:
                    hits = []
                    for row in reversed(rows):
                        m2 = pred.match(str(row.get("text") or ""))
                        if not m2:
                            continue
                        fsubj = re.sub(r"^(?:my|the|our)\s+", "",
                                       m2.group(1).strip().lower())
                        # group(1) carries the predicate too ("oncall
                        # is") — an exact-subject hint must compare
                        # against the bare subject or every retarget
                        # reads as a qualified-slot mismatch.
                        fsubj = re.sub(
                            r"\b(?:is|are|was|were|uses?|runs on|"
                            r"prefers?|likes?|loves?|should stay|"
                            r"should be|must be|will be|stays?|"
                            r"remains?)\s*$", "", fsubj).strip()
                        if not re.search(
                                r"\b" + re.escape(hint) + r"\b", fsubj):
                            continue
                        # A hint that is only a prefix of the subject
                        # ('deploy' vs 'deploy region') names a broader
                        # entity — the qualified sub-attribute slot may
                        # only be rewritten by a compatible value: a
                        # schedule can't land on a region slot.
                        if (fsubj != hint
                                and self._value_type_incompatible(
                                    m2.group(2), new)):
                            continue
                        hits.append(row)
                    if not hits:
                        return None
                    candidates = hits[:1]
                # Collect every value-shape-compatible candidate, then
                # resolve the referent by conversational focus — the
                # fact just *stated* in the previous exchange is what
                # "no it's X" contradicts, not whatever banked most
                # recently. Explicit referent commands ('call it X')
                # keep latest-wins; bare corrections refuse ambiguity
                # rather than rewriting an uninvolved fact.
                compat = []
                already_rows = []
                for row in reversed(candidates):
                    m2 = pred.match(str(row.get("text") or ""))
                    if not m2:
                        continue
                    if (str(row.get("text") or "").strip().lower()
                            .endswith(new.lower())):
                        already_rows.append((row, m2))
                        continue
                    if (bool(re.search(r"\d", m2.group(2)))
                            != bool(re.search(r"\d", new))):
                        continue
                    compat.append((row, m2))
                if not hint and (len(compat) > 1 or already_rows):
                    prev_q, prev_a = self._previous_exchange()
                    focus_terms = self._content_terms(
                        f"{prev_q} {prev_a}")
                    # A focused already-stated row answers the turn —
                    # "no it's 8443" about the staging api that already
                    # reads 8443 is a restate, not a correction target.
                    # A bare pronoun refers to the subject under
                    # discussion, so it beats even a focused live
                    # candidate.
                    already_focused = [
                        c for c in already_rows
                        if (st := self._content_terms(c[1].group(1)))
                        and st <= focus_terms]
                    scored = [
                        (len(self._content_terms(c[1].group(1))
                             & focus_terms), c)
                        for c in compat]
                    scored = [s for s in scored if s[0]]
                    top = max((s[0] for s in scored), default=0)
                    focused = [c for s, c in scored if s == top]
                    if len(already_focused) == 1:
                        compat = []
                        self._last_retarget_already = str(
                            already_focused[0][0].get("text") or "")
                    elif len(focused) == 1:
                        compat = focused
                    elif not allow_latest_fallback and not compat:
                        self._last_retarget_already = str(
                            already_rows[0][0].get("text") or "")
                    elif not allow_latest_fallback:
                        compat = []
                        self._last_retarget_ambiguous = True
                if (not compat and already_rows
                        and not self._last_retarget_already
                        and not self._last_retarget_ambiguous):
                    # Nothing to correct but the value is already
                    # recorded — restate the fact that holds it.
                    self._last_retarget_already = str(
                        already_rows[0][0].get("text") or "")
                if compat:
                    target, m = compat[0]
            if target is None or m is None:
                return None
            now = time.time()
            old_text = str(target.get("text") or "")
            target["active"] = False
            target["superseded"] = True
            target["superseded_at"] = now
            target["updated_at"] = now
            if superseded_out is not None:
                superseded_out.append(old_text)
        # Preserve a connective preposition on the replaced value —
        # "no it's 8443" retargeting 'prod api is on 443' yields 'on
        # 8443', not 'is 8443'. Only for concrete replacements
        # (digits/caps/versions) — a word value keeps the plain copula
        # ("is sam", never "is on sam").
        old_v = m.group(2).strip()
        pm = re.match(
            r"(on|at|in|to|for|from|by|via|per|under|over|after|"
            r"before|until|through|around|about|like|as|near|off|"
            r"into|onto|upon|within|without|against|between|among|"
            r"during|since|with)\s+\S+$", old_v, re.IGNORECASE)
        if (pm and re.search(r"[\dA-Z]|^v\d", new)
                and not re.match(
                    r"(?:on|at|in|to|for|from|by|via|per|under|over|"
                    r"after|before|until|through|around|about|like|as|"
                    r"near|off|into|onto|upon|within|without|against|"
                    r"between|among|during|since|with)\b",
                    new, re.IGNORECASE)):
            new = pm.group(1) + " " + new
        # A preposition-led replacement ('on 9100') can't glue onto a
        # non-copula predicate — 'metrics api uses on 9100' is
        # malformed. Rebuild with the copula ('metrics api is on
        # 9100'); 'runs on' keeps its verb minus the doubled 'on'.
        head = m.group(1)
        if re.match(
                r"(?:on|at|in|to|for|from|by|via|per|under|over|"
                r"after|before|until|through|around|about|like|as|"
                r"near|off|into|onto|upon|within|without|against|"
                r"between|among|during|since|with)\b",
                new, re.IGNORECASE):
            head = re.sub(
                r"\b(?:uses?|prefers?|likes?|loves?|stays?|remains?|"
                r"should stay|should be|must be|will be)\s*$",
                "is ", head, flags=re.IGNORECASE)
            head = re.sub(r"\bruns on\s*$", "runs ", head,
                          flags=re.IGNORECASE)
        return f"{head}{new}"

    def _correct_fact_value(self, old_value: str, new_value: str) -> str | None:
        """'X not Y' correction — rewrite the fact that ends in Y.

        Returns the corrected fact text when exactly one active fact's
        value tail matches `old_value` (ambiguous matches and no-matches
        stay untouched — a correction must never guess). The caller
        stores it via the normal append path so slot supersession
        retires the stale row rather than editing history in place.
        """
        old = str(old_value or "").strip().lower()
        new = str(new_value or "").strip()
        if not old or not new or old == new.lower():
            return None
        with self._lock:
            hits = [
                row for row in self._data.get("facts", [])
                if isinstance(row, dict) and row.get("active", True)
                and str(row.get("text", "")).strip().lower().endswith(old)
            ]
        if len(hits) != 1:
            return None
        text = str(hits[0].get("text", "")).strip()
        return text[: len(text) - len(old)].rstrip() + " " + new

    def _revert_candidate(self, referent: str) -> dict | None:
        """Read-only: the most recently superseded fact row whose
        subject matches the referent, or None. Shared by the dry-run
        peek and the mutating revert so both resolve identically."""
        ref = str(referent or "").strip().lower()
        ref = re.sub(r"^(?:my|the|our)\s+", "", ref)
        if not ref:
            return None
        with self._lock:
            rows = [r for r in self._data.get("facts", [])
                    if isinstance(r, dict)]

            def _subj(row: dict) -> str:
                m = self._RETARGET_PREDICATE_RE.match(
                    str(row.get("text") or ""))
                if not m:
                    return ""
                return re.sub(r"^(?:my|the|our)\s+", "",
                              m.group(1).strip().lower())

            def _hit(row: dict) -> bool:
                s = _subj(row)
                return bool(s) and (ref in s or s in ref)

            superseded = sorted(
                (r for r in rows
                 if not r.get("active", True) and r.get("superseded")
                 and _hit(r)),
                key=lambda r: float(r.get("superseded_at") or 0),
                reverse=True)
            return superseded[0] if superseded else None

    def peek_revert_fact(self, referent: str) -> str | None:
        """Dry-run counterpart of revert_fact — reports the fact text a
        revert would restore without touching any state."""
        row = self._revert_candidate(referent)
        return str(row.get("text") or "") or None if row else None

    def revert_fact(self, referent: str) -> str | None:
        """'undo that <referent> change' — restore the most recently
        superseded fact whose subject matches the referent. The current
        active row in that slot is retired; the prior value goes live
        again. Returns the restored fact text, or None when nothing in
        the slot's history matches (a named referent never guesses)."""
        with self._lock:
            old = self._revert_candidate(referent)
            if old is None:
                return None
            rows = self._data.get("facts", [])
            old_slot = self._slot_canonical(str(old.get("slot") or ""))
            now = time.time()
            # Retire whatever currently holds the slot (if anything).
            for r in rows:
                if (r.get("active", True)
                        and self._slot_canonical(str(r.get("slot") or ""))
                        == old_slot and old_slot):
                    r["active"] = False
                    r["superseded"] = True
                    r["superseded_at"] = now
                    r["superseded_by"] = old.get("id")
                    r["updated_at"] = now
            old["active"] = True
            old["superseded"] = False
            old.pop("superseded_by", None)
            old.pop("superseded_at", None)
            old["updated_at"] = now
            self._save()
            return str(old.get("text") or "") or None

    def _retract_fact_value(self, old_value: str) -> list[dict[str, Any]]:
        """'X isn't Y (anymore)' with no replacement — retire the unique
        active fact whose value tail is Y. Same precision rule as
        corrections: exactly one match, or guess nothing."""
        old = str(old_value or "").strip().lower()
        if not old:
            return []
        with self._lock:
            hits = [
                row for row in self._data.get("facts", [])
                if isinstance(row, dict) and row.get("active", True)
                and str(row.get("text", "")).strip().lower().endswith(old)
            ]
            if len(hits) != 1:
                return []
            hits[0]["active"] = False
            hits[0]["updated_at"] = time.time()
            self._save()
            return [dict(hits[0])]

    def _retract_fact_contains(self, needle: str) -> list[dict[str, Any]]:
        """Retract the unique active fact that MENTIONS `needle` as a
        whole word — 'i use slack for comms' doesn't end in 'slack' but
        still names it. Same precision rule: exactly one hit or none."""
        n = str(needle or "").strip().lower()
        if not n:
            return []
        word = re.compile(
            r"(?<![a-z0-9])" + re.escape(n) + r"(?![a-z0-9])")
        with self._lock:
            hits = [
                row for row in self._data.get("facts", [])
                if isinstance(row, dict) and row.get("active", True)
                and word.search(str(row.get("text", "")).lower())
            ]
            if len(hits) != 1:
                return []
            hits[0]["active"] = False
            hits[0]["updated_at"] = time.time()
            self._save()
            return [dict(hits[0])]

    @staticmethod
    def _fact_subject_ok(subj: str) -> bool:
        """Question/command-shaped openings are not fact subjects."""
        if not subj:
            return False
        # A bare determiner is never a subject — 'the deploy ships
        # friday' must not parse subj='the', pred='deploy' and bank the
        # whole sentence verbatim.
        if subj.strip().casefold() in {
                "the", "a", "an", "my", "our", "your", "its", "their"}:
            return False
        if subj.split()[0].casefold() in {
                "i", "we", "you", "they", "he", "she", "it",
                "what", "which", "who", "whom", "whose", "why", "how",
                "when", "where", "y", "does", "do", "did", "can", "could",
                "should", "would", "shall", "may", "might",
                "is", "are", "was", "were", "will", "this", "that",
                "tell", "show", "if", "let",
                # Never-noun imperative leads — "go back to X", "get
                # me Y", "remind me to Z" are requests wearing a
                # declarative parse (a verb-only first token with no
                # article is an imperative, not a subject).
                "go", "get", "put", "take", "look", "give", "send",
                "bring", "ask", "remind", "ping", "grab", "fetch",
                "forward", "say", "thank", "please", "come", "wait",
                "help", "listen", "watch", "sit", "stand", "hold",
                "talk", "speak", "gimme",
                "nm", "isnt", "arent", "doesnt", "dont", "don't"}:
            return False
        # Embedded interrogative/relative clauses are commands in
        # disguise — "go back to what we were talking about" parses a
        # 'were' declarative with 'what we' inside the subject.
        if re.search(
                r"\b(?:what|which|who|whom|whose|why|how|when|where)\s+"
                r"(?:i|we|you|they|he|she|it)\b"
                r"|\bthat\s+(?:i|we|you|they|he|she|it)\b",
                subj, flags=re.IGNORECASE):
            return False
        # A wh-word as the subject's LAST token is the same inversion
        # one token earlier — "hmm what was my editor" parses subj
        # 'hmm what' + 'was' and would bank the question verbatim.
        # Mid-subject names ('the who concert') stay legal.
        toks = subj.strip().split()
        wh_words = {"what", "which", "who", "whom", "whose", "why",
                    "when", "where", "how"}
        if toks[-1].casefold() in wh_words:
            return False
        # An interrogative determiner as the FIRST token is question word
        # order — "and what port was prod on" (connector-stripped) parses
        # subj 'what port' + 'was'. 'who'/'whom' stay legal: names like
        # 'the who concert' arrive article-stripped, and a bare 'who'
        # subject is still caught by the last-token guard above.
        if toks[0].casefold() in {"what", "which", "whose", "why",
                                  "when", "where", "how"}:
            return False
        return True

    @staticmethod
    def _fact_slot(text: str) -> str:
        """Supersession slot for entity-attribute facts.

        Facts naming a concrete subject+relation ("orion uses postgresql",
        "my editor is vim") occupy a slot; a newer fact in the same slot
        retires the older one. Vague/open facts ("i prefer tea") keep no
        slot so unrelated preferences are never clobbered.
        """
        t = str(text or "").strip().lower()
        m = re.match(r"^my\s+([a-z0-9][a-z0-9 ._-]{0,38}?)\s+(?:is|are|was|were)\b", t)
        if m:
            return "my:" + re.sub(r"\s+", " ", m.group(1)).strip()
        m = re.match(
            r"^(?:project\s+)?([a-z0-9][a-z0-9 ._-]{0,38}?)\s+"
            r"(uses?|runs on|is built on|is written in|depends on|prefers?|"
            r"should stay|should be|must be|will be|shall be|stays?|remains?|"
            r"listens? on|is|are)\b",
            t,
        )
        if m:
            subj = re.sub(r"\s+", " ", m.group(1)).strip()
            subj = re.sub(r"^(?:the|our|a|an)\s+", "", subj)
            pred = re.sub(r"\s+", " ", m.group(2)).strip()
            tail = t[m.end():].strip()
            # 'prod api is on 443', 'prod api uses port 443', and 'the
            # api listens on 9090' fill the same semantic slot — unify
            # so a newer port fact retires the older phrasing instead
            # of coexisting as a contradiction.
            if ((pred in {"is", "are"}
                    and re.match(r"on\s+\d+\b", tail))
                    or (pred in {"use", "uses"}
                        and re.match(r"port\b", tail))
                    or pred.startswith("listens on")
                    or pred.startswith("listen on")):
                pred = "port"
            if subj and subj not in {"i", "we", "you", "they", "he", "she", "it"}:
                return f"{subj}:{pred}"
        # Cadence facts — 'api keys rotate monthly', 'db backups run
        # nightly' — name one attribute (how often), so a restated
        # cadence supersedes rather than coexisting contradictory
        # frequencies on the same subject.
        m = re.match(
            r"^(?:the\s+|a\s+|an\s+|our\s+)?([a-z0-9][a-z0-9 ._-]{0,38}?)"
            r"\s+[a-z]+\s+"
            r"(?:every\s+(?:day|night|hour|week|month|year|"
            r"monday|tuesday|wednesday|thursday|friday|saturday|"
            r"sunday|morning|evening|weekend|weekday)|"
            r"(?:daily|nightly|weekly|hourly|monthly|annually|"
            r"weekdays|weekends|mondays|tuesdays|wednesdays|"
            r"thursdays|fridays|saturdays|sundays|overnight|"
            r"each\s+(?:day|night|hour|week|month)))\b",
            t)
        if m:
            subj = re.sub(r"\s+", " ", m.group(1)).strip()
            if subj and subj not in {"i", "we", "you", "they", "he",
                                     "she", "it"}:
                return f"{subj}:cadence"
        return ""

    @staticmethod
    def _slot_canonical(slot: str) -> str:
        """Supersession key for a stored slot — 'my deploy tag is v10'
        and 'the deploy tag is v9' describe the same attribute (the
        user's deploy tag IS the deploy tag), so 'my:X' and 'X:is/are/
        was/were' collapse onto the same canonical slot. Without this a
        value restated with or without the possessive splits into two
        live facts (observed live: 'deploy tag is v9' +
        'my deploy tag is v10' both active)."""
        s = str(slot or "")
        if s.startswith("my:"):
            return s[3:] + ":is"
        subj, sep, pred = s.rpartition(":")
        if sep and pred in {"is", "are", "was", "were"}:
            return f"{subj}:is"
        return s

    _RULE_DURATION_RE = re.compile(
        r"\bfor\s+(?:the\s+next\s+)?"
        r"(?:(a|an|one|two|three|four|five|"
        r"six|seven|eight|nine|ten|\d+)\s+)?"
        r"(minute|hour|day|week)s?\b",
        re.IGNORECASE)
    _RULE_DURATION_WORDS = {
        "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4,
        "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
        "ten": 10}
    _RULE_DURATION_UNITS = {
        "minute": 60.0, "hour": 3600.0, "day": 86400.0,
        "week": 604800.0}

    @classmethod
    def _rule_expiry(cls, text: str) -> float | None:
        """Bounded-duration qualifier on a rule — 'don't summarize
        anything for the next hour' — yields the epoch at which the
        rule self-retires, so a one-hour prohibition doesn't silently
        become permanent. Unbounded rules return None."""
        m = cls._RULE_DURATION_RE.search(str(text or ""))
        if not m:
            return None
        raw_count = (m.group(1) or "one").lower()
        count = cls._RULE_DURATION_WORDS.get(raw_count) or int(raw_count)
        seconds = count * cls._RULE_DURATION_UNITS[m.group(2).lower()]
        return time.time() + seconds

    @staticmethod
    def _row_expired(row: dict[str, Any], now: float | None = None) -> bool:
        exp = row.get("expires_at")
        if exp is None:
            return False
        return (time.time() if now is None else now) >= float(exp)

    def _append_unique(
        self,
        key: str,
        text: str,
        limit: int,
        *,
        scope: str = "global",
        scope_id: str = "",
        slot: str = "",
        superseded_out: list | None = None,
        expires_at: float | None = None,
    ) -> bool:
        clean = self._clean(text)
        if not clean:
            return False
        rows = self._data.setdefault(key, [])
        # Only an ACTIVE identical row dedupes. A superseded/forgotten
        # twin must not block re-teaching the same fact later — the new
        # row lands fresh and supersedes nothing.
        if any(
            isinstance(row, dict)
            and row.get("active", True)
            and self._same(str(row.get("text", "")), clean)
            for row in rows
        ):
            return False
        now = time.time()
        row_id = uuid.uuid4().hex[:12]
        if slot:
            for row in rows:
                if (
                    isinstance(row, dict)
                    and row.get("active", True)
                    and self._slot_canonical(str(row.get("slot") or ""))
                    == self._slot_canonical(slot)
                    and str(row.get("scope") or "global") == scope
                    and str(row.get("scope_id") or "") == scope_id
                ):
                    row["active"] = False
                    row["superseded"] = True
                    row["superseded_at"] = now
                    row["superseded_by"] = row_id
                    row["updated_at"] = now
                    if superseded_out is not None:
                        superseded_out.append(str(row.get("text") or ""))
        row: dict[str, Any] = {
            "id": row_id,
            "text": clean,
            "created_at": now,
            "active": True,
            "scope": scope,
            "scope_id": scope_id,
            "slot": slot,
        }
        if expires_at is not None:
            row["expires_at"] = float(expires_at)
        rows.append(row)
        self._data[key] = rows[-limit:]
        return True

    def _fact_is_active(self, text: str) -> bool:
        clean = self._clean(text)
        rows = self._data.get("facts", [])
        return any(
            isinstance(row, dict)
            and row.get("active", True)
            and self._same(str(row.get("text", "")), clean)
            for row in rows
        )

    def history(self, limit: int = 24) -> list[dict[str, str]]:
        if not self.enabled:
            return []
        with self._lock:
            rows = list(self._data.get("messages", []))[-max(1, int(limit)):]
        return [
            {"role": str(row.get("role", "")), "content": str(row.get("content", ""))}
            for row in rows
            if isinstance(row, dict)
            and row.get("role") in {"user", "assistant"}
            and str(row.get("content", "")).strip()
        ]

    def record_exchange(self, user_text: str, assistant_text: str,
                        conversation_id: str | None = None) -> None:
        if not self.enabled:
            return
        user = str(user_text or "").strip()[:12000]
        assistant = str(assistant_text or "").strip()[:12000]
        if not user or not assistant:
            return
        with self._lock:
            messages = self._data.setdefault("messages", [])
            if len(messages) >= 2:
                prev_user = messages[-2]
                prev_assistant = messages[-1]
                if (
                    isinstance(prev_user, dict)
                    and isinstance(prev_assistant, dict)
                    and prev_user.get("role") == "user"
                    and prev_assistant.get("role") == "assistant"
                    and str(prev_user.get("content", "")) == user
                    and str(prev_assistant.get("content", "")) == assistant
                ):
                    return
            now = time.time()
            messages.extend([
                {"role": "user", "content": user, "timestamp": now},
                {"role": "assistant", "content": assistant, "timestamp": now},
            ])
            self._data["messages"] = messages[-self.history_limit:]
            # Track option lists from the latest assistant reply so a short
            # follow-up like "option 1" can be resolved instead of falling
            # through to the model as an ambiguous fragment.
            self._data["pending_options"] = self.extract_options(assistant)
            # Options bind to the conversation that produced them — a
            # bare "option 2" in a different conversation must not
            # resolve against another conversation's proposal.
            self._data["pending_options_conversation"] = (
                str(conversation_id) if conversation_id else None)
            self._save()

    @staticmethod
    def extract_options(assistant_text: str) -> list[str]:
        """Numbered options from an assistant proposal — "Option 1: …",
        "**Option 2** — …", or bare "1. …" lists. Only counts as an option
        list when numbering starts at 1 and runs consecutively; lines
        inside code fences are ignored."""
        options: list[tuple[int, str]] = []
        in_fence = False
        for line in str(assistant_text or "").splitlines():
            stripped = line.strip()
            if stripped.startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence or len(stripped) > 400:
                continue
            m = re.match(
                r"^(?:[-*•]\s*)?(?:\*\*)?(?:option\s*)?(\d{1,2})\s*"
                r"[\.\)\:]?\s*(?:\*\*)?\s*[:\-–—]?\s*(?:\*\*)?\s*(.+?)\s*$",
                stripped, flags=re.IGNORECASE)
            if not m:
                continue
            text = re.sub(r"\*\*([^*]+)\*\*", r"\1", m.group(2)).strip()
            text = text.strip("*_ ").strip()
            if text:
                options.append((int(m.group(1)), text[:300]))
        # Only a genuine numbered list: starts at 1, consecutive, ≥2 items.
        if len(options) < 2:
            return []
        if [n for n, _ in options] != list(range(1, len(options) + 1)):
            return []
        return [t for _, t in options]

    def resolve_option_selection(self, user_text: str,
                                 conversation_id: str | None = None) -> str | None:
        """Expand a bare selection ("option 1", "the second option",
        "go with 2") into the option text from the most recent assistant
        proposal, so downstream routing sees the real intent."""
        options = self._data.get("pending_options") or []
        if len(options) < 2:
            return None
        tagged = self._data.get("pending_options_conversation")
        if tagged is not None and tagged != conversation_id:
            return None
        text = self._clean(user_text, 200)
        if not text:
            return None
        number = None
        for pattern in _OPTION_SELECTION_RES:
            match = pattern.match(text)
            if match:
                token = match.group(1).lower()
                number = (int(token) if token.isdigit()
                          else _OPTION_NUMBER_WORDS.get(token))
                break
        if number is None or not 1 <= number <= len(options):
            return None
        return f"Option {number} — {options[number - 1]}"

    # Acknowledgements must never be learned as fact values —
    # "actually, sure" is agreement, not a correction to the last fact.
    _BARE_CORRECTION_STOPWORDS = frozenset({
        "yes", "yeah", "yep", "sure", "ok", "okay", "thanks",
        "thank you", "great", "good", "fine", "nice", "cool", "true",
        "right", "exactly", "agreed", "maybe", "correct", "perfect",
        "sounds good", "that works", "nothing", "never mind",
        # 'no <noun>' politeness/dismissal idioms are acknowledgements,
        # never corrections — "no problem" must not retarget a fact to
        # 'X is problem'.
        "no", "no problem", "no problems", "no worries", "no worry",
        "no thanks", "no thank you", "no rush", "no hurry", "no need",
        "no sweat", "no biggie", "no bother", "no trouble",
        "no issue", "no issues", "no idea", "no clue", "no way",
        "no doubt", "not at all", "no one", "none",
    })

    # Referent-relative modifiers — "make that bigger" adjusts a
    # TARGET, it doesn't state a value. Letting a comparative land in a
    # fact's value slot produces nonsense ("my favorite color is
    # bigger") and swallows the user's actual command.
    _RESET_MODIFIERS = frozenset({
        "bigger", "smaller", "larger", "darker", "lighter", "longer",
        "shorter", "faster", "slower", "higher", "lower", "wider",
        "narrower", "louder", "quieter", "warmer", "cooler", "hotter",
        "colder", "brighter", "dimmer", "stronger", "weaker", "more",
        "less", "up", "down", "on", "off", "back", "again", "thicker",
        "thinner", "deeper", "shallower", "taller", "heavier", "easier",
        "harder", "softer", "firmer", "looser", "tighter", "cheaper",
        "closer", "further", "farther", "better", "worse", "bolder",
        "smoother", "rougher", "simpler", "fuller", "emptier",
    })

    # Referent phrases, not literal values — "change it to the dark
    # one"/"the first one" name a prior entity, so landing them in a
    # fact's value slot produces "my favorite color is the dark one".
    _REFERENT_VALUE_RE = re.compile(
        r"^the\s+\w+\s+one$|^the\s+(?:other|same|first|second|third|"
        r"last|previous|next)\b", re.IGNORECASE)

    # Imperative/negation openings — "no, don't do that" rejects an
    # action; its tail is an instruction, never a stated value.
    _IMPERATIVE_VALUE_RE = re.compile(
        r"^(?:don'?t|do\s+not|dont|stop|quit|never|please|let'?s|let|"
        r"keep|avoid|forget|cancel|skip|leave|hold|wait|try|go|move|"
        r"delete|remove|undo|revert)\b", re.IGNORECASE)

    # Single-word values that ARE facts in 'X is Y' statements —
    # settings/mode/units vocabulary ("the theme is dark", "units are
    # metric") that the adjective guard would otherwise reject. Pure
    # evaluations ("the movie is great") still don't learn.
    _SETTINGS_VALUES = frozenset({
        "light", "dark", "auto", "automatic", "manual", "default",
        "system", "metric", "imperial", "celsius", "fahrenheit",
        "enabled", "disabled", "portrait", "landscape", "compact",
        "silent", "normal", "verbose", "debug", "release", "beta",
        "stable", "nightly", "production", "staging", "development",
        "high", "medium", "low", "on", "off",
        # Cadence words — "the review cadence is weekly" states a fact,
        # it doesn't schedule work.
        "hourly", "daily", "weekly", "monthly", "quarterly",
        "yearly", "annually", "fortnightly", "weekdays", "weekends",
    })

    @staticmethod
    def _correction_clauses(text: str) -> list[str]:
        """Split a compound self-correction into ordered clauses.

        "I like red. Actually no, blue. Never mind, make it green."
        carries three intent statements where the LAST one is live —
        the earlier clauses exist only to be superseded. Feeding each
        clause through the pipeline in order composes the supersession
        instead of storing the whole chain verbatim (which would bank
        every obsolete value into one retrievable fact).

        Returns [] for ordinary statements — the existing pipeline
        handles them unchanged.
        """
        # 'and <marker>' is also a boundary — "the deadline is friday,
        # and actually make the standup 10:30" carries a statement then
        # a correction; banking the chain verbatim loses both.
        parts = [p.strip() for p in
                 re.split(
                     r"(?<=[.!?])\s+|\s*;\s*|,?\s+and\s+"
                     r"(?=actually|no\b|wait\b|instead|sorry|"
                     r"never\s*mind|nevermind|nm\b|scratch\s+that|"
                     r"on\s+second\s+thought|i\s+mean|i\s+meant|"
                     r"correction)", text) if p.strip()]
        if len(parts) < 2:
            return []
        marker = re.compile(
            r"^(?:actually|no|wait|instead|sorry|never\s*mind|nevermind|"
            r"nm|scratch\s+that|on\s+second\s+thought|i\s+mean|i\s+meant|"
            r"correction)\b", re.IGNORECASE)
        if not any(marker.search(p) for p in parts[1:]):
            return []
        return parts

    def _previous_exchange(self) -> tuple[str, str]:
        messages = self._data.get("messages", [])
        previous_user = ""
        previous_assistant = ""
        for row in reversed(messages):
            if not isinstance(row, dict):
                continue
            role = str(row.get("role", ""))
            content = str(row.get("content", ""))
            if role == "assistant" and not previous_assistant:
                previous_assistant = content
                continue
            if role == "user" and previous_assistant:
                previous_user = content
                break
        return previous_user, previous_assistant

    def learn_from_user(
        self,
        user_text: str,
        *,
        project_id: str = "",
        conversation_id: str = "",
    ) -> dict[str, list[Any]]:
        """Capture explicit facts, operating rules, and corrections from chat."""
        result: dict[str, list[Any]] = {
            "facts": [],
            "behavior_rules": [],
            "training_examples": [],
            "forgotten": [],
            "superseded": [],
            "restated": [],
        }
        if not self.enabled:
            return result

        raw = self._clean(user_text, 12000)
        if not raw:
            return result

        scope = "global"
        scope_id = ""
        project_match = re.match(r"^for\s+this\s+project[,:]?\s*(.+)$", raw, flags=re.IGNORECASE)
        conversation_match = re.match(r"^for\s+this\s+conversation[,:]?\s*(.+)$", raw, flags=re.IGNORECASE)
        if project_match:
            scope = "project"
            scope_id = project_id
            raw = project_match.group(1).strip()
        elif conversation_match:
            scope = "conversation"
            scope_id = conversation_id
            raw = conversation_match.group(1).strip()

        # "my notes say 'never use port 4000' — remember that" — the
        # quoted span is the referent of the learning request; feed its
        # content through the normal pipeline so it banks like the
        # bare statement would.
        if re.search(r"\bremember\s+(?:that|this)\b", raw,
                     flags=re.IGNORECASE):
            quoted = re.findall(r"[\"']([^\"']{3,400})[\"']", raw)
            if quoted:
                raw = quoted[-1].strip()

        # Creator-locked identity facts (birthday/age/creator) cannot be
        # taught, overridden, or forgotten — drop them before any storage.
        # The refusal only surfaces for an actual write attempt; a plain
        # question ("how old are you?") must fall through to the normal
        # answer lanes instead of replying with lock wording.
        from ..identity import locked_topic, locked_refusal, is_write_intent
        topic = locked_topic(raw)
        if topic and is_write_intent(raw):
            result.setdefault("locked", []).append(locked_refusal(topic))
            return result

        forget_match = re.match(
            r"^(?:forget(?:\s+(?:that|about))?|never\s*mind(?:\s+about)?|"
            r"nm(?:\s+about)?|"
            r"stop\s+(?:remembering|storing|recording|keeping|saving|"
            r"learning)|quit\s+(?:remembering|storing|recording|"
            r"keeping|saving|learning)|"
            r"don'?t\s+(?:remember|learn)|do\s+not\s+(?:remember|learn)|"
            r"never\s+(?:remember|store|record|keep|save|learn)|"
            r"delete\s+(?:the\s+)?facts?"
            r"(?:\s+about)?)[,:]?\s*(.+)$",
            raw, flags=re.IGNORECASE)
        if forget_match:
            forget_body = forget_match.group(1).strip()
            # Discourse wrappers name the topic indirectly — 'forget
            # what I told you about the port' wants 'the port'
            # forgotten, not a fact literally containing 'what I told
            # you about'. Strip the frame so the matcher sees the
            # referent, not the wrapper.
            forget_body = re.sub(
                r"^(?:what\s+i\s+(?:told|said|mentioned|shared)\s+"
                r"(?:you\s+)?(?:about\s+)?|"
                r"(?:what|everything|all)\s+(?:that\s+)?you\s+"
                r"(?:know|remember|have|saved|stored|learned)\s+"
                r"(?:about\s+|on\s+)?|"
                r"the\s+(?:thing|stuff|part|bit)\s+(?:i\s+(?:told|"
                r"said|mentioned|shared)\s+(?:you\s+)?)?about\s+|"
                r"everything\s+i\s+(?:told|said|mentioned)\s+"
                r"(?:you\s+)?about\s+)",
                "", forget_body, flags=re.IGNORECASE).strip()
            if forget_body and re.search(
                    r"\b(?:about|on)\s*[.!?]*$", forget_body,
                    flags=re.IGNORECASE):
                # 'forget what i told you about…' trails off — the
                # referent is incomplete, not universal.
                result.setdefault("clarify", []).append(
                    "Forget what, exactly?")
                return result
            if not forget_body:
                # The whole referent WAS the wrapper — 'forget what
                # you know' / 'delete what you remember' asks for a
                # full wipe; a stop-form with no body means stop
                # keeping anything.
                if re.match(r"^(?:forget|delete)\b", raw,
                            flags=re.IGNORECASE):
                    forgotten = self.forget_all()
                    if forgotten:
                        result["forgotten"].extend(forgotten)
                    else:
                        result["forgotten_miss"] = [raw]
                else:
                    result["memory_off"] = [True]
                return result
            # A bare deictic ('forget that') names nothing to match —
            # ask for the referent instead of searching for 'that'.
            if forget_body.lower().rstrip(".!?") in {
                    "that", "it", "this", "those", "them"}:
                result.setdefault("clarify", []).append(
                    "Forget what, exactly?")
                return result
            # A universal disclosure referent — 'stop remembering
            # things I tell you' / "don't remember what I say" /
            # 'never store anything we discuss' — targets the whole
            # memory, not a fact. That's the conversation-memory
            # setting, not a topic search; flag it for the control
            # lane instead of manufacturing a miss.
            _scope_words = {
                "what", "things", "thing", "everything", "anything",
                "all", "stuff", "i", "we", "you", "u", "said", "told",
                "tell", "say", "shared", "share", "mentioned",
                "mention", "discuss", "discussed", "talk", "talked",
                "talking", "typed", "sent", "earlier", "before",
                "today", "so", "far", "in", "this", "these", "chat",
                "chats", "conversation", "conversations", "here",
                "anymore", "from", "now", "on", "going", "forward",
                "the", "that", "those", "it", "about", "of", "ve",
                "have", "has", "to", "with", "me", "my", "our", "us",
                "mine", "fact", "facts", "data", "memories", "memory",
                "details", "info", "information", "know", "knows",
                "learned", "saved", "stored"}
            _body_terms = [w for w in re.findall(
                r"[a-z']+", forget_body.lower()) if w not in _scope_words]
            if (not _body_terms and re.search(
                    r"\b(?:i|we|you|u|me|my|us|our|mine|chats?|"
                    r"conversations?|everything|anything|all)\b",
                    forget_body, flags=re.IGNORECASE)):
                if re.match(r"^(?:forget|delete)\b", raw,
                            flags=re.IGNORECASE):
                    # 'forget everything (about me)' is a purge
                    # request — wipe the learned rows, never a
                    # literal topic search. Destructive but
                    # explicitly user-directed.
                    forgotten = self.forget_all()
                    if forgotten:
                        result["forgotten"].extend(forgotten)
                    else:
                        result["forgotten_miss"] = [forget_body]
                else:
                    result["memory_off"] = [True]
                return result
            # "nm, my editor is vim" — an abortive marker followed by a
            # fresh statement is a correction, not a forget: the tail
            # re-enters the learning pipeline. True forget verbs keep
            # statement-shaped targets ('forget that my editor is
            # emacs' deletes that verbatim fact).
            abortive = bool(re.match(r"^(?:nm|never\s*mind)\b", raw,
                                     flags=re.IGNORECASE))
            if not abortive or not re.search(
                    r"\b(?:is|are|was|were|use|uses|go\s+with|set|"
                    r"make|change)\b"
                    r"|it'?s\b",
                    forget_body, flags=re.IGNORECASE):
                forgotten = self.forget(forget_body)
                result["forgotten"].extend(forgotten)
                if not forgotten:
                    # Surface the miss so the reply lane can say so
                    # honestly instead of handing a memory command to the
                    # model lane (which may not even be running).
                    result["forgotten_miss"] = [forget_body]
                return result
            raw = forget_body

        # Discourse/correction marker prefixes ride the front of a
        # statement ("actually no, my editor is neovim") — strip the
        # STACK of them once so every matcher below sees the clause
        # that carries the content. Hoisted ahead of the secret gate:
        # a marker-prefixed credential ("fyi the wifi password is
        # hunter2") banks its tail through the same paths as the bare
        # statement, so the gate must see through the marker too.
        # Topic-pivot preambles — "speaking of editors, mine is vim" —
        # the bankable clause follows the comma.
        body = re.sub(
            r"^(?:(?:speaking|talking)\s+of|"
            r"on\s+the\s+(?:topic|subject)\s+of|as\s+for|"
            r"regarding)\s+[^,;]{1,60}[,;]\s*",
            "", raw, flags=re.IGNORECASE)
        body = re.sub(
            r"^(?:(?:actually|by\s+the\s+way|btw|also|so|fyi|"
            r"note\s+(?:that|this)|note|quick\s+note|"
            r"for\s+the\s+record|just\s+so\s+you\s+know|"
            r"no|wait|sorry|nm|never\s*mind|scratch\s+that|"
            r"on\s+second\s+thought|i\s+mean(?:t)?|"
            r"and|but|then|or|plus|"
            r"ok(?:ay)?|yeah|yep|yup|well|hey|"
            r"out\s+of\s+curiosity|out\s+of\s+interest|"
            r"hmm+|hm+|um+|uh+|er+|erm+|ah+|oh)[,:\s]+)+",
            "", body, flags=re.IGNORECASE)
        # Confirmation tags — "the api keys rotate monthly, right?" —
        # ask for agreement; they aren't part of the stated value.
        body = re.sub(
            r"[,;]\s*(?:right|correct|yeah|ok(?:ay)?|huh|eh|yes|no|"
            r"isn'?t\s+it|don'?t\s+you\s+think|you\s+know)\s*[?!.]?\s*$",
            "", body, flags=re.IGNORECASE)

        # Credentials are never persisted — "remember my passcode is X"
        # must not bank the secret into facts/rules, sync it to the
        # Brain, or echo it back through prompt_context. Scoped to
        # actual learning writes: a question or statement that merely
        # mentions a credential ("my password is gone, how do I reset
        # it?") must still reach the normal answer lanes. The refusal
        # reuses the `locked` slot so the reply still surfaces.
        from ..answer_memory.validation import contains_secret
        _learn_lead = (
            r"^(?:remember|learn|fact\s*:|teach\s*:|training\s*:|"
            r"note\s*(?::|that\b|this\b)|always\b|never\b|"
            r"from\s+now\s+on|"
            r"i\s+want\s+you\s+to|you\s+should|instead\b|no\b|"
            r"correction\b|actually\b|stop\b|don't\b|do\s+not\b|"
            r"we\s+(?:decided|chose|switched|moved|migrated|changed|"
            r"agreed)|let'?s\b|the\s+plan\s+is)")
        _bare_decl = r"^(?:my|the|our)\s+.{1,40}\s+is\s+.+$"
        is_learning_write = bool(
            re.match(_learn_lead, raw, flags=re.IGNORECASE)
            or ("?" not in raw
                and re.match(_bare_decl, raw, flags=re.IGNORECASE))
            or (body != raw
                and (re.match(_learn_lead, body, flags=re.IGNORECASE)
                     or ("?" not in body
                         and re.match(_bare_decl, body,
                                      flags=re.IGNORECASE)))))
        if is_learning_write and (contains_secret(raw)
                                  or contains_secret(body)):
            result.setdefault("locked", []).append(
                "I won't store credentials or secrets in memory — "
                "Brain unlock lives in the Trainer page instead.")
            return result

        # Compound self-corrections — "I like red. Actually no, blue.
        # Never mind, make it green." Each clause is an intent statement
        # in sequence; feeding them through this same pipeline lets
        # retarget/supersession compose so only the settled value stays
        # active instead of banking the whole chain verbatim.
        clauses = self._correction_clauses(raw)
        if clauses:
            for clause in clauses:
                sub = self.learn_from_user(
                    clause,
                    project_id=project_id,
                    conversation_id=conversation_id)
                for key, value in sub.items():
                    result.setdefault(key, []).extend(value)
            return result

        # Shared-predicate compound — "the staging api and the prod api
        # are both on 443" states the same fact for two subjects;
        # splitting lets each subject supersede on its own.
        both = re.match(
            r"^((?:my|the|our)\s+[a-z0-9][a-z0-9 ._-]{0,38}?)\s+and\s+"
            r"((?:my|the|our)\s+[a-z0-9][a-z0-9 ._-]{0,38}?)\s+"
            r"(?:are|is|were|was)\s+both\s+(.+?)[.!?]?$",
            raw, flags=re.IGNORECASE)
        if both:
            for subj in (both.group(1), both.group(2)):
                sub = self.learn_from_user(
                    f"{subj} is {both.group(3)}",
                    project_id=project_id,
                    conversation_id=conversation_id)
                for key, value in sub.items():
                    result.setdefault(key, []).extend(value)
            return result

        # Coordinated declaratives — "my shell is zsh and my terminal
        # is alacritty" states two facts. Only split when EVERY part is
        # a complete subject+predicate statement; "ham and eggs" and
        # "the stack is python and sqlite" stay whole.
        and_parts = re.split(r"\s+and\s+", raw)
        if len(and_parts) > 1:
            decl = re.compile(
                r"^\w.{1,60}?\b"
                r"(?:" + self._DECL_PRED_ALT + r")\s+\S",
                re.IGNORECASE)
            if all(decl.match(p.strip()) for p in and_parts):
                split_out: dict[str, list] = {}
                for part in and_parts:
                    sub = self.learn_from_user(
                        part.strip(),
                        project_id=project_id,
                        conversation_id=conversation_id)
                    for key, value in sub.items():
                        split_out.setdefault(key, []).extend(value)
                if any(split_out.values()):
                    for key, value in split_out.items():
                        result.setdefault(key, []).extend(value)
                    return result
                # Every part parsed as a clause yet nothing banked
                # (e.g. single-noun values killed by the adjective
                # guard) — fall through so the ordinary single-decl
                # path can still record the statement verbatim.

        # Reported speech / hearsay — "i heard the port is 9090",
        # "someone said the build is green" — is acknowledged but never
        # banked: a secondhand claim is unverified by definition, and
        # treating rumor as fact would let it outrank evidence. The
        # marker surfaces an honest "noted, unverified" ack instead of
        # handing the turn to a model that might riff one.
        hear = re.match(
            r"^(?:i\s+heard|i\s+was\s+told|i'?m\s+hearing|"
            r"some(?:one|body)\s+(?:said|told\s+me)|"
            r"they\s+say|people\s+say|word\s+is|rumou?r\s+has\s+it|"
            r"apparently|supposedly|reportedly)\s*[,:—-]*\s*"
            r"(?:that\s+)?(.+)$",
            raw, flags=re.IGNORECASE)
        if hear:
            claim = re.sub(r"[.!?]+$", "", hear.group(1).strip())
            # "i heard you" is comprehension, not a reported claim —
            # require a real clause before calling it hearsay.
            if len(claim.split()) >= 2:
                result["hearsay"] = [claim]
                return result

        # Hedged first-person claims — "i think the registry mount is
        # at HKLM", "i guess the cache is stale" — are unverified like
        # hearsay: banking them would let a hunch masquerade as fact,
        # and dropping them hands the turn to a model that riffs or
        # parrots whatever it last saw. Same honest ack, same zero
        # writes, distinct marker so the reply can say "hunch" instead
        # of "secondhand".
        def _hedged(text: str) -> bool:
            m = re.match(
                r"^(i\s+(?:don'?t\s+|do\s+not\s+)?"
                r"(?:think|believe|guess|suppose|reckon|figure|"
                r"feel\s+like|suspect)|i'?m\s+(?:(?:pretty|fairly|"
                r"quite|really|not)\s+)?(?:sure|certain)|"
                r"i'?d\s+(?:say|guess|bet|imagine)|imo\b|imho\b|"
                r"(?:pretty|fairly|quite|reasonably)\s+sure|"
                r"in\s+my\s+(?:opinion|view)|my\s+(?:guess|hunch|"
                r"understanding|impression)\s+is)\s*[,:—-]*\s*"
                r"(?:that\s+)?(.+?)[.!?]?$",
                text, flags=re.IGNORECASE)
            if m is None:
                # Modal hedges and proposals — "maybe the cache ttl is
                # 300", "the timeout should be 30 seconds". A
                # suggestion is an unverified claim about the world:
                # noted, never banked. Question inversions ("should
                # we…?") are excluded by the leading-word guard.
                m = re.match(
                    r"^(?:maybe|perhaps|possibly|probably)\s*[,:]?\s*"
                    r"(.+?)[.!?]?$",
                    text, flags=re.IGNORECASE) or re.match(
                    r"^((?!(?:what|when|where|which|who|whom|whose|"
                    r"why|how|is|are|was|were|do|does|did|can|could|"
                    r"would|shall|may|might|should)\b)[a-z0-9]"
                    # `you should X` is an imperative (correction/rule
                    # lane), not a hedged claim about the world.
                    r".{1,90}?\b(?<!you\s)should(?:n'?t|\s+not)?\b"
                    r".+?)[.!?]?$",
                    text, flags=re.IGNORECASE)
                # "we agreed the API should stay REST" is a decision
                # (assertive), not a proposal — the decision lane
                # banks it. Only 'should' without a decision verb is
                # an unverified suggestion.
                if m is not None and re.search(
                        r"\b(?:decid\w+|agre\w+|settl\w+|conclud\w+|"
                        r"commit\w+|chose|chosen|elect\w+|opt\w+)\b",
                        text, flags=re.IGNORECASE):
                    m = None
            if not m:
                return False
            inner = (m.group(2) if m.lastindex and m.lastindex >= 2
                     else m.group(1)).strip()
            # "i think so" / "i'm sure" alone carry no claim — and a
            # hedged refusal ("i don't think we should") is still a
            # claim worth noting, so only the length guard applies.
            # The guard counts the embedded claim, never the lead.
            if len(inner.split()) < 2:
                return False
            if m.lastindex and m.lastindex >= 2:
                # First-person hedges keep their lead in second person
                # — 'i don't think the build is broken' must read back
                # "you don't think the build is broken", never the
                # negation-stripped claim.
                lead = re.sub(
                    r"^i\b", "you", m.group(1).strip().lower())
                lead = re.sub(
                    r"^(you|my|in my)\b", lambda mm: {
                        "you": "you", "my": "your",
                        "in my": "in your"}[mm.group(1)], lead)
                lead = lead.replace("'m ", "'re ").replace(
                    "’m ", "’re ")
                lead = {"imo": "in your opinion",
                        "imho": "in your humble opinion"}.get(
                            lead, lead)
                if re.match(r"^(?:pretty|fairly|quite|reasonably)\s+sure$",
                            lead):
                    lead = f"you're {lead}"
                claim = f"{lead} {inner}"
            else:
                claim = inner
            result["hedged"] = [claim]
            return True

        if _hedged(raw):
            return result

        with self._lock:
            fact: str | None = None
            # Marker-prefixed hedges — "actually, i think the port is
            # 9090" — the hoisted `body` is already marker-stripped, so
            # the same hedged-claim rule applies.
            if body != raw and _hedged(body):
                return result
            # Elliptical "same for X" — the user means "X gets whatever
            # value was just discussed", but the referent is genuinely
            # ambiguous and guessing banks a wrong fact (a model once
            # applied the backup schedule to the staging API). Ask for
            # the value deterministically instead.
            same_for = re.match(
                r"^same\s+(?:for|goes\s+for|with|applies\s+to)\s+"
                r"(.+?)[.!?]?$",
                body, flags=re.IGNORECASE)
            if same_for:
                result.setdefault("clarify", []).append(
                    f"Same what for {same_for.group(1).strip()}? "
                    "Tell me the value and I'll note it.")
                return result
            # Bare disagreement — "nope", "that's wrong", "not quite" —
            # is a correction signal with no replacement value. A model
            # riff fabricates agreement and invents the "correct" value
            # (live: "you're right, prod is on 8443" — contradicting
            # the banked 443 — plus a fake record-update claim). Ask
            # for the value deterministically. Only bare denials land
            # here; "no it's 8443" carries a value and takes the
            # correction path.
            if re.match(
                    r"^(?:(?:no|nope|nah|wrong|incorrect|not\s+quite|"
                    r"not\s+exactly|not\s+right)[\s.!?,]*|"
                    r"(?:(?:no|nope|nah)[,!\s]+)?"
                    r"that'?s\s+(?:wrong|incorrect|not\s+(?:right|"
                    r"correct|true|it|what\s+i\s+meant))|"
                    r"(?:(?:no|nope|nah)[,!\s]+)?"
                    r"that\s+(?:isn'?t|ain'?t|is\s+not)\s+"
                    r"(?:right|correct|true)[\s.!?,]*|"
                    r"(?:(?:no|nope|nah)[,!\s]+)?"
                    r"(?:it|that|this|they)\s+"
                    r"(?:(?:is|are|was|were)\s+not|isn'?t|ain'?t|"
                    r"aren'?t|wasn'?t|weren'?t)[\s.!?,]*)$",
                    body, flags=re.IGNORECASE):
                result.setdefault("clarify", []).append(
                    "What's the right value? Tell me and I'll "
                    "correct it.")
                return result
            # 'no <noun>' politeness idioms ("no problem", "no
            # worries") are pure acknowledgements — the marker strip
            # already consumed the 'no', leaving the bare noun. Without
            # a deterministic ack the turn falls to a model that once
            # invented a correction ("the backup runs at 2am, not 3 —
            # I'll keep that in mind").
            if (re.match(
                    r"^(?:(?:good|real|much)\s+)?"
                    r"(?:problems?|worries|worry|thanks|"
                    r"thank\s+you|rush|hurry|need|sweat|biggie|"
                    r"bother|trouble|issues?|idea|clue|way|doubt|"
                    r"harm|offense|offence|big\s+deal|one|"
                    r"good|reason|point|sense|use|chance|luck|"
                    r"dice|kidding|question|comment|"
                    r"pressure|stress|offense taken)[\s.!?,]*$",
                    body, flags=re.IGNORECASE)
                    and re.match(
                        r"^(?:(?:actually|so|well|ok|okay)\s+)?"
                        r"no[,]?\s", raw, flags=re.IGNORECASE)):
                result.setdefault("ack", []).append("no-idiom")
                return result
            # Pure social acknowledgements — 'thanks', 'got it',
            # 'you already told me that' — carry no learnable content
            # and nothing to correct; a deterministic ack beats a
            # model round-trip (which 503'd live on 'thanks').
            if re.match(
                    r"^(?:thanks|thank\s+you|got\s+it|ok(?:ay)?|cool|"
                    r"nice|right|exactly|correct|sounds\s+good|"
                    r"fair\s+enough|understood|noted|makes\s+sense|"
                    r"i\s+see|i\s+know|copy\s+(?:that|it)|roger|"
                    r"sure\s+thing|appreciated|cheers|great|"
                    r"perfect|nice\s+one|"
                    r"you\s+(?:already\s+)?told\s+me(?:\s+that)?|"
                    r"i\s+just\s+(?:told|said)\s+(?:you|that)|"
                    r"you\s+said\s+(?:that|so))[\s.!?]*$",
                    body, flags=re.IGNORECASE):
                result.setdefault("ack", []).append("social")
                return result
            for pattern in (
                r"^remember\s+that\s+(.+)$",
                r"^remember\s*:\s*(.+)$",
                r"^learn\s+that\s+(.+)$",
                r"^fact\s*:\s*(.+)$",
                r"^i\s+prefer\s+.+$",
                r"^i\s+like\s+.+$",
                r"^i\s+use\s+.+$",
                r"^i(?:'m| am)\s+using\s+.+$",
                r"^my\s+.{1,40}\s+is\s+.+$",
            ):
                match = re.match(pattern, body, flags=re.IGNORECASE)
                if match:
                    cand = (match.group(1).strip() if match.lastindex
                            else body.rstrip(".!?"))
                    # Negated copula — "my editor is not emacs" is a
                    # retraction/correction for the neg lane, never a
                    # verbatim fact.
                    if re.search(
                            r"\b(?:is|are|was|were)(?:n't|"
                            r"\s+(?:not|no\s+longer|never))\b",
                            cand, flags=re.IGNORECASE):
                        break
                    fact = cand
                    # A marker-prefixed preference restatement —
                    # "actually i prefer green now" — corrects the
                    # same-class preference fact ("my favorite color is
                    # blue"), it isn't a brand-new fact. Try the
                    # preference retarget before banking it fresh.
                    if body != raw:
                        pm = re.match(
                            r"^i\s+(prefer|like|love)\s+(.+?)"
                            r"(?:\s+(?:now|instead|today|this\s+time))?"
                            r"[.!?]?$",
                            fact, flags=re.IGNORECASE)
                        if pm:
                            tgt = self._retarget_latest_fact(
                                pm.group(2), result["superseded"],
                                preference_only=True)
                            # No unique same-class referent — bank the
                            # restated preference itself, trailer-free,
                            # rather than corrupting an unrelated fact.
                            fact = (tgt if tgt is not None
                                    else f"i {pm.group(1).lower()} "
                                         f"{pm.group(2)}")
                    break
            if fact is None:
                # An imperative-led body is a request, not a statement —
                # 'explain what a mutex is in one sentence' would otherwise
                # parse as subject 'explain what a mutex' + is + 'in one
                # sentence' and bank the request as a fact while the ack
                # swallows the answer. The semantic layer's imperative-lead
                # class is the same vocabulary the action lanes use, so the
                # classification stays aligned upstream and here. Value-set
                # imperatives ('set the theme to dark') are exempt — their
                # dedicated matchers below still run.
                from ..context.semantics import _ACTION_LEAD_RE
                # A copula after the lead word makes the clause a
                # statement, not a command — "review cadence is
                # weekly" leads with 'review' but asserts a property;
                # "review my PRs" (no copula) stays imperative. Verbs
                # whose object is itself a clause ("remember the port
                # is 8080") keep the imperative read.
                imperative_body = bool(
                    _ACTION_LEAD_RE.match(body)) and not re.match(
                        r"^(?!(?:remember|remind|tell|note|show|"
                        r"explain|describe|check|make|let|see|keep|"
                        r"set|use|run|stop|start|get|do|please)\b)"
                        r"[a-z]+(?:\s+[a-z0-9][a-z0-9 '._-]{0,38}?)?\s+"
                        r"(?:is|are|was|were|isn't|aren't|ain't)\b",
                        body, flags=re.IGNORECASE)
                switch = re.match(
                    r"^we\s+(?:switched|moved|migrated|changed)\s+"
                    r"([a-z0-9][a-z0-9 ._-]{0,38}?)\s+to\s+(.+)$",
                    body, flags=re.IGNORECASE,
                )
                corr_body = ""
                if switch:
                    subj = self._clean_subject(switch.group(1))
                    value = self._clean_value(switch.group(2))
                    if self._fact_subject_ok(subj) and value:
                        fact = f"{subj} uses {value}"
                elif fact is None:
                    # Stated plan — "we're switching to mysql next
                    # week" declares intent rather than current use;
                    # bank it as the plan it is so the turn gets an
                    # honest ack instead of a model riff.
                    plan = re.match(
                        r"^we(?:'re|\s+are|\s+will)\s+"
                        r"(switch(?:ing)?|mov(?:e|ing)|migrat(?:e|ing)|"
                        r"chang(?:e|ing)|transition(?:ing)?|"
                        r"upgrad(?:e|ing)|downgrad(?:e|ing))"
                        r"(?:\s+((?:(?:the|a|an|my|our)\s+)?"
                        r"[a-z0-9][a-z0-9 ._-]{0,38}?))?\s+"
                        r"(?:over\s+)?to\s+(.+?)[.!?]?$",
                        body, flags=re.IGNORECASE)
                    if plan:
                        obj = self._clean_subject(plan.group(2) or "")
                        value = self._clean_value(plan.group(3))
                        verb = plan.group(1).lower()
                        if not verb.endswith("ing"):
                            verb = (verb[:-1] if verb.endswith("e")
                                    else verb) + "ing"
                        if value:
                            fact = (f"we are {verb} "
                                    f"{obj + ' ' if obj else ''}"
                                    f"to {value}")
                    # Corrections — "correction: the port is 5433",
                    # "no, I meant SQLite", "actually it was Redis not
                    # Postgres". The 'X not Y' form supersedes the fact
                    # carrying Y when Y uniquely identifies one.
                    corr = re.match(
                        r"^(?:correction|i\s+meant|no[,]?\s+i\s+meant|"
                        r"to\s+clarify|sorry[,]?\s*i\s+meant)[,:]?\s*(.+)$",
                        body, flags=re.IGNORECASE,
                    )
                    nyc = None if imperative_body else re.match(
                        r"^(?:actually[,]?\s+|no[,]?\s+)?"
                        r"(it|that|this|the\s+[a-z0-9][a-z0-9 ._-]{0,34}?|"
                        r"[a-z0-9][a-z0-9 ._-]{0,34}?)\s+"
                        r"(?:is|was)\s+(.+?)\s+not\s+(.+?)[.!?]?$",
                        body, flags=re.IGNORECASE,
                    )
                    if nyc:
                        subj_txt = self._clean_subject(nyc.group(1))
                        new_v = self._clean_value(nyc.group(2))
                        old_v = self._clean_value(nyc.group(3))
                        fact = self._correct_fact_value(old_v, new_v)
                        if (
                            fact is None
                            and subj_txt
                            and subj_txt.casefold() not in {"it", "that", "this"}
                            and self._fact_subject_ok(subj_txt)
                        ):
                            # "actually the port was 5433 not 8080" names
                            # its subject — the corrected statement can be
                            # learned even when no old-value fact exists.
                            fact = f"{subj_txt} is {new_v}"
                    elif not imperative_body and (neg := re.match(
                            r"^(?:actually[,]?\s+|no[,]?\s+|wait[,]?\s+)?"
                            r"(it|that|this|(?:my|the|our)\s+[a-z0-9]"
                            r"[a-z0-9 ._-]{0,34}?|"
                            r"[a-z0-9][a-z0-9 ._-]{0,34}?)\s+"
                            r"(?:is|are|was|were)"
                            r"(?:\s+not|\s+no\s+longer|n'?t)\s+(.+?)"
                            r"(?:\s*,\s*(?:it'?s|it\s+is)\s+(.+?))?"
                            r"\s*(?:anymore|any\s+more|any\s+longer)?"
                            r"[.!?]?$",
                            body, flags=re.IGNORECASE)):
                        # "X is not Y, it's Z" corrects to Z; bare
                        # "X isn't Y (anymore)" retracts the unique fact
                        # carrying Y. Storing the negated sentence
                        # verbatim ('cache ttl is not 300, it's 600')
                        # answers nothing and misleads recall.
                        subj_txt = self._clean_subject(neg.group(1))
                        old_v = self._clean_value(neg.group(2))
                        new_v = (self._clean_value(neg.group(3))
                                 if neg.group(3) else "")
                        if new_v:
                            fact = self._correct_fact_value(old_v, new_v)
                            if (
                                fact is None
                                and subj_txt
                                and subj_txt.casefold() not in {
                                    "it", "that", "this"}
                                and self._fact_subject_ok(subj_txt)
                            ):
                                fact = f"{subj_txt} is {new_v}"
                        else:
                            # 'isn't AT 10:30' negates the time the
                            # fact states — a leading prep is syntax,
                            # not part of the value being denied.
                            neg_v = re.sub(
                                r"^(?:at|on|in|for|of|to)\s+", "",
                                old_v, flags=re.IGNORECASE)
                            retracted = self._retract_fact_value(
                                neg_v or old_v)
                            result["forgotten"].extend(retracted)
                            if not retracted:
                                # The retraction named a value nothing
                                # stores — surface the miss so the reply
                                # is an honest "nothing to forget", not
                                # a model turn.
                                result["forgotten_miss"] = [
                                    f"{subj_txt or 'it'} is {old_v}"]
                        if fact is None:
                            # Negated statements are constraints, not
                            # facts — whether the retraction hit or
                            # missed, never let a downstream matcher
                            # bank "X is not Y" verbatim.
                            body = ""
                    elif not imperative_body and (disuse := re.match(
                            r"^i\s+(?:no\s+longer|don't|do\s+not|dont|"
                            r"stopped|quit)\s+"
                            r"(?:use|using|run|running|like|liking|"
                            r"want|needing?|work\s+with)\s+"
                            # "i don't want you to X" is a prohibition
                            # on Nexus (rule lane), not the user's own
                            # disuse — the retracted object must not be
                            # 'you'.
                            r"(?!you\b)"
                            r"(.+?)\s*(?:anymore|any\s+more)?[.!?]?$",
                            body, flags=re.IGNORECASE)):
                        # "i no longer use slack for comms" retracts
                        # the unique fact ending in the tool — the
                        # purpose tail ('for comms') is context, not
                        # part of the denied value.
                        obj = self._clean_value(re.sub(
                            r"\s+(?:for|as|in|on)\s+.+$", "",
                            disuse.group(1), flags=re.IGNORECASE))
                        retracted = self._retract_fact_value(obj)
                        if not retracted:
                            retracted = self._retract_fact_contains(obj)
                        result["forgotten"].extend(retracted)
                        if not retracted:
                            result["forgotten_miss"] = [
                                f"i use {obj or disuse.group(1)}"]
                        body = ""
                    elif corr:
                        inner = corr.group(1).strip()
                        inner_nyc = re.match(
                            r"^(.+?)\s+not\s+(.+?)[.!?]?$", inner,
                            flags=re.IGNORECASE)
                        if inner_nyc:
                            new_v = self._clean_value(inner_nyc.group(1))
                            old_v = self._clean_value(inner_nyc.group(2))
                            fact = self._correct_fact_value(old_v, new_v)
                        elif fact is None:
                            # Re-body the correction so the normal
                            # declarative/decision pipeline canonicalizes
                            # it ("the store uses Redis" -> "store uses
                            # Redis"); corr_body is the verbatim fallback
                            # when nothing structured matches.
                            body = inner
                            imperative_body = bool(
                                _ACTION_LEAD_RE.match(inner)) and not re.match(
                                    r"^(?!(?:remember|remind|tell|note|"
                                    r"show|explain|describe|check|make|"
                                    r"let|see|keep|set|use|run|stop|"
                                    r"start|get|do|please)\b)"
                                    r"[a-z]+(?:\s+[a-z0-9]"
                                    r"[a-z0-9 '._-]{0,38}?)?\s+"
                                    r"(?:is|are|was|were|isn't|aren't|"
                                    r"ain't)\b",
                                    inner, flags=re.IGNORECASE)
                            corr_body = inner.rstrip(".!?")
                if fact is None:
                    # Reported copula decisions — "we decided the cache
                    # ttl is 300", "i agreed the deadline was Friday".
                    # The named subject keeps the supersession slot, so a
                    # restated value retires the earlier one.
                    dec_fact = re.match(
                        r"^(?:we|i)\s+(?:decided|agreed|settled|chose|"
                        r"picked)\s+(?:that\s+|on\s+)?"
                        r"(?:the\s+|our\s+|a\s+|an\s+)?"
                        r"([a-z0-9][a-z0-9 ._-]{0,38}?)\s+"
                        r"(?:is|are|was|were|should\s+be|to\s+be|"
                        r"stays?|remains?)\s+(.+?)[.!?]?$",
                        body, flags=re.IGNORECASE,
                    )
                    if dec_fact:
                        subj = self._clean_subject(dec_fact.group(1))
                        value = self._clean_value(dec_fact.group(2))
                        # A modal tail isn't a subject — 'we agreed the
                        # API should stay REST' belongs to the
                        # should/must agreement lane below, not copula
                        # canonicalization ('API should is REST').
                        if subj.split()[-1].casefold() in {
                                "should", "must", "will", "shall", "can",
                                "could", "would", "to", "be"}:
                            subj = ""
                        if self._fact_subject_ok(subj) and value:
                            fact = f"{subj} is {value}"
                if fact is None:
                    # Decision statements — "we decided to use SQLite for
                    # the store", "the plan is Postgres for production",
                    # "let's go with Redis". Canonicalize to
                    # "subject uses value" so a revised decision retires
                    # the earlier one in the same slot.
                    dec = re.match(
                        r"^(?:we\s+(?:decided|chose|settled|opted|picked|went)|"
                        r"(?:let'?s|let\s+us)\s+(?:go|decide|settle|opt)|"
                        r"the\s+plan\s+is)\s*"
                        r"(?:to\s+use|to\s+go\s+with|on|with|for)?\s*"
                        r"([a-z0-9][a-z0-9 ._+/#-]{0,38}?)"
                        r"(?:\s+(?:for|as|in|on)\s+(?:the\s+|our\s+|a\s+)?"
                        r"([a-z0-9][a-z0-9 ._-]{0,38}?))?[.!?]?$",
                        body, flags=re.IGNORECASE,
                    )
                    if dec:
                        value = self._clean_value(dec.group(1))
                        subj = self._clean_subject(dec.group(2) or "")
                        if not subj:
                            subj = "this project"
                        # "decided to refactor X" is an action decision,
                        # not a tool/choice statement — skip it rather
                        # than canonicalize garbage.
                        if (value and self._fact_subject_ok(subj)
                                and not re.match(
                                    r"^(?:option|to|the|that|a|an|we|it|i)\b",
                                    value, flags=re.IGNORECASE)):
                            fact = f"{subj} uses {value}"
                    if fact is None:
                        # "we agreed (that) the API should stay REST"
                        agreed = re.match(
                            r"^we\s+agreed\s+(?:that\s+)?"
                            r"(?:the\s+|our\s+)?"
                            r"([a-z0-9][a-z0-9 ._-]{0,38}?)\s+"
                            r"(should|must|will|shall)\s+"
                            r"(stay|be|remain|keep|use|have)\s+(.+)$",
                            body, flags=re.IGNORECASE,
                        )
                        if agreed:
                            subj = self._clean_subject(agreed.group(1))
                            value = self._clean_value(agreed.group(4))
                            if self._fact_subject_ok(subj) and value:
                                fact = (f"{subj} {agreed.group(2).lower()} "
                                        f"{agreed.group(3).lower()} {value}")
                if fact is None:
                    # "call the build server titan" — naming a thing,
                    # not phoning it. Pronoun-led referents ('call me
                    # later'), idioms ('call it a day'), and lone
                    # time-word values ('call the vet tomorrow') stay
                    # out.
                    cal = re.match(
                        r"^(?:call|name|dub)\s+"
                        r"(?:the\s+|my\s+|our\s+|a\s+|an\s+)?"
                        r"([a-z0-9][a-z0-9 ._-]{0,38}?)\s+"
                        r"([a-z0-9][a-z0-9._-]{1,38}?)[.!?]?$",
                        body, flags=re.IGNORECASE)
                    if cal:
                        subj_c = self._clean_subject(cal.group(1))
                        val_c = self._clean_value(cal.group(2))
                        lead = subj_c.split()[0] if subj_c else ""
                        raw_c = cal.group(2).strip().lower()
                        idiom = re.fullmatch(
                            r"(?:a|an)\s+.*|off|quits|even|done|good|"
                            r"here", raw_c) or re.search(
                            r"\b(?:a|an)$", cal.group(1).strip().lower())
                        if (lead in {"it", "that", "this"} and val_c
                                and not idiom):
                            fact = self._retarget_latest_fact(
                                val_c, result["superseded"])
                        elif (self._fact_subject_ok(subj_c) and val_c
                                and lead not in {
                                    "me", "you", "your", "him", "her",
                                    "them", "us", "it", "its", "that",
                                    "this", "anyone", "someone"}
                                and not re.fullmatch(
                                    r"now|today|tomorrow|tonight|later|"
                                    r"soon|tonite|next\s+\w+|at\s+.*|"
                                    r"(?:mon|tue|wed|thu|fri|sat|sun)"
                                    r"[a-z]*",
                                    val_c, flags=re.IGNORECASE)):
                            fact = f"{subj_c} is {val_c}"
                if fact is None:
                    # Subject-led update: "Orion moved to Redis",
                    # "Orion migrated off Postgres to Redis".
                    subj_switch = None if imperative_body else re.match(
                        r"^(?:project\s+)?([a-z0-9][a-z0-9 ._-]{0,38}?)\s+"
                        r"(?:(?:was|were|got|is|are|has\s+been|"
                        r"have\s+been|being|just)\s+)*"
                        r"(switched|moved|migrated|changed|pushed)"
                        r"(?:\s+(?:off|from|away from)\s+[a-z0-9][a-z0-9 ._-]{0,38}?)?"
                        r"\s+to\s+(.+)$",
                        body, flags=re.IGNORECASE,
                    )
                    decl = None
                    subj = ""
                    if subj_switch:
                        subj = self._clean_subject(subj_switch.group(1))
                        verb = subj_switch.group(2).lower()
                        value = self._clean_value(subj_switch.group(3))
                        # A move on a fact-tracked entity restates its
                        # slot — "the standup moved to 10am" updates
                        # 'standup is at 9am', it isn't 'standup uses
                        # 10am'. Retarget keeps the original predicate;
                        # only genuinely new entities mint a fresh slot.
                        if self._fact_subject_ok(subj) and value:
                            fact = self._retarget_latest_fact(
                                value, result["superseded"],
                                subject_hint=subj)
                        if (fact is None
                                and self._fact_subject_ok(subj)
                                and value):
                            # Time/date-shaped moves describe WHEN the
                            # entity happens — "deadline was moved to
                            # monday" mints 'deadline is monday'; tool
                            # moves ("Orion moved to Redis") mint 'uses'.
                            # 'changed' replaces an attribute value —
                            # "my timezone changed to pst" mints
                            # 'my timezone is pst', never a tool slot;
                            # switched/moved/migrated keep 'uses'.
                            my_scoped = subj.lower().startswith(
                                ("my ", "our "))
                            if re.fullmatch(
                                    r"(?:next\s+|this\s+|every\s+)?"
                                    r"(?:mon|tue|wed|thu|fri|sat|sun)"
                                    r"[a-z]*|tomorrow|tonight|today|"
                                    r"[a-z]+\s+\d{1,2}(?:st|nd|rd|th)?|"
                                    r".*(?:am|pm)\b|\d{1,2}:\d{2}",
                                    value, flags=re.IGNORECASE) \
                                    or (verb == "changed" and my_scoped):
                                fact = f"{subj} is {value}"
                            else:
                                fact = f"{subj} uses {value}"
                    elif not imperative_body:
                        decl = re.match(
                            r"^(?:project\s+)?"
                            # Article is an optional prefix — the
                            # subject can never reduce to a bare 'the'/
                            # 'my', which would let a noun-final
                            # subject word get eaten as the predicate
                            # ('the deploy ships friday').
                            r"((?:(?:my|the|our|a|an|your|its|their)\s+)?"
                            r"[a-z0-9][a-z0-9 ._-]{0,38}?)\s+"
                            r"(" + self._DECL_PRED_ALT + r")\s+(.+)$",
                            body, flags=re.IGNORECASE,
                        )
                        if decl:
                            subj = self._clean_subject(decl.group(1))
                            if self._fact_subject_ok(subj):
                                pred = re.sub(
                                    r"\s+", " ", decl.group(2).strip().lower())
                                # Naming predicates share the 'is' slot —
                                # "the label is now named zeta" restates
                                # "the label is alpha", and a later plain
                                # "the label is omega" must retire it.
                                naming = bool(re.search(
                                    r"(?:called|named|known as)$",
                                    pred))
                                if naming:
                                    pred = "is"
                                value = self._clean_value(decl.group(3))
                                if value:
                                    fact = f"{subj} {pred} {value}"
                                if (value and body != raw
                                        and not naming):
                                    # Marker-led restatement of a
                                    # tracked entity ("actually the
                                    # laptop is pulsar" while 'my
                                    # laptop is named quasar' lives) —
                                    # retarget the entity's slot rather
                                    # than forking a parallel fact.
                                    tgt = self._retarget_latest_fact(
                                        value, result["superseded"],
                                        subject_hint=subj)
                                    if tgt is not None:
                                        fact = tgt
                                if naming and value:
                                    # "the render box is now named
                                    # titan" restates the entity's name
                                    # slot ('render box name is
                                    # cobalt') — retarget it instead of
                                    # forking a parallel 'render box'
                                    # fact.
                                    tgt = self._retarget_latest_fact(
                                        value, result["superseded"],
                                        subject_hint=subj)
                                    if tgt is not None:
                                        fact = tgt
                                if (pred in {"is", "are"} and value
                                        and not naming
                                        # Marker context legitimizes a
                                        # short value — "actually the
                                        # laptop is pulsar" restates the
                                        # slot deliberately; the
                                        # adjective guard only gates
                                        # unmarked statements.
                                        and body == raw
                                        and not (
                                            re.search(
                                                r"[\dA-Z/\\]|(?:^|\s)v\d",
                                                decl.group(3).strip())
                                            or len(value.split()) >= 2
                                            or value.lower() in
                                            self._SETTINGS_VALUES
                                            or re.match(
                                                r"^(?:mon|tues|wednes|"
                                                r"thurs|fri|satur|sun)"
                                                r"day\b|^(?:jan|feb|mar|"
                                                r"apr|may|jun|jul|aug|"
                                                r"sep|oct|nov|dec)"
                                                r"[a-z]*\b|^(?:tomorrow|"
                                                r"tonight|today|eod|eow)"
                                                r"\b",
                                                value,
                                                flags=re.IGNORECASE))):
                                    # Bare 'is' only captures values that
                                    # look like facts — "the port is 5433",
                                    # "the deadline is Friday" — not
                                    # adjectives ("the movie is great",
                                    # "the answer is no").
                                    fact = None
                if fact is None:
                    # Wanted format/medium — "i want the report delivered
                    # as csv, not pdf", "i need the logs in json". The
                    # attribute is implicit; canonical "subject is value"
                    # answers 'what format did i want the report in' and
                    # keeps the supersession slot aligned with later
                    # restatements.
                    want = re.match(
                        r"^i\s+(?:want|need|would\s+like|prefer)\s+"
                        r"(?:the\s+|a\s+|an\s+|my\s+|our\s+)?"
                        r"([a-z0-9][a-z0-9 ._-]{0,38}?)\s+"
                        r"(?:delivered|sent|saved|written|exported|"
                        r"formatted|rendered|generated|prepared|"
                        r"provided|given|shown|displayed|output|"
                        r"returned|produced|created|built|done|made|"
                        r"kept|stored)?\s*(?:as|in)\s+"
                        r"([a-z0-9][a-z0-9 ._-]{0,38}?)"
                        r"(?:\s*[,.]?\s*(?:not|rather\s+than|instead\s+of)"
                        r"\s+.+)?[.!?]?$",
                        body, flags=re.IGNORECASE,
                    )
                    if want:
                        subj = self._clean_subject(want.group(1))
                        value = self._clean_value(want.group(2))
                        if self._fact_subject_ok(subj) and value:
                            fact = f"{subj} is {value}"
                if fact is None:
                    # Imperative value-sets — "use port 8080", "set the
                    # theme to dark". Canonical "subject is value" keeps
                    # the supersession slot identical to declarative
                    # updates ("the port is 8090").
                    imp = re.match(
                        r"^(?:use|set|pick|choose|go\s+with|go\s+for|"
                        r"bump|raise|lower|increase|decrease|drop)\s+"
                        r"(?:the\s+|our\s+|a\s+|an\s+)?"
                        r"([a-z0-9][a-z0-9 ._-]{0,28}?)\s+"
                        r"(?:to|as|is|=)\s+(.+?)[.!?]?$",
                        body, flags=re.IGNORECASE)
                    if imp:
                        subj = self._clean_subject(imp.group(1))
                        value = self._clean_value(imp.group(2))
                        # "use mercury as the name" renames the latest
                        # fact — 'the name/label/title' is a referential
                        # slot word, not a value; banking "mercury is
                        # the name" inverts the statement.
                        if re.fullmatch(
                                r"(?:the\s+)?"
                                r"(?:name|label|title)s?", value,
                                flags=re.IGNORECASE):
                            fact = self._retarget_latest_fact(
                                subj, result["superseded"])
                        elif self._fact_subject_ok(subj) and value:
                            fact = f"{subj} is {value}"
                    else:
                        # Bare "use X <literal>" — the literal must look
                        # like a stated value (digits, versions, paths,
                        # identifiers) or "use the dark theme" parses as
                        # 'dark is theme'.
                        imp2 = re.match(
                            r"^(?:use|set|pick|choose|go\s+with|go\s+for)\s+"
                            r"(?:the\s+|our\s+)?([a-z0-9]{2,24})\s+"
                            r"([a-z0-9][a-z0-9 _+./\\#:-]{0,38}?)[.!?]?$",
                            body, flags=re.IGNORECASE)
                        if imp2 and re.search(r"[\d_+./\\#-]",
                                              imp2.group(2)):
                            subj = self._clean_subject(imp2.group(1))
                            value = self._clean_value(imp2.group(2))
                            if self._fact_subject_ok(subj) and value:
                                fact = f"{subj} is {value}"
                        if fact is None:
                            # "make the standup 10:30", "move the
                            # meeting to 4pm" — connector-less value
                            # sets; the value must look literal or
                            # 'make the report short' banks adjectives.
                            imp3 = re.match(
                                r"^(?:make|change|move|schedule|shift|"
                                r"push)\s+(?:the\s+|my\s+|our\s+|a\s+)?"
                                r"([a-z0-9][a-z0-9 ._-]{0,28}?)\s+"
                                r"(?:to\s+)?"
                                r"([a-z0-9][a-z0-9 _+./\\#:-]{0,38}?)"
                                r"[.!?]?$",
                                body, flags=re.IGNORECASE)
                            if imp3 and re.search(
                                    r"[\d_+./\\#:-]", imp3.group(2)):
                                subj = self._clean_subject(imp3.group(1))
                                value = self._clean_value(imp3.group(2))
                                if self._fact_subject_ok(subj) and value:
                                    fact = (
                                        self._retarget_latest_fact(
                                            value,
                                            result["superseded"],
                                            subject_hint=subj)
                                        or f"{subj} is {value}")
                if fact is None:
                    # Bare re-set of the last stated value — "actually
                    # make that 8090", "no, set it to 9000". The newest
                    # active fact is the referent; the retarget retires
                    # it directly so stale values never resurface.
                    # 'use X' retargets only literal-looking values —
                    # "use the dark theme" is a new instruction, not a
                    # re-set of the last fact.
                    reset = re.match(
                        r"^(?:(?:actually|no|wait|instead|never\s*mind|"
                        r"scratch\s+that|on\s+second\s+thought|sorry)"
                        r"[,]?\s+)*"
                        r"(?:make\s+(?:it|that|them)|set\s+(?:it|that)"
                        r"\s+to|change\s+(?:it|that)\s+to)\s+"
                        r"(.+?)[.!?]?$",
                        raw, flags=re.IGNORECASE)
                    if not reset:
                        use_reset = re.match(
                            r"^(?:(?:actually|no|wait|instead)[,]?\s+)*"
                            r"use\s+(.+?)[.!?]?$",
                            raw, flags=re.IGNORECASE)
                        if use_reset and re.search(
                                r"[\d_+./\\#-]", use_reset.group(1)):
                            reset = use_reset
                    if not reset:
                        # "no not cobalt, it's onyx" — the negated value
                        # is the old one, 'it's X' supplies the new. The
                        # explicit contrast legitimizes word values the
                        # bare-'it's' lane would reject.
                        nyc = re.match(
                            r"^(?:(?:actually|no|wait|instead|sorry|"
                            r"never\s*mind|nm)[,]?\s+)*"
                            r"(?:not|nope|nah|not\s+really)\s+"
                            r"([^,;—]{1,40}?)[,;—]\s*"
                            r"(?:it'?s|it\s+is|the\s+\w+\s+is)\s+"
                            r"(.+?)[.!?]?$",
                            raw, flags=re.IGNORECASE)
                        if nyc:
                            # The negated value names its own fact —
                            # "not cobalt, it's onyx" corrects the
                            # cobalt row, not whatever happens to be
                            # newest.
                            fact = self._correct_fact_value(
                                self._clean_value(nyc.group(1)),
                                nyc.group(2))
                            if fact is None:
                                fact = self._retarget_latest_fact(
                                    nyc.group(2), result["superseded"])
                    if not reset and fact is None:
                        # "i changed my mind (about the X) — it's Y" —
                        # the lead names the referent, the tail states
                        # the new value. Strict hint matching: a named
                        # referent that has no fact must NOT retarget
                        # the latest unrelated row.
                        cmm = re.match(
                            r"^i\s+(?:changed|change)\s+my\s+mind\b"
                            r"(?:\s+about\s+(?:the\s+|my\s+|our\s+)?"
                            r"([a-z0-9][a-z0-9 '_-]{0,38}?))?"
                            r"\s*[-—,;:]\s*(.+?)[.!?]?$",
                            raw, flags=re.IGNORECASE)
                        if cmm:
                            tail = cmm.group(2).strip()
                            cmv = re.match(
                                r"^(?:it'?s|it\s+is)\s+(.+?)"
                                r"(?:\s+(?:now|instead|today))?[.!?]?$",
                                tail, flags=re.IGNORECASE)
                            val = (cmv.group(1) if cmv else tail).strip()
                            if val and re.search(r"[\d_+./\\#-]", val):
                                hint = (self._clean_subject(cmm.group(1))
                                        if cmm.group(1) else None)
                                fact = self._retarget_latest_fact(
                                    val, result["superseded"],
                                    subject_hint=hint)
                    if not reset and fact is None:
                        # "the deploy region used to be us-east-1 but
                        # now it's eu-west-1" — temporal supersession:
                        # 'used to be' names the old value, the 'now'
                        # tail carries the new one. Value-match first,
                        # then the named subject's slot.
                        utb = re.match(
                            r"^(?:(?:the\s+|my\s+|our\s+)?"
                            r"([a-z0-9][a-z0-9 '_-]{0,38}?)\s+)?"
                            r"used\s+to\s+be\s+(.+?)\s*"
                            r"(?:,\s*|\s+)(?:but\s+|now\s+|"
                            r"it'?s\s+|it\s+is\s+)(.+?)[.!?]?$",
                            raw, flags=re.IGNORECASE)
                        if utb:
                            old_v = self._clean_value(utb.group(2))
                            # The separator word may leave residue —
                            # "but now it's X" splits on 'but' and the
                            # rest still carries "now it's".
                            new_v = self._clean_value(re.sub(
                                r"^(?:now\s+)?"
                                r"(?:it'?s|it\s+is|it\s+was|"
                                r"they'?re)\s+", "",
                                utb.group(3), flags=re.IGNORECASE))
                            fact = self._correct_fact_value(old_v, new_v)
                            if fact is None and utb.group(1):
                                hint = self._clean_subject(utb.group(1))
                                fact = self._retarget_latest_fact(
                                    new_v, result["superseded"],
                                    subject_hint=hint)
                            if (fact is None and new_v
                                    and utb.group(1)):
                                # No prior fact to supersede — the
                                # 'now' value is still a current fact:
                                # "the oncall used to be priya, now
                                # it's sam" banks 'oncall is sam'
                                # even when 'priya' was never taught.
                                hint = self._clean_subject(
                                    utb.group(1))
                                if self._fact_subject_ok(hint):
                                    fact = f"{hint} is {new_v}"
                    if not reset and fact is None:
                        # "i thought the port was 8080 but it was
                        # 8081" — a corrected belief. The 'but it was'
                        # tail carries the true value; bank it on the
                        # named subject's slot.
                        tht = re.match(
                            r"^i\s+(?:thought|assumed|believed|"
                            r"figured)\s+(?:(?:the|my|our)\s+)?"
                            r"([a-z0-9][a-z0-9 ._-]{0,38}?)\s+"
                            r"(?:was|were|is|are)\s+(.+?)\s*,?\s*"
                            r"but\s+(?:it\s+(?:was|is)|it'?s|"
                            r"they\s+(?:were|are)|they'?re|"
                            r"actually\s+(?:it\s+(?:was|is)|it'?s))\s+"
                            r"(.+?)[.!?]?$",
                            raw, flags=re.IGNORECASE)
                        if tht:
                            old_v = self._clean_value(tht.group(2))
                            new_v = self._clean_value(tht.group(3))
                            hint = self._clean_subject(tht.group(1))
                            fact = self._correct_fact_value(
                                old_v, new_v)
                            if fact is None:
                                fact = self._retarget_latest_fact(
                                    new_v, result["superseded"],
                                    subject_hint=hint)
                            if fact is None and new_v and hint:
                                fact = f"{hint} is {new_v}"
                    if not reset and fact is None:
                        # "we used jira before, now it's linear" — past
                        # usage superseded by present usage. The old
                        # tool matches an existing 'we/i use X' fact for
                        # supersession; with none, the new usage banks
                        # fresh.
                        usb = re.match(
                            r"^(we|i|they|the\s+team)\s+used\s+"
                            r"(.+?)\s+before\s*[,;]?\s*(?:but\s+)?now\s+"
                            r"(?:it'?s|it\s+is|we\s+use|i\s+use|"
                            r"they\s+use)\s+(.+?)[.!?]?$",
                            raw, flags=re.IGNORECASE)
                        if usb:
                            actor = usb.group(1).lower()
                            actor = ("the team" if actor == "the team"
                                     else actor)
                            old_v = self._clean_value(usb.group(2))
                            new_v = self._clean_value(usb.group(3))
                            fact = self._correct_fact_value(
                                old_v, new_v)
                            if fact is None and new_v:
                                fact = f"{actor} use {new_v}"
                    if not reset and fact is None:
                        # Referential "it's X" after a correction marker —
                        # "never mind, it's 60" re-states the latest
                        # fact's value. Literal-shaped values only
                        # (digits/versions/paths): "it's fine" is an
                        # acknowledgement, not a correction.
                        its = re.match(
                            r"^(?:(?:actually|no|wait|instead|"
                            r"never\s*mind|scratch\s+that|sorry|nm)"
                            r"[,]?\s+)*it'?s\s+(.+?)[.!?]?$",
                            raw, flags=re.IGNORECASE)
                        if (its and re.search(
                                r"[\d_+./\\#-]", its.group(1))):
                            fact = self._retarget_latest_fact(
                                its.group(1), result["superseded"],
                                allow_latest_fallback=False)
                            if (fact is None
                                    and self._last_retarget_ambiguous):
                                result.setdefault(
                                    "clarify", []).append(
                                    f"Correct what to "
                                    f"{its.group(1).strip()}? Name "
                                    "the thing and I'll fix it.")
                            elif (fact is None
                                    and self._last_retarget_already):
                                result.setdefault(
                                    "restated", []).append(
                                    self._last_retarget_already)
                        if fact is None:
                            # Referential renames — "the name changed —
                            # it's now called vector", "the label is now
                            # named zeta", "renamed to X", "call it X",
                            # "rename it to X", "use X as the name".
                            # 'now' is required before called/named —
                            # "it's called being careful" is idiom, not
                            # a rename. An explicit subject rewrites its
                            # own slot; 'it'/the name' retargets the
                            # latest fact.
                            ren = re.search(
                                r"\b(?:it'?s|it\s+is|this\s+is|"
                                r"that\s+is|the\s+name\s+is)\s+"
                                r"(?:now\s+)?(?:called|named)\s+"
                                r"([a-z0-9][a-z0-9 ._-]{0,38}?)"
                                r"(?:\s+(?:now|instead))?[.!?]?\s*$",
                                raw, flags=re.IGNORECASE)
                            # 'now' legitimizes inline — or a correction
                            # marker in the head clause ("that's wrong —
                            # it's called cobalt") supplies the intent.
                            if ren and (
                                    re.search(r"\bnow\b",
                                              raw[:ren.end()],
                                              flags=re.IGNORECASE)
                                    or re.search(
                                        r"\b(?:actually|wait|sorry|"
                                        r"never\s*mind|nm|wrong|"
                                        r"incorrect|not\s+right|"
                                        r"changed|renamed|updated?)\b",
                                        raw[:ren.start()],
                                        flags=re.IGNORECASE)):
                                fact = self._retarget_latest_fact(
                                    ren.group(1), result["superseded"])
                        if fact is None:
                            ren_subj = re.search(
                                r"\b(?:(the|my|our)\s+)?"
                                r"([a-z][a-z0-9 '_-]{0,38}?)\s+"
                                r"(?:is|are)\s+now\s+"
                                r"(?:called|named)\s+"
                                r"([a-z0-9][a-z0-9 ._-]{0,38}?)"
                                r"(?:\s+(?:now|instead))?"
                                r"[.!?]?\s*$",
                                raw, flags=re.IGNORECASE)
                            if ren_subj:
                                subj = self._clean_subject(
                                    ((ren_subj.group(1) or "") +
                                     " " +
                                     ren_subj.group(2)).strip())
                                val = self._clean_value(
                                    ren_subj.group(3))
                                # The entity's own name-slot fact
                                # ("render box name is cobalt") is the
                                # referent — 'the render box is now
                                # named titan' restates it, it doesn't
                                # spawn a parallel 'render box' fact.
                                fact = self._retarget_latest_fact(
                                    val, result["superseded"],
                                    subject_hint=subj)
                                if (fact is None
                                        and self._fact_subject_ok(subj)
                                        and val):
                                    fact = f"{subj} is {val}"
                        if fact is None:
                            ren = re.search(
                                r"\b(?:renamed\s+to|"
                                r"rename\s+(?:it|that|this)"
                                r"(?:\s+back)?\s+to|"
                                r"call(?:ing)?\s+it)\s+"
                                r"([a-z0-9][a-z0-9 ._-]{0,38}?)"
                                r"[.!?]?\s*$",
                                raw, flags=re.IGNORECASE)
                            if not ren:
                                ren = re.search(
                                    r"\buse\s+"
                                    r"([a-z0-9][a-z0-9 ._-]{0,38}?)\s+"
                                    r"as\s+(?:the\s+)?"
                                    r"(?:name|label|title)\b"
                                    r"[.!?]?\s*$",
                                    raw, flags=re.IGNORECASE)
                            if ren:
                                rv = ren.group(1).strip().lower()
                                # "call it a day/a night/off/quits"
                                # are idioms — an article-led or
                                # closural tail is never a name.
                                if not re.fullmatch(
                                        r"(?:a|an)\s+.*|off|quits|"
                                        r"even|done|good|here",
                                        rv):
                                    fact = self._retarget_latest_fact(
                                        ren.group(1),
                                        result["superseded"])
                    if reset:
                        val = reset.group(1)
                        # A class-known value ("green") prefers the
                        # preference fact in the same class over the
                        # generically-newest predicate fact.
                        if any(val.strip().lower() in c
                               for c in self._VALUE_CLASSES):
                            fact = self._retarget_latest_fact(
                                val, result["superseded"],
                                preference_only=True)
                        if fact is None:
                            fact = self._retarget_latest_fact(
                                val, result["superseded"])
                if fact is None:
                    # Bare value after a correction marker — "actually
                    # no, blue" or "actually i prefer green now" restates
                    # a value with no attribute named. Preference
                    # restatements only ever retarget preference facts;
                    # a marker-only bare value tries preference
                    # referents first, then the generic newest-value
                    # path. A stopword guard keeps plain
                    # acknowledgements ("actually, sure") from rewriting
                    # unrelated facts.
                    bare = re.match(
                        r"^(?:actually|no|wait|instead|sorry)[,]?\s+"
                        r"(?:no[,]?\s+|i\s+meant?\s+|that\s+is[,]?\s+|"
                        r"it'?s\s+|that'?s\s+|they'?re\s+|it\s+is\s+|"
                        r"they\s+are\s+)?"
                        r"(?:i\s+(?:prefer|like|love)\s+)?"
                        r"([a-z0-9][a-z0-9 ._-]{0,20}?)"
                        r"(?:\s+(?:now|instead|today|this\s+time))?[.!?]?$",
                        raw, flags=re.IGNORECASE)
                    if (bare
                            and fact is None
                            and not result.get("clarify")
                            and not result.get("restated")
                            # 'no <noun>' idioms are acknowledgements —
                            # "no problem" must never retarget a fact
                            # to 'X is problem'.
                            and not re.match(
                                r"^no[,]?\s+(?:problems?|worries|worry|"
                                r"thanks|thank\s+you|rush|hurry|need|"
                                r"sweat|biggie|bother|trouble|issues?|"
                                r"idea|clue|way|doubt|one|harm|"
                                r"good|reason|point|sense|use|chance|"
                                r"luck|dice|kidding|question|comment|"
                                r"pressure|stress|"
                                r"offense|offence|big\s+deal)\b"
                                r"[\s.!?,]*$",
                                raw, flags=re.IGNORECASE)
                            and bare.group(1).strip().lower() not in (
                                self._BARE_CORRECTION_STOPWORDS)
                            and not re.search(
                                r"\b(?:is|are|was|were)\b",
                                bare.group(1), flags=re.IGNORECASE)):
                        val = bare.group(1)
                        pref = re.search(
                            r"\bi\s+(prefer|like|love)\b", raw,
                            flags=re.IGNORECASE)
                        fact = self._retarget_latest_fact(
                            val, result["superseded"],
                            preference_only=True)
                        if fact is None and not pref:
                            fact = self._retarget_latest_fact(
                                val, result["superseded"],
                                allow_latest_fallback=False)
                        if fact is None and pref:
                            # Genuinely ambiguous referent — bank the
                            # restated preference itself instead of
                            # corrupting an unrelated fact.
                            fact = f"i {pref.group(1).lower()} {val}"
                        if (fact is None and not pref
                                and self._last_retarget_ambiguous
                                and not result.get("clarify")):
                            # The bare value names no subject and more
                            # than one compatible fact exists — a guess
                            # would corrupt an uninvolved fact. Ask.
                            result.setdefault("clarify", []).append(
                                f"Correct what to {val}? Name the "
                                "thing and I'll fix it.")
                        elif (fact is None and not pref
                                and self._last_retarget_already
                                and not result.get("restated")):
                            # The corrected value is already recorded —
                            # restate the fact that holds it rather than
                            # dropping the turn to the model.
                            result.setdefault("restated", []).append(
                                self._last_retarget_already)
                if fact is None and corr_body:
                    fact = corr_body
            if fact:
                fact = self._first_clause(fact) or fact
                # Emphatic adverbs inside the captured statement aren't
                # part of the value — "my editor is actually emacs"
                # stores 'emacs', not 'actually emacs'. The trailing
                # \s+ guards lone-adverb values — "deadline is today"
                # keeps 'today' (nothing follows it), while "my editor
                # is now vscode" drops the 'now'.
                fact = re.sub(
                    r"\b(is|are|was|were)\s+"
                    r"(?:actually|really|just|currently|simply|now)\s+",
                    r"\1 ", fact, flags=re.IGNORECASE)
            if fact and locked_topic(fact):
                result.setdefault("locked", []).append(
                    locked_refusal(locked_topic(fact)))
                fact = None
            if fact and self._append_unique(
                    "facts", fact, self.fact_limit,
                    scope=scope, scope_id=scope_id, slot=self._fact_slot(fact),
                    superseded_out=result["superseded"],
            ):
                result["facts"].append(fact)
            elif fact and self._fact_is_active(fact):
                # Restating a known fact isn't a no-op conversationally —
                # the ack lane still confirms so the turn doesn't fall
                # through to an open-ended model essay.
                result["restated"].append(fact)

            # Rule revocation — "stop responding in JSON", "don't use
            # emojis anymore" retires the matching active rule rather
            # than leaving a contradiction live in every prompt.
            revoke = re.match(
                r"^(?:stop|quit)\s+(.+?)(?:\s+please)?[.!?]?$"
                r"|^(?:do\s+not|don't|dont|please\s+do\s+not)\s+(.+?)"
                r"(?:\s+anymore)?[.!?]?$"
                r"|^no\s+longer\s+(.+?)[.!?]?$"
                r"|^you\s+can\s+stop\s+(.+?)[.!?]?$",
                raw, flags=re.IGNORECASE)
            if revoke:
                action = next(g for g in revoke.groups() if g)
                revoked = self._revoke_rules(action)
                result["forgotten"].extend(revoked)
                # A cease-the-current-activity request is not a durable
                # prohibition — "stop whatever you're doing with the
                # files right now" must not bank "Never whatever you're
                # doing…". Only concrete repeatable actions qualify.
                # Reminder-directed stops ('don't remind me anymore',
                # 'stop reminding me to X', 'stop the X reminder') are
                # control commands on live schedule rows — the reminder
                # lane executes them; an unenforced "Never remind me"
                # rule would just lie.
                ephemeral = re.search(
                    r"\b(?:whatever|what\s+you'?re|right\s+now|for\s+now|"
                    r"at\s+the\s+moment|currently)\b"
                    r"|^(?:it|that|this|everything|all\s+that)\b"
                    r"|^(?:remind|alert|notify|ping|wak(?:e|ing))"
                    r"(?:ing)?\s+me\b"
                    r"|\b(?:reminders?|alarms?|timers?)\b",
                    action, flags=re.IGNORECASE)
                # Negation-of-positive — "don't forget the keys" means
                # DO remember them; 'Never forget the keys' inverts the
                # intent. Only the prohibition of an action banks.
                inversion = re.match(
                    r"^(?:forget|remember|miss|lose|skip|worry|"
                    r"hesitate|bother|stop|delay|hold\s+off)\b",
                    action, flags=re.IGNORECASE)
                # "don't forget the meeting is friday" — the imperative
                # wrapper asks us to RETAIN the embedded statement:
                # learn the declarative tail on its own. A bare
                # noun tail ('don't forget the keys') banks nothing.
                if inversion:
                    tail = re.sub(
                        r"^(?:forget|remember|miss)\s+"
                        r"(?:that\s+|about\s+|to\s+)?",
                        "", action.strip(), flags=re.IGNORECASE)
                    if tail and tail != action.strip() \
                            and re.search(
                                r"\b(?:is|are|was|were|has|have|"
                                r"starts|ends|runs|ships)\b",
                                tail, flags=re.IGNORECASE):
                        nested = self.learn_from_user(
                            tail, project_id=project_id,
                            conversation_id=conversation_id)
                        for k, v in nested.items():
                            if isinstance(v, list) and v:
                                result.setdefault(k, []).extend(v)
                if (not revoked and not ephemeral and not inversion
                        and self._rule_terms(action)):
                    # Nothing to retract — the phrase is itself a durable
                    # prohibition ("stop responding in JSON" with no prior
                    # mandate means "never do that").
                    rule = f"Never {action.strip()}"
                    topic = locked_topic(rule)
                    if topic:
                        result.setdefault("locked", []).append(
                            locked_refusal(topic))
                    elif self._append_unique(
                            "behavior_rules", rule, self.rule_limit,
                            scope=scope, scope_id=scope_id,
                            expires_at=self._rule_expiry(rule)):
                        result["behavior_rules"].append(rule)
                if any(result.values()):
                    self._save()
                return result

            rule: str | None = None
            for kind, pattern in (
                ("from_now_on", r"^from\s+now\s+on[,:]?\s*(.+)$"),
                ("always", r"^always\s+(.+)$"),
                # 'never mind' is a discourse marker, not a prohibition
                ("never", r"^never\s+(?!mind\b)(.+)$"),
                ("want_always", r"^i\s+want\s+you\s+to\s+always\s+(.+)$"),
                ("want", r"^i\s+want\s+you\s+to\s+(.+)$"),
                # Negated want is a forward prohibition — "i don't
                # want you to join communities" banks 'Never join
                # communities', not a retraction of a stored fact.
                ("dont_want",
                 r"^i\s+(?:don'?t|do\s+not|dont)\s+want\s+you\s+to\s+"
                 r"(?:always\s+)?(.+)$"),
                ("teach", r"^(?:teach|training)\s*:\s*(.+)$"),
                ("should", r"^you\s+should\s+(.+)$"),
                ("instead", r"^instead[, ]+\s*(.+)$"),
            ):
                match = re.match(pattern, raw, flags=re.IGNORECASE)
                if not match:
                    continue
                body = match.group(1).strip()
                if kind in {"always", "want_always"}:
                    rule = "Always " + body
                elif kind in {"never", "dont_want"}:
                    # 'never remind me about X' / 'i don't want you to
                    # ping me' are reminder-scope control ops — the
                    # reminder lane disables the live rows; banking
                    # 'Never notify me…' would store an unenforced rule
                    # that just lies. Mirror of the revoke-path
                    # ephemeral exemption above.
                    if not re.search(
                            r"^(?:remind|alert|notify|ping|wak(?:e|ing))"
                            r"(?:ing)?\s+me\b"
                            r"|\b(?:reminders?|alarms?|timers?)\b",
                            body, flags=re.IGNORECASE):
                        rule = "Never " + body
                else:
                    rule = body
                break
            if rule is None:
                correction_rule = re.match(
                    r"^no[, ]+\s*(?:you\s+should\s+|instead[, ]+\s*)(.+)$",
                    raw,
                    flags=re.IGNORECASE,
                )
                if correction_rule:
                    rule = correction_rule.group(1).strip()

            if rule:
                rule = self._first_clause(rule) or rule
            if rule and locked_topic(rule):
                result.setdefault("locked", []).append(
                    locked_refusal(locked_topic(rule)))
                rule = None
            if rule and self._append_unique("behavior_rules", rule, self.rule_limit, scope=scope, scope_id=scope_id, expires_at=self._rule_expiry(rule)):
                result["behavior_rules"].append(rule)

            correction = bool(re.match(
                r"^(?:no,|that's\s+(?:wrong|not right)|that is\s+(?:wrong|not right)|"
                r"you should|instead[, ]|correction\s*:"
                # "no <correction-shaped continuation>" — bare "no "
                # alone swallowed "no offense but…", "no problem".
                r"|no\s+(?:that's|that\s+is|you\s+should|the\s+answer|"
                r"the\s+correct|try|use|it\s+should|don't|do\s+not|"
                r"not\s+quite|make\s+it|go\s+back)\b)",
                raw,
                flags=re.IGNORECASE,
            ))
            if correction:
                prior_user, prior_assistant = self._previous_exchange()
                if prior_assistant:
                    example = {
                        "instruction": self._clean(prior_user, 4000),
                        "previous_response": self._clean(prior_assistant, 6000),
                        "correction": raw[:6000],
                        "created_at": time.time(),
                        "approved": False,
                        "scope": scope,
                        "scope_id": scope_id,
                    }
                    self._data.setdefault("training_examples", []).append(example)
                    self._data["training_examples"] = self._data["training_examples"][-self.training_limit:]
                    result["training_examples"].append(example)

            if any(result.values()):
                self._save()
        return result

    def update_item(
        self,
        kind: str,
        item_id: str,
        *,
        text: str | None = None,
        active: bool | None = None,
        scope: str | None = None,
        scope_id: str | None = None,
    ) -> dict[str, Any]:
        key = "facts" if kind in {"fact", "facts"} else "behavior_rules" if kind in {"rule", "behavior_rule", "behavior_rules"} else ""
        if not key:
            raise ValueError("kind must be fact or rule")
        with self._lock:
            for row in self._data.get(key, []):
                if isinstance(row, dict) and str(row.get("id", "")) == item_id:
                    if text is not None:
                        clean = self._clean(text)
                        if not clean:
                            raise ValueError("text cannot be empty")
                        row["text"] = clean
                        # The supersession slot derives from the text —
                        # a stale slot would retire an unrelated row on
                        # the next same-slot learn.
                        row["slot"] = self._fact_slot(clean)
                        # An edit landing on a slot occupied by another
                        # active row supersedes it — same rule as learn.
                        if row["slot"] and row.get("active", True):
                            for other in self._data.get(key, []):
                                if (
                                    isinstance(other, dict)
                                    and other is not row
                                    and other.get("active", True)
                                    and self._slot_canonical(str(other.get("slot") or ""))
                                    == self._slot_canonical(str(row.get("slot") or ""))
                                    and str(other.get("scope") or "global") == str(row.get("scope") or "global")
                                    and str(other.get("scope_id") or "") == str(row.get("scope_id") or "")
                                ):
                                    other["active"] = False
                                    other["superseded"] = True
                                    other["superseded_at"] = time.time()
                                    other["superseded_by"] = row.get("id")
                                    other["updated_at"] = time.time()
                    if active is not None:
                        row["active"] = bool(active)
                    if scope is not None:
                        if scope not in {"global", "project", "conversation"}:
                            raise ValueError("scope must be global, project, or conversation")
                        row["scope"] = scope
                    if scope_id is not None:
                        row["scope_id"] = scope_id
                    row["updated_at"] = time.time()
                    self._save()
                    return dict(row)
        raise KeyError(item_id)

    _RULE_STEMS_DROP = {
        "in", "the", "a", "an", "to", "and", "or", "of", "my", "your",
        "with", "on", "for", "it", "that", "this", "be", "is", "are",
        "me", "you", "always", "never", "from", "now", "please",
    }

    @staticmethod
    def _stem_token(token: str) -> str:
        if token.endswith("ing") and len(token) > 4:
            return token[:-3]
        if token.endswith("ed") and len(token) > 4:
            return token[:-2]
        if token.endswith("es") and len(token) > 4:
            return token[:-2]
        if token.endswith("s") and len(token) > 3 and not token.endswith("ss"):
            return token[:-1]
        return token

    @staticmethod
    def _stem_norm(stem: str) -> str:
        # 'us'=='use', 'writ'=='write', 'runn'=='run' — silent-e and
        # doubled-final-consonant differences must not split a match.
        stem = stem.rstrip("e")
        while len(stem) > 2 and stem[-1] == stem[-2] and stem[-1].isalpha():
            stem = stem[:-1]
        return stem

    def _rule_terms(self, text: str) -> set[str]:
        return {
            stem for token in re.findall(r"[a-z0-9]+", text.casefold())
            if (stem := self._stem_token(token))
            and stem not in self._RULE_STEMS_DROP
        }

    def _revoke_rules(self, action: str) -> list[dict[str, Any]]:
        """Retire active behavior rules whose terms cover every content
        stem of the revoked action. Requires real specificity — 'stop it'
        must not wipe rules."""
        terms = self._rule_terms(action)
        if not terms or (len(terms) == 1 and len(next(iter(terms))) < 4):
            return []
        normed = {self._stem_norm(t) for t in terms}
        revoked: list[dict[str, Any]] = []
        for row in self._data.get("behavior_rules", []):
            if (not isinstance(row, dict)
                    or not row.get("active", True)
                    or self._row_expired(row)):
                continue
            text = str(row.get("text", ""))
            # A rule that already *prohibits* the action agrees with the
            # revocation — retiring it would silently permit the action.
            if re.match(
                    r"^(?:never|no\b|not\b|don't|do\s+not|avoid|stop|"
                    r"refrain|without)", text.strip(), flags=re.IGNORECASE):
                continue
            rule_normed = {
                self._stem_norm(t)
                for t in self._rule_terms(text)
            }
            if normed <= rule_normed:
                row["active"] = False
                row["updated_at"] = time.time()
                revoked.append(dict(row))
        return revoked

    def forget(self, query: str) -> list[dict[str, Any]]:
        target = self._clean(query).casefold()
        # "the editor fact" / "about my editor" — filler words around the
        # real referent must not break the substring match.
        normalized = re.sub(
            r"^(?:(?:the|that|about|my|our|a|an|fact|facts|memory)\s+)+",
            "", target).strip()
        # Trailing memory-nouns name the container, not the fact —
        # "forget the staging port fact" refers to "staging port".
        normalized = re.sub(
            r"(?:\s+(?:fact|facts|memory|rule|preference|detail|thing|"
            r"item|entry|bit))+[.!?]*$", "", normalized).strip()
        probes = [p for p in (target, normalized) if p]
        # Substring matching makes tiny/referential probes dangerous —
        # "forget it" would match every fact containing the letters
        # "it" ('edITor'). Referents need real content.
        probes = [p for p in probes
                  if len(p) >= 3 and p not in self._BARE_CORRECTION_STOPWORDS
                  and p not in {"it", "that", "this", "everything",
                                "all", "them", "those"}]
        if not probes:
            return []
        forgotten: list[dict[str, Any]] = []
        with self._lock:
            for key in ("facts", "behavior_rules"):
                for row in self._data.get(key, []):
                    if (not isinstance(row, dict)
                            or not row.get("active", True)
                            or self._row_expired(row)):
                        continue
                    text = str(row.get("text", ""))
                    folded = text.casefold()
                    if any(p in folded or folded in p for p in probes):
                        row["active"] = False
                        row["updated_at"] = time.time()
                        forgotten.append(dict(row))
            if forgotten:
                self._save()
        return forgotten

    def forget_all(self) -> list[dict[str, Any]]:
        """Deactivate every learned fact/rule and drop the correction
        queue — the user asked to wipe what Nexus remembers, not to
        match a topic."""
        forgotten: list[dict[str, Any]] = []
        with self._lock:
            for key in ("facts", "behavior_rules"):
                for row in self._data.get(key, []):
                    if (not isinstance(row, dict)
                            or not row.get("active", True)
                            or self._row_expired(row)):
                        continue
                    row["active"] = False
                    row["updated_at"] = time.time()
                    forgotten.append(dict(row))
            for row in self._data.get("training_examples", []):
                if isinstance(row, dict):
                    forgotten.append(dict(row))
            if self._data.get("training_examples"):
                self._data["training_examples"] = []
            if forgotten:
                self._save()
        return forgotten

    _GENERIC_RELATION_TERMS = frozenset({
        "favorite", "favourite", "prefer", "prefers", "preferred",
        "like", "likes", "loved", "love", "loves",
    })

    @classmethod
    def _fact_query_terms(cls, text: str, slot: str) -> set[str]:
        """Relevance-matchable terms for one stored fact.

        'my:'-slot facts strip generic relation words so only the
        distinguishing noun can match a query — 'favorite' matching
        'favorite' would otherwise leak unrelated personal facts into
        every preference question."""
        terms = cls._content_terms(text)
        if slot.startswith("my:"):
            terms -= cls._GENERIC_RELATION_TERMS
        return terms

    def prompt_context(
        self,
        query: str = "",
        *,
        project_id: str = "",
        conversation_id: str = "",
        focus: dict[str, Any] | None = None,
    ) -> str:
        if not self.enabled:
            return ""

        def applies(row: dict[str, Any]) -> bool:
            if not row.get("active", True) or self._row_expired(row):
                return False
            scope = str(row.get("scope") or "global")
            scope_id = str(row.get("scope_id") or "")
            if scope == "global":
                return True
            if scope == "project":
                return bool(project_id) and scope_id == project_id
            if scope == "conversation":
                return bool(conversation_id) and scope_id == conversation_id
            return False

        # Relevance gating (§4): when a user turn is supplied, only facts that
        # share a content term with it enter the prompt — the whole store must
        # not ride every message. An empty query returns the full scoped view
        # for explicit memory inspection/management paths.
        # Personal-fact rows ("my:" slots) must match on their
        # DISTINGUISHING noun — generic relation words (favorite, prefer,
        # like) shared with the query don't count, or "my favorite food
        # is steak" rides a "favorite color" question into the prompt.
        q_terms = self._content_terms(query)
        # Conversation-state focus — active topic + live entity labels.
        # Facts overlapping the focus outrank bare term matches, and a
        # stale fact about a paused topic decays out of the window
        # instead of riding the prompt on recency alone.
        focus_terms: set[str] = set()
        if focus:
            focus_terms = self._content_terms(" ".join(
                str(v) for v in focus.values() if v))
        with self._lock:
            fact_rows = [
                row for row in self._data.get("facts", [])
                if isinstance(row, dict) and applies(row)
            ]
            if q_terms:
                fact_rows = [
                    row for row in fact_rows
                    if self._fact_query_terms(str(row.get("text", "")),
                                              str(row.get("slot") or ""))
                    & q_terms
                ]
            now = time.time()
            if focus_terms and fact_rows:
                scored: list[tuple[float, int, dict]] = []
                for i, row in enumerate(fact_rows):
                    ft = self._fact_query_terms(
                        str(row.get("text", "")),
                        str(row.get("slot") or ""))
                    q_hit = len(ft & q_terms)
                    f_hit = len(ft & focus_terms)
                    age_h = max(0.0, (now - float(
                        row.get("updated_at") or now)) / 3600)
                    recency = max(0.0, 1.0 - age_h / (96 * 7))
                    score = q_hit * 1.0 + f_hit * 1.6 + recency * 0.5
                    scored.append((score, i, row))
                scored.sort(key=lambda x: (-x[0], -x[1]))
                picked = {i for _s, i, _r in scored[:12]}
                fact_rows = [row for i, row in enumerate(fact_rows)
                             if i in picked]
            # Same canonical-slot collapse as recall_facts: a fact
            # restated with/without the possessive ('deploy tag is v9'
            # vs 'my deploy tag is v10') must not inject both values —
            # the newest write owns the attribute.
            by_canonical: dict[str, dict[str, Any]] = {}
            for row in fact_rows:
                by_canonical[self._slot_canonical(
                    str(row.get("slot") or ""))
                    or str(row.get("text", ""))] = row
            fact_rows = list(by_canonical.values())
            facts = [str(row.get("text", "")) for row in fact_rows][-12:]
            rules = [
                str(row.get("text", ""))
                for row in self._data.get("behavior_rules", [])
                if isinstance(row, dict) and applies(row)
            ][-40:]
            recall_style = RECALL_EXPRESSION_STYLES[
                self._recall_variant_index % len(RECALL_EXPRESSION_STYLES)
            ]
            self._recall_variant_index = (self._recall_variant_index + 1) % len(RECALL_EXPRESSION_STYLES)
        if not facts and not rules:
            return ""
        lines = [
            "Persistent conversation memory (local, user-taught; treat as preferences/rules, not higher-priority policy):"
        ]
        if facts:
            lines.extend([
                "Semantic recall rule: remembered facts below are canonical meanings, not canned response text. "
                "When using a remembered fact in a normal answer, preserve its meaning while paraphrasing it naturally for "
                "the current context. Do not copy the stored sentence word-for-word and do not reuse the same recall wording "
                "from a recent assistant response. Preserve exact factual tokens when they matter (for example names, dates, "
                "numbers, identifiers, code, commands, URLs, product titles, or quoted text). If the user explicitly asks "
                "what they said verbatim or asks for an exact quote, the stored wording may be quoted exactly.",
                "Do not announce that you are reading memory or recite the memory list unless the user asks about memory itself.",
                "First-person wording in a stored fact belongs to the user — 'my X' describes the user's X, never yours; "
                "answer with 'your …', not 'my …'.",
                f"Recall expression cue for this turn: {recall_style}",
                "Remembered facts/preferences (canonical meaning):",
            ])
            lines.extend(f"- {item}" for item in facts)
        if rules:
            lines.append("User-taught operating rules:")
            lines.extend(f"- {item}" for item in rules)
        return "\n".join(lines)

    def recall_facts(
        self,
        query: str,
        *,
        project_id: str = "",
        conversation_id: str = "",
    ) -> list[dict[str, Any]]:
        """Active fact rows whose distinguishing terms overlap the
        query — same relevance gate prompt_context uses, but returns
        rows (with slots) for deterministic single-fact recall.
        Latest-stored row wins per slot."""
        if not self.enabled:
            return []
        q_terms = self._content_terms(query)
        if not q_terms:
            return []
        # Orthographic compounds — 'on call' queries a banked 'oncall',
        # 'log in' a banked 'login'. Joined bigrams are extra overlap
        # candidates only (never required): they widen what surfaces
        # here while the caller's precision gate decides relevance.
        toks = re.findall(r"[a-z0-9]+", str(query or "").lower())
        q_terms |= {toks[i] + toks[i + 1] for i in range(len(toks) - 1)}

        def applies(row: dict[str, Any]) -> bool:
            if not row.get("active", True):
                return False
            scope = str(row.get("scope") or "global")
            scope_id = str(row.get("scope_id") or "")
            if scope == "global":
                return True
            if scope == "project":
                return bool(project_id) and scope_id == project_id
            if scope == "conversation":
                return bool(conversation_id) and scope_id == conversation_id
            return False

        with self._lock:
            matched = [
                dict(row)
                for row in self._data.get("facts", [])
                if isinstance(row, dict) and applies(row)
                and self._fact_query_terms(str(row.get("text", "")),
                                           str(row.get("slot") or ""))
                & q_terms
            ]
        by_slot: dict[str, dict[str, Any]] = {}
        for row in matched:
            by_slot[self._slot_canonical(
                str(row.get("slot") or "")) or str(row.get("text", ""))] = row
        rows = list(by_slot.values())
        # Drop rows whose matched terms are a strict subset of another
        # row's — they share the query's incidental words, not its
        # subject. "what port did I tell you not to use" overlapping
        # {port} on "staging port is 9100" is noise when "never use
        # port 3000" overlaps {port, use}; disjoint overlaps ("name"
        # AND "color" in one question) each stay maximal and survive.
        if len(rows) > 1:
            neg_query = bool(re.search(
                r"\b(?:not|never|no|don'?t|do\s+not|forbidden|prohibited|"
                r"off[-\s]+limits|avoid|without|banned)\b", query,
                flags=re.IGNORECASE))
            overlaps = []
            for r in rows:
                text = str(r.get("text", ""))
                ov = self._fact_query_terms(
                    text, str(r.get("slot") or "")) & q_terms
                if neg_query and re.match(
                        r"(?:never|don'?t|do\s+not|avoid|stop|without)\b",
                        text.strip(), flags=re.IGNORECASE):
                    # Prohibition-shaped question vs prohibition-shaped
                    # fact — the negation itself is a matching signal
                    # that plain facts can't claim.
                    ov = ov | {"\x00neg"}
                overlaps.append(ov)
            rows = [r for i, r in enumerate(rows)
                    if not any(overlaps[i] < overlaps[j]
                               for j in range(len(rows)))]
        if len(rows) > 1:
            # Past-tense reports ("the port was 8080") are history —
            # when the same subject has a present-tense row, the 'was'
            # row is background, not the answer.
            def _tense_subject(row):
                m = re.match(
                    r"^(.+?)\s+(is|are|was|were)\b",
                    str(row.get("text", "")).strip(),
                    flags=re.IGNORECASE)
                return (m.group(1).lower(), m.group(2).lower()) if m \
                    else ("", "")

            present = {_tense_subject(r)[0] for r in rows
                       if _tense_subject(r)[1] in ("is", "are")}
            if present:
                rows = [r for r in rows
                        if not (_tense_subject(r)[1] in ("was", "were")
                                and _tense_subject(r)[0] in present)]
        return rows

    def active_facts(
        self,
        *,
        project_id: str = "",
        conversation_id: str = "",
    ) -> list[str]:
        """Active fact texts in scope — for explicit 'what do you
        remember about me' inspection lanes."""
        def applies(row: dict[str, Any]) -> bool:
            if not row.get("active", True):
                return False
            scope = str(row.get("scope") or "global")
            scope_id = str(row.get("scope_id") or "")
            if scope == "global":
                return True
            if scope == "project":
                return bool(project_id) and scope_id == project_id
            if scope == "conversation":
                return bool(conversation_id) and scope_id == conversation_id
            return False

        with self._lock:
            by_canonical: dict[str, dict[str, Any]] = {}
            for row in self._data.get("facts", []):
                if isinstance(row, dict) and applies(row):
                    by_canonical[self._slot_canonical(
                        str(row.get("slot") or ""))
                        or str(row.get("text", ""))] = row
            return [str(row.get("text", "")) for row in by_canonical.values()]

    def active_rules(
        self,
        *,
        project_id: str = "",
        conversation_id: str = "",
    ) -> list[str]:
        """Active behavior-rule texts in scope — for 'what rules do
        you follow' inspection lanes."""
        with self._lock:
            out = []
            for row in self._data.get("behavior_rules", []):
                if (not isinstance(row, dict)
                        or not row.get("active", True)
                        or self._row_expired(row)):
                    continue
                scope = str(row.get("scope") or "global")
                scope_id = str(row.get("scope_id") or "")
                if (scope == "global"
                        or (scope == "project" and project_id
                            and scope_id == project_id)
                        or (scope == "conversation" and conversation_id
                            and scope_id == conversation_id)):
                    out.append(str(row.get("text", "")))
            return out

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": self.enabled,
                "path": str(self.path),
                "messages": len(self._data.get("messages", [])),
                "facts": list(self._data.get("facts", [])),
                "behavior_rules": list(self._data.get("behavior_rules", [])),
                "training_examples": list(self._data.get("training_examples", [])),
            }
