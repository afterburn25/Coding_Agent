"""Silent misspelling normalization for user turns (backlog §1).

The model and the intent classifier see corrected text; the task ledger
keeps the user's original words. Corrections are deliberately
conservative:

- Protected spans are never touched: code spans, quoted strings, URLs,
  emails, paths, slash commands, dotted identifiers, tokens containing
  digits/underscores/hyphens, camelCase names, and capitalized
  mid-sentence words (proper nouns).
- A token that is already a known word is left alone unless a
  context-gated confusion rule fires (``witch model`` → ``which``).
- Unknown tokens resolve by Damerau-Levenshtein distance plus a
  consonant-skeleton phonetic match. Context vocabulary (recent
  entities, project names) gets a strong boost — that is how
  "desrt" resolves to "dessert" in a cooking thread and "desert" in a
  travel thread.
- Two candidates within the acceptance margin means genuinely
  ambiguous → the token is left for the model (or a clarification) to
  resolve. Nothing is ever silently guessed between equals.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

from .words_en import (
    COMMON_WORDS, CONTRACTIONS, DOMAIN_WORDS, HARVESTED_WORDS,
)

_WORD_RANK: dict[str, int] = {}
for _i, _w in enumerate(COMMON_WORDS):
    _WORD_RANK.setdefault(_w, _i)  # first occurrence wins — dupes exist
_VOCAB: frozenset[str] | None = None
_KNOWN: frozenset[str] | None = None
_WORD_SK: dict[str, str] | None = None


def _vocab() -> frozenset[str]:
    global _VOCAB
    if _VOCAB is None:
        _VOCAB = set(COMMON_WORDS) | DOMAIN_WORDS | CONTRACTIONS | HARVESTED_WORDS
    return _VOCAB


def _known() -> frozenset[str]:
    """Protective lexicon — real words that must never be "corrected".

    ``words_en_scowl.txt`` is a SCOWL-60-derived dictionary (~94k
    entries incl. proper names). It is protection-only: membership
    shields a token but never makes it a correction *candidate*, so
    obscure dictionary entries can't become typo magnets. This layer
    exists because the curated vocab can't enumerate English — a real
    word absent from it ('dinosaur', 'threw', 'spilled') would
    otherwise be mangled to a nearby candidate ('dancer', 'throw',
    'spelled').
    """
    global _KNOWN
    if _KNOWN is None:
        from pathlib import Path
        path = Path(__file__).with_name("words_en_scowl.txt")
        try:
            data = path.read_text(encoding="utf-8")
        except OSError:
            data = ""
        _KNOWN = frozenset(data.split()) | _vocab()
    return _KNOWN


def _skeleton(word: str) -> str:
    """Consonant skeleton — a compact phonetic key.

    Strips interior vowels, collapses doubled letters, and normalizes
    the digraphs/soft letters that produce most phonetic misspellings
    (ph→f, soft c→s, ch→x, ck→k, qu→k, silent starters).
    "fettuccine" and "feticcinii" both reduce to ``ftksn``.
    """
    w = re.sub(r"[^a-z]", "", word.lower())
    for pre, rep in (("kn", "n"), ("gn", "n"), ("pn", "n"),
                     ("ps", "s"), ("wr", "r"), ("wh", "w"),
                     ("ae", "e"), ("ph", "f")):
        if w.startswith(pre):
            w = rep + w[2:]
            break
    out: list[str] = []
    i = 0
    while i < len(w):
        ch = w[i]
        nxt = w[i + 1] if i + 1 < len(w) else ""
        if ch in "aeiouy":
            if not out:
                out.append("a")
            i += 1
            continue
        if ch == "c":
            if nxt == "h":
                # ch → soft "x" before e/i/y (church), hard "k" elsewhere
                # (school, psychology).
                after = w[i + 2] if i + 2 < len(w) else ""
                out.append("x" if after in "eiy" else "k")
                i += 2
                continue
            if nxt == "k":
                out.append("k")
                i += 2
                continue
            out.append("s" if nxt in "eiy" else "k")
        elif ch == "p" and nxt == "h":
            out.append("f")
            i += 1
        elif ch == "q":
            out.append("k")
        elif ch == "x":
            out.append("ks")
        elif ch == "g" and nxt == "h":
            i += 1          # silent gh
        elif ch == "g" and nxt in "eiy":
            out.append("j")
        elif ch == "t" and nxt == "i" and w[i + 2:i + 3] in "aon":
            out.append("x")  # -tion/-tien → "shun"
        elif ch == "z":
            out.append("s")
        else:
            out.append(ch)
        i += 1
    collapsed: list[str] = []
    for c in out:
        if not collapsed or collapsed[-1] != c:
            collapsed.append(c)
    return "".join(collapsed)


def _word_skeletons() -> dict[str, str]:
    global _WORD_SK
    if _WORD_SK is None:
        _WORD_SK = {w: _skeleton(w) for w in _vocab()}
    return _WORD_SK


def _dl(a: str, b: str, limit: int = 4) -> int:
    """Damerau-Levenshtein with early cutoff at `limit` (returns limit+1
    when exceeded)."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev2: list[int] | None = None
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        row_min = i
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur[j] = min(cur[j - 1] + 1, prev[j] + 1, prev[j - 1] + cost)
            if (prev2 is not None and i > 1 and j > 1
                    and ca == b[j - 2] and a[i - 2] == cb):
                cur[j] = min(cur[j], prev2[j - 2] + 1)
            if cur[j] < row_min:
                row_min = cur[j]
        if row_min > limit:
            return limit + 1
        prev2, prev = prev, cur
    return prev[len(b)]


