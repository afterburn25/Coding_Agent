"""Conversation state machinery — topic tracking, entity extraction,
decision and open-loop detection folded into ActiveContext.

The ActiveContext dataclass holds the state; this module holds the
deterministic extraction/update logic so the data container stays clean.
Everything here is regex/heuristic structure — no model call — and runs
once per user turn inside run() between understand_turn and lane
selection.
"""
from __future__ import annotations

import re
import time
from typing import Any

# ---------------------------------------------------------------------------
# Entity extraction — deterministic NER-lite.
# ---------------------------------------------------------------------------

_WORD_STOP = frozenset({
    "i", "the", "a", "an", "and", "or", "but", "if", "when", "while",
    "then", "so", "to", "of", "for", "in", "on", "at", "by", "with",
    "from", "as", "is", "are", "was", "were", "be", "been", "it", "its",
    "this", "that", "these", "those", "they", "them", "we", "you", "he",
    "she", "my", "your", "our", "their", "his", "her", "not", "no",
    "do", "does", "did", "done", "have", "has", "had", "can", "could",
    "would", "should", "will", "shall", "may", "might", "must", "let",
    "what", "who", "where", "when", "why", "how", "which", "okay", "ok",
    "yes", "yeah", "no", "nope", "now", "then", "just", "also", "very",
    "make", "made", "get", "got", "put", "see", "saw", "say", "said",
    "tell", "told", "know", "think", "want", "like", "need", "try",
    "use", "used", "using", "go", "going", "come", "take", "give",
    "find", "work", "working", "run", "running", "still", "really",
    "actually", "probably", "maybe", "something", "anything", "someone",
    "anyone", "thing", "things", "way", "stuff", "lot", "kind", "sort",
    "today", "yesterday", "tomorrow", "here", "there", "back", "again",
    "new", "old", "good", "bad", "right", "wrong", "sure", "please",
    "thanks", "hello", "hi", "hey", "because", "about", "into", "over",
    "after", "before", "between", "under", "more", "most", "other",
    "some", "such", "only", "same", "too", "than", "then", "out", "up",
    "down", "off", "any", "all", "each", "few", "much", "many", "own",
    "once", "ever", "never", "always", "often", "sometimes", "either",
})

# Canonical project vocabulary — stable entities Nexus knows about.
# Each entry: (pattern, entity_id, type, aliases).
_CANON_ENTITIES: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    (r"\bnexus(?:\s*core)?\b", "app:nexus-core", "app",
     ("nexus", "nexus core", "you", "yourself")),
    (r"\bcoding[_\s-]?agent\b", "repo:coding-agent", "project",
     ("coding agent", "coding_agent", "the repo", "the repository")),
    (r"\bmoltbook\b", "app:moltbook", "app",
     ("moltbook", "the ai community", "the community")),
    (r"\bisabella\b(?:\s*v?\d+(?:\.\d+)?)?\b", "voice:isabella", "voice",
     ("isabella", "the voice", "her voice", "your voice", "the preset")),
    (r"\bchatterbox\b(?:\s*turbo)?\b", "voice-engine:chatterbox",
     "engine", ("chatterbox", "chatterbox turbo", "the engine")),
    (r"\bkokoro\b", "voice-engine:kokoro", "engine",
     ("kokoro", "the voice", "the voice preset",
      "kokoro voice preset", "the engine")),
    (r"\bdevin\b", "agent:devin", "agent", ("devin",)),
    (r"\bgithub\b", "service:github", "service", ("github", "the remote")),
    (r"\bcomfyui\b", "app:comfyui", "app", ("comfyui", "comfy")),
    (r"\binstaller\b", "artifact:installer", "artifact",
     ("the installer", "installer", "setup", "the setup")),
    (r"\bllama[.\s-]*cpp\b", "runtime:llama-cpp", "runtime",
     ("llama.cpp", "llama cpp", "llama")),
    (r"\brtx\s*3080\b", "hardware:rtx-3080", "hardware",
     ("the gpu", "the card", "the 3080", "rtx 3080")),
    (r"\bwebview2\b", "lib:webview2", "lib", ("webview2",)),
    (r"\bpyinstaller\b", "tool:pyinstaller", "tool", ("pyinstaller",)),
    (r"\banswer\s+memory\b", "system:answer-memory", "system",
     ("answer memory", "the answer memory")),
    (r"\bnexus\s+brain\b", "system:nexus-brain", "system",
     ("nexus brain", "the brain")),
    (r"\bconversation\s+memory\b", "system:conversation-memory",
     "system", ("conversation memory",)),
    (r"\bsemantic\s+frame\b", "system:semantic-frame", "system",
     ("semantic frame",)),
    (r"\bsemantic\s+graph\b|\bstate\s+graph\b", "system:state-graph",
     "system", ("state graph", "conversation state")),
    (r"\bqwen[\d.]*[bkm]?\b", "model:qwen", "model", ("qwen", "the model")),
    (r"\bjuggernaut\b", "model:juggernaut", "model", ("juggernaut",)),
    (r"\bflux[\d.]*\b(?:\s*klein)?\b", "model:flux-klein", "model",
     ("flux", "flux klein", "the flux model")),
    (r"\bmoltbook\b", "app:moltbook", "app",
     ("moltbook", "ai community")),
)

# Capitalized multiword names not covered by canon — "Isabella V7",
# "John Hamburn", "Moltbook". Consuming merges: "V7" folds into a
# preceding name when adjacent.
_PROPER_RE = re.compile(
    r"\b([A-Z][a-zA-Z0-9]+(?:[ \-][A-Z][a-zA-Z0-9.]+){0,3})\b")

