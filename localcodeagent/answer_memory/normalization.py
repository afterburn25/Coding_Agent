"""Deterministic question normalization for Nexus Answer Memory.

Normalization must make paraphrases converge without destroying meaning:
numbers, filenames, model names, versions, and code identifiers are preserved
("Qwen 14B" must never collapse into "Qwen 30B").
"""

from __future__ import annotations

import re
import unicodedata

# Harmless trailing punctuation only — never strip internal punctuation that
# may be part of identifiers (file.py, v2.1, C#, ...).
_TRAILING_PUNCT = "?!.,;: \t\r\n"

_CONTRACTIONS = {
    "what's": "what is",
    "what're": "what are",
    "who's": "who is",
    "who're": "who are",
    "where's": "where is",
    "when's": "when is",
    "why's": "why is",
    "how's": "how is",
    "it's": "it is",
    "that's": "that is",
    "there's": "there is",
    "can't": "cannot",
    "won't": "will not",
    "don't": "do not",
    "doesn't": "does not",
    "didn't": "did not",
    "isn't": "is not",
    "aren't": "are not",
    "wasn't": "was not",
    "weren't": "were not",
    "couldn't": "could not",
    "shouldn't": "should not",
    "wouldn't": "would not",
    "haven't": "have not",
    "hasn't": "has not",
    "i'm": "i am",
    "you're": "you are",
    "we're": "we are",
    "they're": "they are",
    "i've": "i have",
    "we've": "we have",
    "you've": "you have",
    "they've": "they have",
    "i'll": "i will",
    "you'll": "you will",
    "we'll": "we will",
    "let's": "let us",
}

_TOKEN_RE = re.compile(r"[0-9a-zA-Z][0-9a-zA-Z_+.#-]*")

# Domain synonym → concept expansion. Lets paraphrases converge semantically:
# "which checkpoint" ≈ "what model", "capabilities" ≈ "features".
_SYNONYMS = {
    "checkpoint": "model", "checkpoints": "model", "weights": "model",
    "models": "model", "llm": "model",
    "capabilities": "feature", "capability": "feature", "features": "feature",
    "functionality": "feature",
    "normally": "default", "usually": "default", "typically": "default",
    "by default": "default", "defaults": "default",
    "generate": "create", "generates": "create", "generating": "create",
    "generations": "create", "creation": "create", "creates": "create",
    "images": "image", "img": "image", "picture": "image", "pictures": "image",
    "photos": "image", "photo": "image", "pic": "image",
    "utilize": "use", "utilizes": "use", "uses": "use",
    "employs": "use", "leverage": "use", "leverages": "use",
    "enable": "enable", "enables": "enable", "disable": "disable",
    "disables": "disable", "deactivate": "disable",
    "repo": "repository", "repos": "repository", "repositories": "repository",
    "codebase": "repository",
    "nexus": "nexus_core",
    "cfg": "config", "configuration": "config", "configurations": "config",
    "settings": "config", "preferences": "config",
    "assist": "help", "assists": "help", "assistance": "help",
    "memory": "memory", "memories": "memory", "remembers": "remember",
    "learned": "learn", "learnt": "learn", "learning": "learn",
    "change": "update", "modify": "update", "edit": "update",
    "remove": "delete", "erase": "delete", "wipe": "delete",
    "launch": "start", "run": "start", "execute": "start", "boot": "start",
    "quit": "stop", "exit": "stop", "shutdown": "stop", "kill": "stop",
    "location": "where", "path": "where",
    "cost": "price", "price": "price",
    "rapid": "fast", "quick": "fast", "quickly": "fast", "faster": "fast",
    "slow": "slow", "slower": "slow",
    "large": "big", "huge": "big", "small": "small",
    "accurate": "correct", "right": "correct",
    "incorrect": "wrong", "mistaken": "wrong",
    "currently": "now", "presently": "now", "today": "now",
    "support": "support", "supports": "support", "supported": "support",
    "doc": "documentation", "docs": "documentation",
    "turn": "enable", "activate": "enable", "activation": "enable",
    "deactivation": "disable",
    "generation": "create",
}