# ---------------------------------------------------------------------------
# Protected spans — never normalize inside these.
# ---------------------------------------------------------------------------

_PROTECT_RES = (
    re.compile(r"```.*?```", re.S),
    re.compile(r"`[^`\n]*`"),
    re.compile(r'"[^"\n]{1,500}"'),
    re.compile(r"(?<![\w'])'[^'\n]{2,200}'(?![\w])"),
    re.compile(r"https?://\S+|www\.\S+|[\w.+-]+@[\w-]+\.[\w.-]+"),
    re.compile(r"(?:[A-Za-z]:)?[\\/][\w.\\/:-]+"),
    re.compile(r"[\w.-]+[\\/][\w.\\/:-]+"),
    re.compile(r"(?<![\w/])/(?:[^\s/][^\s]*)?"),  # slash command / abs path
    re.compile(r"\b[\w-]+\.[\w-]+(?:\.[\w-]+)*\b"),  # dotted ids, file.ext
)

_TOKEN_RE = re.compile(r"[A-Za-z]+(?:['’][a-zA-Z]+)?")

# Sentence-initial boundary for the proper-noun guard.
_SENT_BOUND_RE = re.compile(r"[.!?]\s*$")


def _protected_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for pat in _PROTECT_RES:
        spans.extend(m.span() for m in pat.finditer(text))
    return spans


def _in_spans(pos: int, spans: list[tuple[int, int]]) -> bool:
    return any(s <= pos < e for s, e in spans)


# ---------------------------------------------------------------------------
# Context-gated rules — real words in the wrong slot.
# ---------------------------------------------------------------------------