# Versions — v0.35.2, 0.36.0.
_VERSION_RE = re.compile(r"\bv?\d+\.\d+(?:\.\d+)?(?:[a-z]\d*)?\b")

# File paths — D:\x, ~/x, ./x, /x/y, name.ext.
_PATH_RE = re.compile(
    r"(?:[A-Za-z]:\\[^\s\"']+|~[/\\][^\s\"']+|\.[/\\][^\s\"']+|"
    r"/[^\s\"']+/[^\s\"']*|\b[\w.-]+\.(?:py|js|json|md|gguf|iss|ps1|"
    r"exe|dll|txt|png|jpg|safetensors|bin|yaml|yml|toml|ini)\b)")

# model ids — qwen3-14b, 30b-a3b, etc.
_MODEL_ID_RE = re.compile(r"\b[a-z]+\d+(?:\.\d+)?b(?:-[\w.]+)?\b", re.I)

_ENTITY_STOP = frozenset({
    "the", "this", "that", "yes", "no", "nexus", "i", "we", "you",
    "it", "he", "she", "they", "and", "but", "or", "if", "so", "then",
    "what", "when", "where", "who", "how", "why", "which", "can",
    "could", "would", "should", "will", "do", "does", "did", "is",
    "are", "was", "were", "be", "been", "have", "has", "had", "not",
    "just", "still", "also", "very", "really", "actually", "probably",
    "maybe", "okay", "ok", "sure", "please", "thanks", "let", "lets",
    "now", "today", "yesterday", "tomorrow", "good", "bad", "new",
    "old", "same", "other", "another", "more", "most", "all", "any",
    "some", "one", "two", "three", "first", "second", "third", "last",
    "next", "previous", "earlier", "later", "back", "again", "going",
})

_ENTITY_TYPE_RE = {
    "file": _PATH_RE,
    "version": _VERSION_RE,
    "model": _MODEL_ID_RE,
}


def _slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:48]


def _entity_type(label: str) -> str:
    """Best-effort type for an extracted surface form."""
    low = label.lower()
    if _PATH_RE.search(label):
        return "file"
    if _VERSION_RE.fullmatch(low):
        return "version"
    if _MODEL_ID_RE.search(label):
        return "model"
    if re.search(r"\b(?:voice|preset|clone)\b", low):
        return "voice"
    if re.search(r"\b(?:model|checkpoint|gguf)\b", low):
        return "model"
    if re.search(r"\b(?:repo|project|codebase|module|package|lib)\b",
                 low):
        return "project"
    if re.search(r"\b(?:app|tool|program|service|runtime|engine|"
                 r"framework|backend|frontend)\b", low):
        return "app"
    if re.search(r"\b(?:install|setup|build|release|patch|commit)\b",
                 low):
        return "artifact"
    if re.search(r"\b(?:agent|bot|assistant|ai)\b", low):
        return "agent"
    if re.search(r"\b(?:bug|error|issue|crash|failure)\b", low):
        return "error"
    return "concept"


def extract_entities(text: str, frame: Any = None) -> list[dict]:
    """Surface entity mentions in one utterance — [{id,type,label,
    aliases,conf}]. Deterministic; proper-noun capture + canonical vocab
    + typed patterns (paths, versions, model ids)."""
    out: dict[str, dict] = {}

    def _put(label: str, etype: str, aliases=(), conf: float = 0.6):
        label = str(label or "").strip(" ,.;:—-")
        if not label or len(label) < 2:
            return
        if label.lower() in _ENTITY_STOP:
            return
        if etype == "concept" and label.lower() in _WORD_STOP:
            return
        key = None
        # Canon entities get a stable id; others slug on label.
        for pat, eid, et, al in _CANON_ENTITIES:
            if re.search(pat, label, re.IGNORECASE):
                eid = eid
                if re.fullmatch(pat, label, re.IGNORECASE):
                    etype = et
                    key = eid
                    aliases = tuple(sorted(set(aliases) | set(al)))
                    conf = max(conf, 0.9)
                    break
        if key is None:
            if etype == "concept":
                etype = _entity_type(label)
            key = f"{etype}:{_slug(label)}"
        ent = out.get(key)
        if ent is None:
            out[key] = {"id": key, "type": etype, "label": label,
                        "aliases": sorted(set(aliases)),
                        "conf": round(conf, 3)}
        else:
            ent["aliases"] = sorted(set(ent["aliases"]) | set(aliases))
            ent["conf"] = max(ent["conf"], round(conf, 3))

    masked = getattr(frame, "masked_case", "") or text
    # 1) Canon entities on masked text (quoted spans excluded).
    for pat, eid, et, aliases in _CANON_ENTITIES:
        for m in re.finditer(pat, masked, re.IGNORECASE):
            _put(m.group(0), et, aliases, 0.9)
    # 2) Proper-noun runs on the cased mask.
    for m in _PROPER_RE.finditer(masked):
        label = m.group(1)
        if label.lower() in _ENTITY_STOP or label in ("I",):
            continue
        _put(label, "concept", (), 0.55)
    # 3) Typed patterns.
    for m in _PATH_RE.finditer(masked):
        _put(m.group(0), "file", (), 0.85)
    for m in _VERSION_RE.finditer(masked):
        _put(m.group(0), "version", (), 0.8)
    for m in _MODEL_ID_RE.finditer(masked):
        if not re.search(_MODEL_ID_RE, m.group(0)):
            continue
        _put(m.group(0), "model", (), 0.8)
    return list(out.values())


# ---------------------------------------------------------------------------
# Topic tracking
# ---------------------------------------------------------------------------