# Opposed concepts — if one side asks to do X and the other asks to do the
# opposite, the questions are NOT equivalent ("start ComfyUI" ≠ "stop ComfyUI").
_OPPOSED = {
    "start": "stop", "stop": "start",
    "enable": "disable", "disable": "enable",
    "on": "off", "off": "on",
    "add": "remove", "remove": "add",
    "create": "delete", "delete": "create",
    "install": "uninstall", "uninstall": "install",
    "open": "close", "close": "open",
    "allow": "deny", "deny": "allow",
    "increase": "decrease", "decrease": "increase",
    "min": "max", "max": "min", "minimum": "maximum", "maximum": "minimum",
    "latest": "oldest", "oldest": "latest", "newest": "oldest",
    "up": "down", "down": "up",
}


def opposed(a: str, b: str) -> bool:
    """True when tokens a and b name opposite actions/values."""
    return _OPPOSED.get(a) == b


# Action/scaffold verbs. A paraphrase often substitutes one verb for another
# ("generate images with" ↔ "use"), which is NOT an entity swap. Canonical
# uncovered sets that differ *only* in action tokens are therefore not a
# conflict — unlike noun/value swaps (france/italy, image/voice).
_ACTIONS = {
    "use", "create", "update", "delete", "remove", "add", "enable", "disable",
    "start", "stop", "install", "uninstall", "open", "close", "allow", "deny",
    "config", "help", "learn", "remember", "support", "set", "reset", "find",
    "show", "list", "check", "fix", "build", "deploy", "run", "load", "save",
    "see", "view", "get", "make", "increase", "decrease", "change",
}


def is_action(token: str) -> bool:
    return token in _ACTIONS


def is_concept(token: str) -> bool:
    """True when a token participates in domain concept vocabulary."""
    return token in _CONCEPT_VOCAB


def synonyms(token: str) -> list[str]:
    return [_SYNONYMS[token]] if token in _SYNONYMS else []


# Scaffold/stop words — removed before content coverage scoring.
STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "do", "does", "did", "can",
    "could", "should", "would", "will", "may", "might", "must", "shall",
    "i", "you", "me", "my", "your", "yours", "it", "its", "we", "our", "they",
    "their", "he", "she", "his", "her", "to", "of", "in", "on", "for", "with",
    "by", "at", "from", "and", "or", "but", "not", "be", "been", "being",
    "this", "that", "these", "those", "there", "here", "what", "which", "who",
    "whom", "whose", "when", "where", "why", "how", "tell", "show", "explain",
    "please", "about", "any", "some", "all", "much", "many", "if", "so",
    "than", "then", "into", "way", "thing", "things", "stuff", "kind", "sort",
    "type", "lot", "lots", "really", "just", "even", "still", "back", "get",
    "got", "getting", "make", "makes", "want", "need", "know", "mean", "like",
    "something", "someone", "anyone", "anything", "able", "go", "going",
    "have", "has", "had", "having", "file", "files",
}

# Concept vocabulary — tokens that participate in synonym expansion and are
# therefore "soft" (their uncovered absence is not a hard semantic conflict).
_CONCEPT_VOCAB = set(_SYNONYMS) | set(_SYNONYMS.values()) | {
    part for v in _SYNONYMS.values() for part in v.split("_")
}


def content_tokens(text: str) -> set[str]:
    return {t for t in expanded_tokens(text) if t not in STOPWORDS and len(t) > 1}


def content_parts(tokens_: set[str]) -> set[str]:
    """Tokens plus concept-token parts ('nexus_core' → {'nexus','core'})."""
    out = set()
    for t in tokens_:
        out.add(t)
        if "_" in t:
            out.update(t.split("_"))
    return out


def proper_tokens(raw: str) -> set[str]:
    """Discriminative tokens: mid-sentence capitalized words, digits,
    identifiers. A mismatch here must block a semantic match — 'capital of
    France' vs 'capital of Italy' differs only by the entity."""
    out = set()
    words = str(raw).split()
    for i, w in enumerate(words):
        stripped = w.strip("?!.,;:()\"'`[]")
        if stripped.lower().endswith("'s"):
            stripped = stripped[:-2]
        if not stripped:
            continue
        has_digit = any(c.isdigit() for c in stripped)
        # Mid-sentence capitals are discriminative UNLESS the word is concept
        # vocabulary (e.g. "Nexus", "Model") — those expand via _SYNONYMS and
        # are covered by content matching instead.
        cap_mid = (
            i > 0 and stripped[0].isupper()
            and stripped.lower() not in STOPWORDS
            and stripped.lower() not in _CONCEPT_VOCAB
        )
        if has_digit or cap_mid or "." in stripped or "_" in stripped:
            if len(stripped) > 1 or has_digit:
                out.add(stripped.lower())
    return out