_DOSE_VERB = (
    "use|work|run|do|mean|need|take|cost|support|have|handle|load|start|"
    "stop|make|look|say|go|get|fit|help|matter|affect|change|weigh|"
    "sound|seem|require|store|offer|allow|play|eat|know|come|give"
)
_CONFUSION_RULES = (
    # "how much ram dose nexus use" — dose+noun+verb = does.
    (re.compile(r"\bdose\b(?=\s+(?:(?:the|a|an|my|your|this|that)\s+)?"
                r"[a-z]+\s+(?:" + _DOSE_VERB + r")\b)"), "does"),
    (re.compile(r"\bdose\b(?=\s+(?:it|he|she|they|this|that|nexus|you)\s+"
                r"(?:" + _DOSE_VERB + r")\b)"), "does"),
    # "witch model is loaded" — which + noun/aux, never "the witch".
    (re.compile(r"\bwitch\b(?=\s+(?:is|are|was|were|do|does|did|should|"
                r"can|could|will|would|to|of|the|one|ones|model|option|"
                r"version|file|tool|image|way|setting|mode)\b)"), "which"),
    # Contraction restorations gated on a following noun/determiner —
    # "whats the weather" but never "thats" a possessive.
    (re.compile(r"\bwhats\b(?=\s+(?:the|up|your|my|this|that|it|going|"
                r"new|on|in|next|a|an|wrong|happening|wrong|good)\b)"),
     "what's"),
    (re.compile(r"\bwheres\b(?=\s+(?:the|my|your|this|that|it|a|an)\b)"),
     "where's"),
    (re.compile(r"\bwhens\b(?=\s+(?:the|my|your|this|that|it|a|an)\b)"),
     "when's"),
    (re.compile(r"\bwhos\b(?=\s+(?:the|my|your|this|that|it|a|an)\b)"),
     "who's"),
    (re.compile(r"\bhows\b(?=\s+(?:the|my|your|this|that|it|a|an|things|"
                r"everything|work|life)\b)"), "how's"),
    # "wether" is a real word (a sheep) but in chat it is almost always
    # "weather" — fire only with an article/question or weather context.
    (re.compile(r"\bwether\b(?=\s+(?:outside|today|tomorrow|tonight|"
                r"like|forecast|report|is|looks|now|here|in|tomorrow)\b)"),
     "weather"),
    (re.compile(r"(?<=\bthe\s)wether\b|(?<=\bin\s)wether\b"), "weather"),
)


# Domain co-occurrence: a cue word pulls its related vocabulary into the
# context set, so "deploy the brach" prefers "branch" over "breach".
_CONTEXT_AFFINITY: tuple[tuple[re.Pattern, frozenset], ...] = (
    (re.compile(r"\b(?:git|github|gitlab|deploy|commit|push|pull|merge|"
                r"rebase|checkout|clone|fork|pr|repo)\b", re.I),
     frozenset({"branch", "tag", "origin", "upstream", "merge", "commit",
                "deploy", "trunk", "release", "diff"})),
    (re.compile(r"\b(?:image|images|draw|paint|render|upscale|photo|"
                r"picture|portrait|generate|inpaint|outpaint)\b", re.I),
     frozenset({"image", "prompt", "checkpoint", "denoise", "upscale",
                "variation", "sampler", "seed", "steps", "portrait"})),
    (re.compile(r"\b(?:model|models|llm|llama|qwen|vram|gpu|inference|"
                r"checkpoint|quant)\b", re.I),
     frozenset({"model", "quant", "context", "llama", "gguf", "vram",
                "checkpoint", "backend"})),
    (re.compile(r"\b(?:recipe|cook|cooking|bake|ingredient|sauce|oven|"
                r"meal|dish|dinner|lunch)\b", re.I),
     frozenset({"recipe", "bake", "simmer", "sauce", "season", "dessert",
                "marinate", "skillet", "preheat", "broil"})),
    (re.compile(r"\b(?:weather|rain|snow|forecast|temperature|storm|"
                r"outside|sunny|cloudy|humid)\b", re.I),
     frozenset({"weather", "forecast", "degrees", "humidity",
                "precipitation"})),
)


def _message_context_words(text: str) -> set[str]:
    """Words the message itself makes likely — same-message cues plus
    domain affinity expansions."""
    words = {w.lower() for w in _TOKEN_RE.findall(text) if len(w) >= 4}
    for pat, extra in _CONTEXT_AFFINITY:
        if pat.search(text):
            words |= extra
    return words


# Inflection/derivation suffixes — a word absent from the lexicon that
# stems to a known word is still a word ("normally" → normal+ly).
_SUFFIXES = (
    "ation", "ition", "tion", "ness", "ment", "ally", "able", "ible",
    "ings", "ies", "ied", "ier", "iest", "ily", "ing", "ers", "est",
    "ely", "ive", "ous", "ious", "ity", "ism", "ist", "ful", "less",
    "ize", "ise", "ized", "ised", "en", "er", "or", "ed", "ly", "al",
    "es", "s", "y",
)