_TOPIC_NOUN_RE = re.compile(
    r"\b(?:new\s+)?(?:topic|subject)\s*[:—–-]\s*([^,.;?!]{2,60})|"
    r"\b(?:about|regarding|concerning|re\s*:)\s+"
    r"(?:the\s+|a\s+|an\s+|my\s+|our\s+|your\s+)?([a-z0-9][\w .'/-]{1,60})",
    re.IGNORECASE)

_WORK_ON_RE = re.compile(
    r"\b(?:work(?:ing)?\s+on|discuss(?:ing)?|talk(?:ing)?\s+about|"
    r"focus(?:ing)?\s+on|look\s+at|get\s+back\s+to|deal\s+with|"
    r"dig\s+into|dive\s+into|go\s+over|review|tackle|handle)\s+"
    r"(.{2,60}?)(?:[.!?,;:]|$)",
    re.IGNORECASE)

_RESUME_RE = re.compile(
    r"^\s*(?:okay|ok|so|alright|anyway)?[,!\s]*"
    r"(?:let'?s\s+)?(?:continue|go\s+on|carry\s+on|resume|"
    r"keep\s+going|back\s+to\s+it|pick\s+(?:it|that|this)\s+"
    r"(?:back\s+)?up)\b[.!]?\s*$",
    re.IGNORECASE)

_UTILITY_Q_RE = re.compile(
    r"\b(?:what\s+time|what'?s\s+the\s+time|what\s+day|what'?s\s+the\s+"
    r"date|what\s+version|how\s+much\s+(?:vram|ram|memory|disk)|"
    r"battery|weather|who\s+are\s+you|what\s+are\s+you)\b", re.I)


def derive_topic(text: str, env: Any = None,
                 entities: list[dict] | None = None) -> str:
    """Best short label for what the turn is about — explicit markers
    first, then the highest-salience extracted entity, then the frame's
    object/subject."""
    t = str(text or "")
    m = _TOPIC_NOUN_RE.search(t)
    if m:
        return next((g for g in m.groups() if g),
                    "").strip(" .,;—–-")[:60]
    # 'let's work on Moltbook peer learning' — the governed phrase is
    # the topic, richer than a bare entity label.
    m = _WORK_ON_RE.search(t)
    if m:
        return m.group(1).strip(" .,;—–-")[:60]
    # The object phrase is richer than an entity label — 'Moltbook peer
    # learning' over 'Moltbook'.
    if env is not None:
        subj = getattr(env, "subject", "")
        if subj and len(subj) >= 3:
            return str(subj).strip(" .,;")[:60]
        tgt = getattr(env, "topic_target", "")
        if tgt:
            return str(tgt)[:60]
    frame = getattr(env, "semantic", None)
    obj = getattr(frame, "obj", "") if frame else ""
    if obj:
        return str(obj).strip(" .,;")[:60]
    if entities:
        top = max(entities, key=lambda e: e.get("conf", 0))
        if top.get("conf", 0) >= 0.8:
            label = top["label"][:60]
            # Prefer the fuller noun phrase the user actually wrote —
            # 'kokoro voice preset' over the bare 'kokoro' alias, so
            # parked/recalled topics keep their descriptive name.
            m = re.search(
                r"\b" + re.escape(label.lower()) +
                r"((?:\s+[a-z][a-z0-9'/-]*){1,3})", t.lower())
            if m:
                words = (label.lower() + m.group(1)).split()
                while len(words) > 1 and \
                        (words[-1] in _WORD_STOP
                         or words[-1] in _ENTITY_STOP):
                    words.pop()
                if len(words) > 1:
                    label = " ".join(words)[:60]
            return label
    return ""


def is_utility_question(text: str, env: Any = None) -> bool:
    """Interruption class — a bounded factual aside that should not push
    the topic stack. 'what time is it', 'what version are you on'."""
    if _UTILITY_Q_RE.search(str(text or "")):
        return True
    if env is None:
        return False
    slot = getattr(env, "requested_slot", "")
    act = getattr(env, "speech_act", "")
    return act == "question" and slot in ("time", "operational_status")


def topic_label_match(a: str, b: str) -> float:
    """0..1 word-overlap between two topic labels."""
    wa = {w for w in re.findall(r"[a-z0-9]+", a.lower())
          if w not in _WORD_STOP}
    wb = {w for w in re.findall(r"[a-z0-9]+", b.lower())
          if w not in _WORD_STOP}
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / max(1, min(len(wa), len(wb)))


# ---------------------------------------------------------------------------
# Decision extraction — user commitments that outrank memory.
# ---------------------------------------------------------------------------

_DECIDE_RE = re.compile(
    r"\b(?:we'?ll|we\s+will|let'?s|i'?ll|i\s+will|let\s+us)\s+"
    r"(?:go\s+with|use|pick|choose|settle\s+on|take|stick\s+with|"
    r"keep|make\s+it|name|call|select)\s+([^,.;?!]{2,80})|"
    r"\b(?:use|using|pick|picked|chose|choose|chosen|select|selected|"
    r"settled?\s+on|decided\s+on|going\s+with|sticking\s+with|"
    r"name|call|keep|take)\s+"
    r"(?:the\s+|a\s+|an\s+|it\s+|them\s+|this\s+|that\s+)?"
    r"([^,.;?!]{2,80})|"
    r"\bmake\s+(?:that|it|this|them)\s+([^,.;?!]{2,80})|"
    r"\b(?:the\s+answer|the\s+choice|final\s+answer|the\s+decision)\s+"
    r"(?:is|was)\s+([^,.;?!]{2,80})|"
    r"\b(?:that'?s|it'?s)\s+(?:settled|decided|approved|final)\b\.?\s*"
    r"([^,.;?!]{0,80})",
    re.IGNORECASE)