def hard_content(tokens_: set[str]) -> set[str]:
    """Content tokens that are NOT part of the known concept vocabulary —
    uncovered ones represent a genuine semantic conflict."""
    return {t for t in tokens_ if t not in _CONCEPT_VOCAB}


def canonical_tokens(text: str) -> set[str]:
    """Content tokens collapsed to canonical concept forms.

    Synonym variants map to their concept ('checkpoint' → 'model'), so
    'image model' vs 'voice model' exposes the *image/voice* contrast even
    though both words are vocabulary terms. Multiword concepts collapse too
    ('Nexus Core' → 'nexus_core', 'image generation' → 'image create').
    """
    s = normalize_question(text)
    raw = _TOKEN_RE.findall(s)
    out: set[str] = set()
    consumed: set[str] = set()
    for phrase, concept in _PHRASE_CONCEPTS:
        if phrase in s:
            consumed.update(_TOKEN_RE.findall(phrase))
            out.update(concept.split())
    for t in raw:
        if t in consumed or t in STOPWORDS or len(t) <= 1:
            continue
        out.add(_SYNONYMS.get(t, t))
    return out


def opposed_conflict(q_canon: set[str], s_canon: set[str]) -> bool:
    """True when one side contains a token whose opposite appears (only) on
    the other side — 'start ComfyUI' vs 'stop ComfyUI'."""
    for t in q_canon:
        o = _OPPOSED.get(t)
        if o and o in s_canon and o not in q_canon:
            return True
    for t in s_canon:
        o = _OPPOSED.get(t)
        if o and o in q_canon and o not in s_canon:
            return True
    return False


# Multiword → concept expansions (shared by embeddings and canonicalization).
_PHRASE_CONCEPTS = (
    ("by default", "default"), ("text to image", "image create"),
    ("image generation", "image create"), ("generate image", "image create"),
    ("can do", "feature"), ("able to", "can"), ("how do i", "how"),
    ("what do you", "what"), ("what does", "what"), ("tell me about", "what"),
    ("real time", "live"), ("open source", "opensource"),
    ("nexus core", "nexus_core"),
)


def expanded_tokens(text: str) -> list[str]:
    """Tokens plus concept synonyms — used by the semantic embedder."""
    base = tokens(text)
    out = list(base)
    for t in base:
        out.extend(synonyms(t))
    joined = " ".join(base)
    for phrase, concept in _PHRASE_CONCEPTS:
        if phrase in joined:
            out.extend(concept.split())
    return out


def normalize_question(text: str) -> str:
    """Canonicalize a question for exact-match lookup.

    - Unicode NFKC normalization
    - lowercase (ASCII case only is meaningful for matching here)
    - expand common contractions
    - collapse whitespace
    - strip harmless trailing punctuation
    - preserve numbers, versions, filenames, identifiers
    """
    s = unicodedata.normalize("NFKC", str(text or ""))
    s = s.strip().lower()
    # Expand contractions word-wise so apostrophes inside identifiers survive.
    words = s.split(" ")
    s = " ".join(_CONTRACTIONS.get(w, w) for w in words)
    s = re.sub(r"\s+", " ", s).strip()
    s = s.strip(_TRAILING_PUNCT)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def normalize_answer_key(text: str) -> str:
    """Fingerprint-ish normalization for comparing answer equivalence."""
    s = unicodedata.normalize("NFKC", str(text or "")).strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s.strip(_TRAILING_PUNCT)


def tokens(text: str) -> list[str]:
    """Content tokens for lexical similarity — identifiers and digits kept."""
    return _TOKEN_RE.findall(normalize_question(text))


def token_set(text: str) -> set[str]:
    return set(tokens(text))