def _morph_shield(low: str) -> str | None:
    """Suffix whose stem is a real word — the token *could* be a valid
    inflection the lexicon lacks ('talks', 'funnier')."""
    for suf in _SUFFIXES:
        if not low.endswith(suf) or len(low) - len(suf) < 3:
            continue
        stem = low[: -len(suf)]
        stems = {stem, stem + "e"}
        if suf in ("ies", "ied", "ier", "iest", "ily"):
            stems.add(stem + "y")
        # Consonant doubling only happens before vowel-initial suffixes
        # like -ing/-ed/-y ("running"→run, "funny"→fun) — never -ly.
        if (suf in ("ing", "ed", "er", "est", "ers", "y", "en")
                and len(stem) > 3 and stem[-1] == stem[-2]):
            stems.add(stem[:-1])
        if any(s in _vocab() for s in stems):
            return suf
    return None


def _looks_like_word(low: str) -> bool:
    """Real word (dictionary), vocab member, or inflection of one."""
    return low in _known() or _morph_shield(low) is not None


# ---------------------------------------------------------------------------
# Candidate scoring
# ---------------------------------------------------------------------------

def _freq_bonus(word: str) -> float:
    rank = _WORD_RANK.get(word)
    if rank is None:
        return 0.5 if word in DOMAIN_WORDS else 0.4
    if rank >= 300:
        # The list's first ~300 words are truly frequency-ordered; the
        # remainder is alphabetical, so a flat mid bonus is honest.
        return 0.4
    # Sharp decay — "the"/"and" must decisively beat "tea"/"ten" for teh.
    return 1.5 / (1.0 + rank / 35.0)


def _candidates(token: str, ctx_words: set[str], *,
                decisive_only: bool = False) -> list[tuple[str, float]]:
    """Score every plausible correction for an unknown token."""
    sk = _skeleton(token)
    word_sk = _word_skeletons()
    max_dl = 1 if len(token) <= 4 else 2
    scored: dict[str, float] = {}

    # Phonetic-only candidates (dl > max_dl) need a long enough token for
    # the skeleton to mean anything — on "teh" a bare skeleton ties "the"
    # with "they". The dl<=max_dl branch can use the skeleton at any
    # length because edit distance already vetted the pair.
    phonetic = len(token) >= 5
    for word, wsk in word_sk.items():
        if word == token or len(word) < 3:
            continue
        sk_eq = sk == wsk
        if not sk_eq and len(word) - len(token) > 4:
            continue  # far too long to be this typo
        dl = _dl(token, word, limit=4)
        sim = -1.0
        if decisive_only:
            # Only the near-certain tier: one edit away AND
            # phonetically identical — a real-word shield must not
            # block this ('editer' looks like edit+er but 'editor'
            # exists one keystroke away). Deletions stay shielded:
            # 'waiter'→'water' is a real word shortened, not a typo.
            if dl == 1 and sk_eq and len(word) >= len(token):
                sim = 3.9
        elif dl <= max_dl:
            if sk_eq and dl == 1:
                # One-edit-away AND phonetically identical — swaps and
                # doubled letters land here; near-certain.
                sim = 3.9
            else:
                sim = 2.2 - 0.9 * (dl - 1) + (0.3 if sk_eq else 0.0)
        if not decisive_only and phonetic and sk_eq:
            # Phonetically identical but far on the surface — weaker
            # than a clean one-edit match, still beats no signal.
            sim = max(sim, 1.9 - 0.15 * max(0, dl - 3))
        if sim < 0:
            continue
        score = sim + _freq_bonus(word)
        if word in ctx_words:
            score += 2.5
        if score > scored.get(word, 0.0):
            scored[word] = score

    return sorted(scored.items(), key=lambda kv: -kv[1])


def _same_stem(a: str, b: str) -> bool:
    """Morphological siblings (recommend/recommended) — an inflection of
    the front-runner is not a competing interpretation."""
    stem = min(len(a), len(b)) - 2
    if stem < 4:
        stem = min(len(a), len(b))
    if stem < 3:
        return a == b
    return a[:stem] == b[:stem]