def extract_decision(text: str, env: Any = None) -> dict | None:
    """A settled choice — 'use port 9000', 'Isabella V7 is approved',
    'let's go with SQLite'. Returns {subject,value,confidence}."""
    t = str(text or "")
    frame = getattr(env, "semantic", None)
    # Offers/questions/imperative-negations aren't commitments.
    if frame is not None and frame.speech_act in (
            "offer", "preference_question", "hypothetical",
            "comparison", "question"):
        return None
    m = _DECIDE_RE.search(t)
    if not m:
        # Bare restatement — 'no wait, actually green', 'actually, 9500'
        # carries a value with no verb or subject; it inherits the most
        # recent active decision's subject downstream.
        bare = re.match(
            r"^\s*(?:(?:no|nope|nah|wait|actually|sorry|hmm|um)[,]?\s*)+"
            r"(?:i\s+meant?\s+|it'?s\s+|that'?s\s+|make\s+it\s+|"
            r"use\s+|let'?s\s+(?:do|go\s+with)\s+)?"
            r"([a-z0-9][a-z0-9 ._-]{0,30}?)"
            r"(?:\s+(?:now|instead|this\s+time))?[.!?]?\s*$",
            t, re.IGNORECASE)
        if not bare:
            return None
        val = bare.group(1).strip(" .,;'\"")
        if not val or val.lower() in _BARE_VALUE_STOP:
            return None
        return {"subject": "", "value": val, "confidence": 0.9}
    value = next((g for g in m.groups() if g), "")
    # The chosen value stops before a purpose/prepositional tail —
    # "use port 9000 for the bridge" -> "port 9000".
    tail_m = re.search(r"\b(?:for|on|in)\s+(?:the|a|an|our|your|its)\s+"
                       r"(\w+)", t, re.IGNORECASE)
    value = re.split(
        r"\s+(?:for|because|so\s+that|instead\s+of|rather\s+than)\s+",
        value)[0]
    value = re.sub(r"\s+(?:instead|now|this\s+time)\s*$", "", value,
                   flags=re.IGNORECASE)
    value = re.sub(r"\s+", " ", value).strip(" .,;'\"")[:120]
    if not value:
        return None
    # Subject = the domain noun the value belongs to when one is
    # present ('port', 'model', 'voice') — env.subject is often the
    # raw object span and reads as garbage ('9000 for the bridge').
    subject = ""
    vs = value.lower()
    for key in ("port", "version", "model", "voice", "preset",
                "name", "color", "date", "time", "path", "dir",
                "endpoint", "url", "branch", "file", "language",
                "theme", "engine", "host", "password", "token",
                "backend", "frontend", "database", "db", "server"):
        if re.search(rf"\b{re.escape(key)}\b", vs):
            subject = key
            break
    # 'let's call the project Nexus' / 'name it Vega' — the subject is
    # the thing's name.
    if not subject and re.search(
            r"\b(?:call|name|rename|dub)\b", t, re.IGNORECASE):
        subject = "name"
    # 'pick Isabella for the voice' → subject 'voice' from the tail.
    if not subject and tail_m:
        subject = tail_m.group(1).lower()
    if not subject and env is not None:
        subject = getattr(env, "subject", "") or getattr(
            env, "topic_target", "") or ""
    if not subject and frame is not None:
        subject = frame.obj or ""
    # A subject that echoes the value or reads as a prepositional
    # fragment ('for coding') is no subject — downstream shape/entity
    # matching may inherit a real one ('9500' → 'port').
    if subject and (subject.lower() == value.lower()
                    or re.match(r"^(?:for|on|in|to|with|about|at|by)\b",
                                subject.lower())):
        subject = ""
    # "use port 9000" → subject 'port', value '9000' — the domain noun
    # prefixes the value, don't repeat it.
    if subject and vs.startswith(subject + " "):
        value = value[len(subject):].strip(" .,;'\"")[:120]
        if not value:
            return None
    conf = 0.9 if env and getattr(env, "correction_of", "") else 0.75
    return {"subject": subject[:80],
            "value": value, "confidence": conf}


# ---------------------------------------------------------------------------
# Open-loop detection — unresolved asks, promised work, pending answers.
# ---------------------------------------------------------------------------

_RETURN_RE = re.compile(
    r"^\s*(?:(?:okay|ok|so|anyway|alright)[,!\s—–]*)*"
    r"(?:let'?s\s+|we\s+)?(?:go\s+)?back\s+to\s+([^—–]{1,60}?)"
    r"(?:\s*[—–,.;]|\s*$)|"
    r"^\s*(?:returning|back)\s+to\s+([^—–]{1,60}?)(?:\s*[—–,.;]|\s*$)",
    re.IGNORECASE)

_LOOP_RE = re.compile(
    r"\b(?:still\s+(?:need|needs|needed|waiting|pending|open|todo)\b|"
    r"(?:i|we|you)\s+(?:still\s+)?(?:need|needs|have)\s+to\b|"
    r"(?:i'?ll|i\s+will|we'?ll|we\s+will|let\s+me)\s+"
    r"(?:check|look|verify|confirm|test|get\s+back|circle\s+back|"
    r"follow\s+up|send|update|fix|finish|install|deploy|build|run)\b|"
    r"\bwaiting\s+(?:on|for)\b|\bhasn'?t\s+(?:replied|responded|"
    r"answered|come\s+back)\b|\b(?:remind\s+me|todo|to\s+do)\b|"
    r"\b(?:open|outstanding|unresolved)\s+(?:issue|task|item|question)\b|"
    r"\bcome\s+back\s+to\b|\brevisit\b|\blater\b.{0,20}\b(?:check|fix|"
    r"review|test|verify|look)\b)",
    re.IGNORECASE)

_BARE_VALUE_STOP = frozenset({
    "no", "yes", "yeah", "yep", "nope", "sure", "ok", "okay", "fine",
    "nothing", "never mind", "nevermind", "thanks", "thank you",
    "right", "good", "great", "perfect", "cool", "nice", "true",
    "agreed", "exactly", "correct", "i guess", "whatever", "anything",
    "nothing much", "all good", "never",
})

_LOOP_CLOSE_RE = re.compile(
    r"\b(?:it'?s|that'?s|is)\s+(?:fixed|done|resolved|working|sorted|"
    r"handled|closed)\b|\b(?:fixed|solved|resolved|handled|closed|"
    r"cancelled|canceled|never\s*mind|forget\s+(?:it|about))\b|"
    r"\b(?:no\s+longer|not)\s+(?:needed|an\s+issue|a\s+problem)\b",
    re.IGNORECASE)


def extract_open_loop(text: str, env: Any = None) -> dict | None:
    """A promise, pending check, or unresolved item worth tracking."""
    t = str(text or "")
    m = _LOOP_RE.search(t)
    if not m:
        return None
    # A question *about* an open thing isn't a new loop.
    frame = getattr(env, "semantic", None)
    if frame is not None and frame.speech_act in ("question",):
        if re.match(r"^(?:did|is|are|was|has|have|what|how)\b",
                    t.strip().lower()):
            return None
    return {"text": t[:160], "confidence": 0.65}


def loop_close_match(text: str) -> bool:
    return bool(_LOOP_CLOSE_RE.search(str(text or "")))


# ---------------------------------------------------------------------------
# Requirement accumulation — multi-turn spec assembly.
# ---------------------------------------------------------------------------

_REQ_RE = re.compile(
    r"\b(?:it|the\s+\w+|this|nexus|you)\s+(?:should|must|needs?\s+to|"
    r"has\s+to|ought\s+to|shall)\s+([^,.;?!]{2,80})|"
    r"\bi\s+want\s+(?:it|this|nexus|you)\s+to\s+([^,.;?!]{2,80})|"
    r"\b(?:it|this|nexus)\s+(?:can|could)\s+(?:also\s+)?([^,.;?!]{2,80})|"
    r"\band\s+(?:also\s+)?(?:ask|learn|remember|track|store|log|"
    r"keep|verify|check)\b([^,.;?!]{0,80})",
    re.IGNORECASE)


def extract_requirement(text: str, env: Any = None) -> dict | None:
    """A spec fragment targeting the active topic — 'it should learn
    from other AIs', 'Nexus must verify claims'."""
    frame = getattr(env, "semantic", None)
    if frame is not None and frame.speech_act in (
            "question", "hypothetical", "offer", "preference_question"):
        return None
    m = _REQ_RE.search(str(text or ""))
    if not m:
        return None
    value = next((g for g in m.groups() if g), "")
    value = re.sub(r"\s+", " ", value).strip(" .,;'\"")[:140]
    if not value:
        return None
    return {"requirement": value, "confidence": 0.7}


# ---------------------------------------------------------------------------
# Spec edits inside an active requirements session — imperative adds
# ('make the font monospace', 'use a manual save button') and retirements
# ('no auto-save', 'drop the markdown rendering').
# ---------------------------------------------------------------------------

_SPEC_DROP_RE = re.compile(
    r"\b(?:no|drop|remove|skip|cut|lose|forget|don'?t\s+need|"
    r"do\s+not\s+need|without|get\s+rid\s+of)\s+"
    r"(?:the\s+|a\s+|an\s+|that\s+)?([^,.;?!]{2,80})",
    re.IGNORECASE)

_SPEC_ADD_RE = re.compile(
    r"\b(?:make|add|use|keep|include|support|render|show|enable|allow|"
    r"give|have)\s+(?:it\s+|the\s+|a\s+|an\s+|us\s+)?([^,.;?!]{2,80})",
    re.IGNORECASE)

_SPEC_VALUE_TAIL_RE = re.compile(
    r"\s+(?:instead|now|this\s+time|please)\s*$", re.IGNORECASE)


def extract_spec_edits(text: str, env: Any = None) -> list[dict]:
    """Imperative requirement edits — applied only while a spec session
    is active for the topic (the caller gates on an existing req_spec
    entry) so ordinary commands never bank as requirements. One turn can
    retire and add in the same breath: 'no auto-save, use a manual save
    button instead' returns both edits."""
    frame = getattr(env, "semantic", None)
    if frame is not None and frame.speech_act in (
            "question", "hypothetical", "offer", "preference_question"):
        return []
    t = str(text or "")
    edits: list[dict] = []
    for m in _SPEC_DROP_RE.finditer(t):
        val = _SPEC_VALUE_TAIL_RE.sub("", m.group(1)).strip(" .,;'\"")
        if val:
            edits.append({"requirement": val[:140], "retire": True})
    for m in _SPEC_ADD_RE.finditer(t):
        val = _SPEC_VALUE_TAIL_RE.sub("", m.group(1)).strip(" .,;'\"")
        if val:
            edits.append({"requirement": val[:140], "retire": False})
    return edits


# ---------------------------------------------------------------------------
# Attitude — cheap affect signal for response styling.
# ---------------------------------------------------------------------------

_ATTITUDE_RE = re.compile(
    r"\b(?:ugh|argh|damn|frustrating|annoying|annoyed|ridiculous|"
    r"seriously\?*|broken\s+again|still\s+broken|wtf|ffs)\b|"
    r"\b(?:great|perfect|nice|awesome|excellent|love\s+it|works!|"
    r"thank|thanks|appreciated)\b|\b(?:haha|lol|lmao|jk|just\s+kidding)\b",
    re.IGNORECASE)