def _pick(token: str, ctx_words: set[str], *,
          decisive_only: bool = False) -> str | None:
    cands = _candidates(token, ctx_words, decisive_only=decisive_only)
    if not cands:
        return None
    best, score = cands[0]
    runner_word, runner = next(
        ((w, s) for w, s in cands[1:] if not _same_stem(best, w)),
        ("", 0.0))
    if score >= 1.5 and (score - runner >= 0.5 or score >= runner * 1.25):
        return best
    if (
        score >= 1.5 and runner_word
        and _dl(token, best) < _dl(token, runner_word)
    ):
        # The winner needs strictly fewer edits than every unrelated
        # rival — 'reccomend'->'recommend' (dl1) must not be blocked by
        # a same-score dl2 rival like 'richmond'.
        return best
    return None  # ambiguous or too weak — leave it


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def normalize_user_text(
    text: str,
    *,
    context_words: Iterable[str] = (),
) -> tuple[str, list[dict[str, Any]]]:
    """Return ``(normalized_text, fixes)``.

    ``fixes`` is a list of ``{"raw": ..., "fixed": ...}`` for tracing.
    The input is returned unchanged when nothing needs correcting.
    """
    t = str(text or "")
    if not t.strip():
        return t, []

    # Phase 1 — context-gated confusion rules on a lowercase render are
    # position-sensitive; apply them by scanning the ORIGINAL text with
    # case-insensitive patterns and splicing replacements back in.
    fixes: list[dict[str, Any]] = []
    out = t
    for pat, rep in _CONFUSION_RULES:
        ci = re.compile(pat.pattern, re.IGNORECASE)
        spans = _protected_spans(out)

        def _sub(m: re.Match) -> str:
            if _in_spans(m.start(), spans):
                return m.group(0)
            fixes.append({"raw": m.group(0), "fixed": rep})
            return rep

        out = ci.sub(_sub, out)

    spans = _protected_spans(out)

    ctx_words = {str(w).lower() for w in context_words if w}
    ctx_words |= _message_context_words(out)
    vocab = _vocab()
    known = _known()

    pieces: list[str] = []
    last = 0
    for m in _TOKEN_RE.finditer(out):
        s, e = m.span()
        pieces.append(out[last:s])
        last = e
        tok = m.group(0)
        low = tok.lower().replace("’", "'")

        # --- protection gates ------------------------------------------------
        if len(low) < 3 or low in known or _in_spans(s, spans):
            pieces.append(tok)
            continue
        if "'" in low:
            # Possessives/contractions ("cat's", "dogs'") — the tail
            # after an apostrophe is grammar, not part of the stem, and
            # must never look like an editable typo.
            pieces.append(tok)
            continue
        shield = _morph_shield(low) if low not in vocab else None
        if shield is not None:
            # Inflected/derived real word not in the lexicon — usually
            # leave it. A -s/-es shield stays absolute (plurals are
            # almost always real); other shields yield to a decisive
            # same-skeleton one-edit candidate ('editer' -> 'editor').
            fixed = None
            if shield not in ("s", "es"):
                fixed = _pick(low, ctx_words, decisive_only=True)
            if fixed is None:
                pieces.append(tok)
                continue
            if tok[0].isupper():
                fixed = fixed.capitalize()
            pieces.append(fixed)
            fixes.append({"raw": tok, "fixed": fixed})
            continue
        prev = out[s - 1] if s > 0 else ""
        nxt = out[e] if e < len(out) else ""
        if ((prev and prev in "/@#.") or (nxt and nxt in ".-_")
                or prev.isdigit() or nxt.isdigit()):
            pieces.append(tok)
            continue
        if tok.isupper() and len(tok) <= 4 and tok.isalpha():
            # Short all-caps tokens are usually acronyms (RAM, GPU).
            pieces.append(tok)
            continue
        if any(c.isupper() for c in tok[1:]):
            # camelCase / ProperCase mid-word → identifier.
            pieces.append(tok)
            continue
        if (tok[0].isupper() and s > 0
                and not _SENT_BOUND_RE.search(out[:s])):
            # Capitalized mid-sentence and not a known word → proper noun.
            pieces.append(tok)
            continue

        fixed = _pick(low, ctx_words)
        if fixed is None:
            pieces.append(tok)
            continue
        if tok[0].isupper():
            fixed = fixed.capitalize()
        pieces.append(fixed)
        fixes.append({"raw": tok, "fixed": fixed})
    pieces.append(out[last:])
    return "".join(pieces), fixes