def detect_attitude(text: str) -> str:
    t = str(text or "")
    if re.search(r"\b(?:haha|lol|lmao|jk|just\s+kidding|joking)\b", t,
                 re.I):
        return "joking"
    if re.search(r"\b(?:ugh|argh|damn|frustrat\w*|annoy\w*|ridiculous|"
                 r"seriously\?|broken\s+again|still\s+broken|wtf|ffs)\b",
                 t, re.I):
        return "frustrated"
    if re.search(r"\b(?:great|perfect|nice|awesome|excellent|love\s+it|"
                 r"thank|thanks|appreciate)\b", t, re.I):
        return "pleased"
    return ""


# ---------------------------------------------------------------------------
# State update — the per-turn fold into ActiveContext.
# ---------------------------------------------------------------------------

def update_state(ctx: Any, env: Any, text: str,
                 *, response_text: str = "") -> None:
    """Fold one user turn into the state graph on `ctx`.

    Order: topic tracking → entity extraction → decision ledger →
    open loops → requirement accumulation → attitude → referent
    bindings. All deterministic; entities keep stable ids so a later
    'her voice' / 'the installer' resolves by alias rather than string
    matching.
    """
    frame = getattr(env, "semantic", None)
    ctx.turn_index = int(getattr(ctx, "turn_index", 0)) + 1
    ctx.last_user_text = str(text or "")[:400]

    # ---- entities -------------------------------------------------------
    mentions = extract_entities(text, frame)
    now = time.time()
    for ent in mentions:
        eid = ent["id"]
        row = ctx.entity_graph.get(eid)
        if row is None:
            row = {"id": eid, "type": ent["type"], "label": ent["label"],
                   "aliases": [], "salience": 0.0, "mentions": 0,
                   "first_ts": now, "last_ts": now}
            ctx.entity_graph[eid] = row
        row["aliases"] = sorted(set(row.get("aliases") or []) |
                                set(ent.get("aliases") or []))
        row["mentions"] = int(row.get("mentions", 0)) + 1
        row["last_ts"] = now
        row["salience"] = round(min(1.0,
            0.35 + 0.10 * row["mentions"]
            + 0.25 * float(ent.get("conf", 0.6))), 3)
    # The envelope's `entities` field was declared but never populated —
    # downstream consumers (inspector, prompt context) read it now.
    if mentions and not getattr(env, "entities", None):
        try:
            env.entities = [e["label"] for e in mentions][:12]
        except Exception:
            pass

    # ---- topic ----------------------------------------------------------
    intent = getattr(env, "primary_intent", "")
    shifted = bool(getattr(env, "topic_shift", ""))
    returning = getattr(env, "followup_of", "") == "topic_return"
    ret_m = _RETURN_RE.match(str(text or ""))
    topic_event = ""

    def _restore(target: str) -> None:
        target = re.sub(r"^(?:the|a|an|my|our|that)\s+", "",
                        str(target or "").strip(" .,;!")).strip()
        hit = None
        for s in ctx.topic_stack:
            if topic_label_match(s.get("label", ""), target) >= 0.5:
                hit = s
                break
        if hit is not None:
            ctx.topic_stack = [s for s in ctx.topic_stack
                               if s is not hit]
            if ctx.active_topic:
                ctx.topic_stack.insert(0, {
                    "label": ctx.active_topic, "ts": now,
                    "status": "paused"})
            ctx.active_topic = hit["label"]
            return
        # 'back to Isabella' — not a stack label but a live entity.
        try:
            from .references import graph_entity_for
            ent = graph_entity_for(ctx, target)
        except Exception:
            ent = None
        if ent is not None:
            lab = str(ent.get("label") or target)
            if ctx.active_topic and \
                    lab.lower() in ctx.active_topic.lower():
                # The richer active label already names this entity —
                # 'kokoro voice preset' covers 'kokoro'; don't park it
                # for a shorter alias.
                return
            if ctx.active_topic:
                ctx.topic_stack.insert(0, {
                    "label": ctx.active_topic, "ts": now,
                    "status": "paused"})
                ctx.topic_stack = ctx.topic_stack[:12]
            ctx.active_topic = lab
        elif target:
            if ctx.topic_stack:
                # "back to the voice" with an unresolvable referent —
                # the parked topic is what the user left, not a new
                # subject. Restore it rather than minting a bogus
                # topic label and stranding the real one.
                hit = ctx.topic_stack.pop(0)
                if ctx.active_topic:
                    ctx.topic_stack.insert(0, {
                        "label": ctx.active_topic, "ts": now,
                        "status": "paused"})
                ctx.active_topic = hit.get("label", ctx.active_topic)
            else:
                ctx.active_topic = target

    if ret_m:
        _restore(next((g for g in ret_m.groups() if g), ""))
        topic_event = "return"
    elif returning:
        _restore(getattr(env, "topic_target", ""))
        topic_event = "return"
    elif shifted:
        if ctx.active_topic:
            ctx.topic_stack = [s for s in ctx.topic_stack
                               if s.get("label") != ctx.active_topic]
            ctx.topic_stack.insert(0, {
                "label": ctx.active_topic, "ts": now,
                "status": "paused",
                "entities": [e["id"] for e in mentions[:6]],
            })
            ctx.topic_stack = ctx.topic_stack[:12]
        ctx.active_topic = getattr(env, "topic_target", "") or \
            derive_topic(text, env, mentions)
        topic_event = "push"
    elif (_RESUME_RE.match(str(text or "")) and ctx.topic_stack
          and ctx.last_topic_event == "push"):
        # 'okay continue' resumes only when the LAST turn displaced the
        # topic — a utility question that kept the active topic means
        # 'continue' just confirms it, never pops the stack.
        hit = ctx.topic_stack.pop(0)
        if ctx.active_topic:
            ctx.topic_stack.insert(0, {
                "label": ctx.active_topic, "ts": now,
                "status": "paused"})
        ctx.active_topic = hit.get("label", ctx.active_topic)
        topic_event = "return"
    elif ctx.active_topic and not is_utility_question(text, env):
        # Implicit shift only when a confident canon/typed entity that
        # doesn't overlap the active topic anchors the turn — a bare
        # "use port 9000" stays a subtopic of the current work.
        anchored = [e for e in mentions
                    if float(e.get("conf", 0)) >= 0.8]
        # Commands/requests about an entity are work inside the current
        # topic, not a shift — 'use qwen3-14b' while debugging llama.cpp
        # is a decision, not a new discussion. Implicit shift only on
        # statement/complaint/question shapes.
        shiftable = getattr(env, "speech_act", "") in (
            "complaint", "assertion", "question", "statement",
            "explanation", "")
        if anchored and shiftable:
            new_label = anchored[0]["label"]
            if topic_label_match(new_label, ctx.active_topic) < 0.3 \
                    and intent in ("tool_action", "coding", "research",
                                   "writing", "image_generation",
                                   "conversation"):
                if ctx.active_topic:
                    ctx.topic_stack.insert(0, {
                        "label": ctx.active_topic, "ts": now,
                        "status": "paused"})
                    ctx.topic_stack = ctx.topic_stack[:12]
                ctx.active_topic = new_label
                topic_event = "push"
    if not ctx.active_topic:
        ctx.active_topic = derive_topic(text, env, mentions)
    ctx.last_topic_event = topic_event

    # ---- decisions ------------------------------------------------------
    dec = extract_decision(text, env)
    if dec:
        subj_key = _slug(dec["subject"] or "")
        if not subj_key:
            # Bare restatement or unlabeled value — inherit the latest
            # active subject whose value SHAPE matches ('9500' → the
            # numeric 'port' slot, not the 'Isabella' voice slot).
            num = bool(re.fullmatch(r"[\d.]+", dec["value"]))
            for d in reversed(ctx.decisions):
                if d.get("status") != "active":
                    continue
                if bool(re.fullmatch(r"[\d.]+",
                                     str(d.get("value", "")))) == num:
                    dec["subject"] = d["subject"]
                    dec["confidence"] = max(dec["confidence"], 0.9)
                    break
            if not dec.get("subject") and mentions:
                # 'use qwen3-14b' — a typed entity inside the value IS
                # the domain; otherwise the strongest typed mention.
                typed = [m for m in mentions
                         if m.get("type") not in ("concept",)]
                in_value = [m for m in typed
                            if m["label"].lower()
                            in dec["value"].lower()]
                pick = (in_value or typed)
                if pick:
                    dec["subject"] = pick[0]["type"]
            if not dec.get("subject"):
                # The turn names an active slot by word — 'forget the
                # port idea, we'll use whatever's free' — so the new
                # value claims that slot instead of floating under
                # 'general' while the stale decision stays active.
                for d in reversed(ctx.decisions):
                    if d.get("status") != "active":
                        continue
                    s = str(d.get("subject") or "")
                    if s and re.search(rf"\b{re.escape(s)}\b", text,
                                       re.IGNORECASE):
                        dec["subject"] = s
                        break
            subj_key = _slug(dec["subject"] or "general")
        for d in ctx.decisions:
            if _slug(d.get("subject", "")) == subj_key and \
                    d.get("status") == "active":
                d["status"] = "superseded"
                d["superseded_at"] = now
                d["superseded_by"] = dec["value"]
        ctx.decisions.append({
            "id": f"d{ctx.turn_index}",
            "subject": dec["subject"], "value": dec["value"],
            "ts": now, "turn": ctx.turn_index,
            "confidence": dec["confidence"], "status": "active",
        })
        ctx.decisions = ctx.decisions[-40:]

    # ---- open loops -----------------------------------------------------
    if loop_close_match(text):
        for loop in ctx.open_loops:
            if loop.get("status") == "open":
                loop["status"] = "resolved"
                loop["resolved_at"] = now
    else:
        loop = extract_open_loop(text, env)
        # A complaint about a live entity is unresolved work — 'the
        # installer failed on the dll copy' stays open until confirmed.
        if loop is None and getattr(frame, "speech_act", "") == \
                "complaint" and mentions:
            loop = {"text": str(text or "")[:160], "confidence": 0.6}
        if loop:
            ctx.open_loops.append({
                "id": f"loop-{ctx.turn_index}",
                "text": loop["text"], "entity":
                    (mentions[0]["id"] if mentions else ""),
                "status": "open", "ts": now,
                "priority": 0.6,
                "next": "",
            })
            ctx.open_loops = ctx.open_loops[-20:]

    # ---- requirement accumulation ---------------------------------------
    req = extract_requirement(text, env)
    topic_slug = _slug(ctx.active_topic) if ctx.active_topic else ""
    if req:
        # No active topic yet — the spec still seeds under a default
        # bucket; a topic-less opener like "let's spec a feature" must
        # not drop the first requirement.
        spec = ctx.req_spec.setdefault(topic_slug or "_default", {})
        spec.setdefault("requirements", []).append(req["requirement"])
        spec["requirements"] = spec["requirements"][-24:]
        spec["updated_at"] = now
    elif ctx.req_spec:
        # Imperative spec edits — gated on a spec session so ordinary
        # commands never bank as requirements. Targets the active topic's
        # spec; topic labels drift mid-session ('make the font monospace'
        # retitles the topic), so fall back to the spec touched within
        # the last hour — a stale spec from an old session stays sealed.
        spec = ctx.req_spec.get(topic_slug)
        if spec is None:
            cand = max(ctx.req_spec.values(),
                       key=lambda s: float(s.get("updated_at") or 0.0))
            if now - float(cand.get("updated_at") or 0.0) < 3600:
                spec = cand
        if spec is not None:
            reqs = spec.setdefault("requirements", [])
            retired = spec.setdefault("retired", [])
            changed = False
            for edit in extract_spec_edits(text, env):
                val = str(edit.get("requirement") or "")
                if not val:
                    continue
                if edit.get("retire"):
                    q = set(re.findall(r"[a-z0-9]+", val.lower()))
                    keep = []
                    for r in reqs:
                        rt = set(re.findall(r"[a-z0-9]+", r.lower()))
                        if q and q <= rt:
                            retired.append(r)
                        else:
                            keep.append(r)
                    if len(keep) != len(reqs):
                        spec["requirements"] = keep
                        reqs = spec["requirements"]
                        spec["retired"] = retired[-24:]
                        changed = True
                else:
                    reqs.append(val)
                    spec["requirements"] = reqs[-24:]
                    changed = True
            if changed:
                spec["updated_at"] = now

    # ---- attitude -------------------------------------------------------
    att = detect_attitude(text)
    if att:
        ctx.speaker_attitude = att

    # ---- goal -----------------------------------------------------------
    if not ctx.current_goal and ctx.active_topic:
        # First substantive topic seeds the working goal.
        if intent in ("coding", "research", "tool_action",
                      "image_generation"):
            ctx.current_goal = ctx.active_topic[:120]

    # ---- referent bindings ----------------------------------------------
    refs = getattr(env, "references", {}) or {}
    for term, label in refs.items():
        # store by entity id when the label matches a known entity
        bound = ""
        for eid, row in ctx.entity_graph.items():
            if row.get("label", "").lower() == str(label).lower() or \
                    str(label).lower() in [
                        a.lower() for a in row.get("aliases") or []]:
                bound = eid
                break
        ctx.referents[str(term).lower()] = bound or str(label)
    ctx.referents = dict(list(ctx.referents.items())[-16:])

    # ---- compaction ------------------------------------------------------
    if ctx.turn_index % 10 == 0:
        compact_state(ctx)


def forget_from_state(ctx: Any, forgotten_text: str) -> int:
    """Propagate a user-controlled forget into the state graph — remove
    entities, decisions, referents and open loops derived solely from
    the forgotten fact. Returns the number of state entries removed."""
    terms = {w for w in re.findall(r"[a-z0-9]+",
                                   str(forgotten_text or "").lower())
             if w not in _WORD_STOP and len(w) > 2}
    if not terms:
        return 0
    removed = 0
    graph = getattr(ctx, "entity_graph", None) or {}
    drop_ids = []
    for eid, row in graph.items():
        labels = [str(row.get("label", "")),
                  *[str(a) for a in (row.get("aliases") or [])]]
        lt = set()
        for lab in labels:
            lt |= {w for w in re.findall(r"[a-z0-9]+", lab.lower())}
        if terms & lt:
            drop_ids.append(eid)
    for eid in drop_ids:
        graph.pop(eid, None)
        removed += 1
    kept_decisions = []
    for d in getattr(ctx, "decisions", None) or []:
        dt = set(re.findall(r"[a-z0-9]+",
                            (str(d.get("subject", "")) + " "
                             + str(d.get("value", ""))).lower()))
        if terms & dt:
            removed += 1
        else:
            kept_decisions.append(d)
    ctx.decisions = kept_decisions
    kept_loops = []
    for l in getattr(ctx, "open_loops", None) or []:
        lt = set(re.findall(r"[a-z0-9]+",
                            str(l.get("text", "")).lower()))
        if terms & lt:
            removed += 1
        else:
            kept_loops.append(l)
    ctx.open_loops = kept_loops
    refs = getattr(ctx, "referents", None) or {}
    for term in list(refs):
        if refs[term] in drop_ids:
            refs.pop(term, None)
            removed += 1
    return removed


def compact_state(ctx: Any, *, now: float | None = None) -> None:
    """Bounded growth — drop the least salient entities, prune resolved
    loops older than 48h, cap stacks/specs. Runs every 10 turns so a
    200-turn session can't grow the row unboundedly."""
    import time as _t
    now = _t.time() if now is None else now
    graph = getattr(ctx, "entity_graph", None) or {}
    if len(graph) > 24:
        ranked = sorted(
            graph.values(),
            key=lambda r: (float(r.get("salience", 0)),
                           float(r.get("last_ts") or 0)))
        keep = {r["id"] for r in ranked[-24:]}
        ctx.entity_graph = {k: v for k, v in graph.items()
                            if k in keep}
    loops = getattr(ctx, "open_loops", None) or []
    ctx.open_loops = [
        l for l in loops
        if l.get("status") == "open"
        or (now - float(l.get("resolved_at") or l.get("ts") or now))
        < 48 * 3600][-20:]
    spec = getattr(ctx, "req_spec", None) or {}
    if len(spec) > 8:
        keep = sorted(spec.items(),
                      key=lambda kv: float(kv[1].get("updated_at") or 0),
                      reverse=True)[:8]
        ctx.req_spec = dict(keep)
    stack = getattr(ctx, "topic_stack", None) or []
    ctx.topic_stack = stack[:12]
