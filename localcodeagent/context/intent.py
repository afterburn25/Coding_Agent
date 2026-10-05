"""Intent envelope — the structured representation of one user turn.

Pipeline order (per the routing contract):

    newest explicit user instruction
      → corrections / pending clarifications
      → explicit high-confidence actions (image, tool, git, file, research)
      → contextual follow-ups against active state
      → narrow identity/utility questions
      → conversation

A HIGH-confidence explicit intent is authoritative — no second, weaker
classifier may veto it downstream.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

IMAGE_GENERATION = "image_generation"
IMAGE_EDIT = "image_edit"
IMAGE_FOLLOWUP = "image_followup"
TOOL_ACTION = "tool_action"
GIT_ACTION = "git_action"
FILE_EDIT = "file_edit"
CODING = "coding"
RESEARCH = "research"
WRITING = "writing"
QUESTION = "question"
IDENTITY_QUERY = "identity_query"
CLARIFICATION_RESPONSE = "clarification_response"
CORRECTION = "correction"
FEEDBACK_SIGNAL = "feedback_signal"
CONVERSATION = "conversation"

# Intents that must never be answered by a canned utility/identity reply —
# an explicit action request always outranks small talk.
ACTION_INTENTS = {
    IMAGE_GENERATION, IMAGE_EDIT, IMAGE_FOLLOWUP, TOOL_ACTION, GIT_ACTION,
    FILE_EDIT, CODING, RESEARCH, WRITING,
}

# Confidence at which an explicit intent routes directly — no second gate.
DIRECT_CONFIDENCE = 0.8


@dataclass
class IntentEnvelope:
    """One user turn, structured. `to_trace()` is the safe developer
    surface: routing evidence only, never chain-of-thought."""

    primary_intent: str = CONVERSATION
    secondary_intents: list[dict[str, Any]] = field(default_factory=list)
    requested_action: str = ""
    subject: str = ""
    objects: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    attributes: list[str] = field(default_factory=list)
    modifiers: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    negative_constraints: list[str] = field(default_factory=list)
    preserve_constraints: list[str] = field(default_factory=list)
    references: dict[str, str] = field(default_factory=dict)
    source_images: list[str] = field(default_factory=list)
    target_files: list[str] = field(default_factory=list)
    target_project: str = ""
    temporal_context: str = ""
    conversation_references: list[str] = field(default_factory=list)
    followup_of: str = ""
    correction_of: str = ""
    correction_value: str = ""
    continuation_of: str = ""
    needs_clarification: str = ""
    confidence: float = 0.0
    evidence: list[str] = field(default_factory=list)
    output_type: str = ""
    followup_prompt: str = ""
    compound: bool = False

    def direct_image(self) -> bool:
        """HIGH-confidence direct image intent — authoritative for routing."""
        return (
            self.primary_intent in {IMAGE_GENERATION, IMAGE_EDIT}
            and self.confidence >= DIRECT_CONFIDENCE
        )

    def suppresses_canned(self) -> bool:
        """An explicit action intent at routing confidence can never be
        answered by a builtin small-talk/identity reply."""
        return (
            self.primary_intent in ACTION_INTENTS
            and self.confidence >= 0.6
        )

    def to_trace(self) -> dict[str, Any]:
        """Structured routing evidence — debuggable without reasoning."""
        return {
            "intent": self.primary_intent,
            "action": self.requested_action,
            "subject": self.subject[:200],
            "references": self.references,
            "constraints": self.negative_constraints[:8],
            "confidence": round(self.confidence, 3),
            "correction_of": self.correction_of,
            "continuation_of": self.continuation_of,
            "compound": self.compound,
            "evidence": self.evidence[:8],
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "primary_intent": self.primary_intent,
            "requested_action": self.requested_action,
            "subject": self.subject,
            "attributes": self.attributes,
            "references": self.references,
            "confidence": self.confidence,
        }


# --------------------------------------------------------------------------
# Shared text scaffolding
# --------------------------------------------------------------------------

def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip()).strip()


def _low(text: str) -> str:
    return _norm(text).lower()


# Conversational scaffolding that pads a prompt without describing the
# image — image models do better with the bare descriptive phrase.
# Kept here so intent detection and prompt refinement share one grammar.
_SCAFFOLD_PIECES = [
    r"hey(?:\s+nexus)?[,!]?",
    r"okay[,!]?",
    r"please",
    r"(?:can|could|would|will)\s+you",
    r"(?:can|could)\s+i",
    r"i\s+(?:want|need|would\s+like|'?d\s+like|wanna|really\s+want)\s+"
    r"(?:you\s+to\s+|to\s+see\s+)?",
    r"go\s+ahead\s+and",
    r"let\s+me\s+see",
    r"(?:may|can|could)\s+i\s+see",
    r"i'?d\s+like\s+to\s+see",
    r"i\s+want\s+to\s+see",
    r"show\s+me",
    r"(?:generate|create|make|draw|paint|render|produce|give\s+me|"
    r"whip\s+up|design|depict|visualize|imagine|illustrate|sketch)\s+"
    r"(?:me\s+)?",
]
_SCAFFOLD_OBJECT_PIECES = [
    r"(?:an?|the|some|another|a\s+few|one)",
    r"(?:images?|pictures?|photos?|pics?|photographs?|portraits?|"
    r"illustrations?|drawings?|renders?|wallpapers?|paintings?|"
    r"sketches?|artworks?|depictions?|visualizations?|selfies?|"
    r"avatars?|posters?|banners?|logos?|icons?|pics)",
    r"(?:of|showing|depicting|featuring|with|where|that\s+shows?|of\s+a|"
    r"of\s+an)",
]
_SCAFFOLD_RE = re.compile(
    r"^(?:" + "|".join(_SCAFFOLD_PIECES) + r"|"
    + r"(?:" + "|".join(_SCAFFOLD_PIECES) + r")\s*(?:"
    + "|".join(_SCAFFOLD_OBJECT_PIECES) + r")"
    + r")\s+",
    re.IGNORECASE)


def strip_image_scaffold(text: str) -> str:
    """Strip request scaffolding so only descriptive content survives —
    'please show me a picture of a red cat' → 'a red cat'."""
    t = _norm(text)
    prev = None
    while prev != t:
        prev = t
        t = _SCAFFOLD_RE.sub("", t, count=1).strip(" ,.:;")
    return t or _norm(text)


# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------

_VISUAL_NOUNS = (
    "image", "picture", "photo", "photograph", "pic", "portrait",
    "illustration", "drawing", "artwork", "render", "wallpaper",
    "painting", "sketch", "depiction", "visualization", "selfie",
    "avatar", "poster", "banner", "logo", "icon", "concept art",
    "nude", "hentai",
)
_VISUAL_NOUN_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(n) for n in _VISUAL_NOUNS) + r")s?\b",
    re.IGNORECASE)

# Adjectives that imply a visual artifact even without a visual noun —
# "let me see a photorealistic woman in red" is a picture request.
_VISUAL_ADJS = (
    "photorealistic", "hyperrealistic", "realistic", "anime", "cartoon",
    "cel-shaded", "cel shaded", "painted", "hand-drawn", "hand drawn",
    "3d rendered", "cgi", "watercolor", "oil painting", "pixel art",
    "low-poly", "low poly", "nude", "naked", "topless", "nsfw",
    "stylized", "surreal", "digital art", "concept", "fantasy art",
)
_VISUAL_ADJ_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(a) for a in _VISUAL_ADJS) + r")\b",
    re.IGNORECASE)

# Objects whose presence means "show me X" is NOT an image request.
_NON_IMAGE_OUTPUTS = (
    "code", "function", "class", "component", "script", "report",
    "email", "essay", "document", "spreadsheet", "presentation",
    "website", "webpage", "api", "query", "command", "uuid", "json",
    "log", "logs", "diff", "output", "error", "traceback", "stack trace",
    "test result", "test results", "build", "file", "files", "folder",
    "directory", "repo status", "commit", "commits", "branch", "branches",
    "pull request", "pr", "ci", "workflow", "run", "runs", "config",
    "settings", "permissions", "status", "metrics", "memory", "history",
    "conversation", "chat", "messages", "notification", "notifications",
    "list", "table", "spreadsheet", "chart", "graph of", "terminal",
    "console", "shell", "snippet", "regex", "documentation", "docs",
    "readme", "manifest", "changelog", "diff", "patch",
)
_NON_IMAGE_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(n) for n in _NON_IMAGE_OUTPUTS) + r")\b",
    re.IGNORECASE)

# "show me / let me see / can I see" request verbs — need a visual object.
_SHOW_VERB_RE = re.compile(
    r"^(?:please\s+)?(?:hey(?:\s+\w+)?[,!]?\s+)?"
    r"(?:(?:can|could|would|may)\s+i\s+|i\s+(?:wanna|want\s+to|"
    r"would\s+like\s+to|'?d\s+like\s+to|need\s+to)\s+|let\s+me\s+|"
    r"(?:can|could|would|will)\s+you\s+(?:please\s+)?|"
    r"i\s+(?:want|need|would\s+like|'?d\s+like)\s+(?:you\s+to\s+)?)?"
    r"(?:show\s+me|let\s+me\s+see|give\s+me|get\s+me|bring\s+me|"
    r"fetch\s+me|pull\s+up|display)\b",
    re.IGNORECASE)

_GENERATE_VERB_RE = re.compile(
    r"\b(?:generate|create|make|render|produce|draw|paint|illustrate|"
    r"sketch|design|depict|visualize|imagine|dream\s+up|whip\s+up|"
    r"come\s+up\s+with|conjure|spawn|build\s+me\s+a\s+picture|"
    r"craft|compose)\b",
    re.IGNORECASE)

# Image-op verbs on an existing artifact — edit intent (source image).
_IMAGE_OP_RE = re.compile(
    r"\b(?:recreate|re-?create|remake|redraw|re-?draw|reimagine|rework|"
    r"redo|regenerate|retry|edit|change|modify|alter|enhance|fix|"
    r"retouch|touch\s*up|transform|improve|restore|upscale|clean\s*up|"
    r"remove|replace|crop|extend|outpaint|inpaint|vary|variation)\b"
    r"[^.?!]{0,80}\b(?:this|that|the|my|attached|uploaded|existing|"
    r"source|last|previous|same)?\s*"
    r"(?:images?|pictures?|photos?|pics?|background)\b",
    re.IGNORECASE)

# Pure image follow-up phrasings — meaningful only against active image
# context ("make her blonde", "full body", "zoom out", "try again").
_FOLLOWUP_RE = re.compile(
    r"^(?:please\s+)?(?:hey(?:\s+\w+)?[,!]?\s+)?(?:"
    r"try\s+again|retry|redo|regenerate|another\s+one|one\s+more|"
    r"do\s+it|do\s+that|go\s+ahead|"
    r"(?:more|less)\s+\w+|"
    r"make\s+(?:her|him|them|it|the|his|their|its)\b|"
    r"give\s+(?:her|him|them|it)\b|"
    r"put\s+(?:her|him|them|it)\b|"
    r"keep\s+(?:the|her|his|their|its|it)\b|"
    r"same\s+\w+|"
    r"remove\s+(?:the|her|his|their|its|it)\b|"
    r"(?:full|whole|entire|complete)\s+body|zoom\s+(?:out|in)|"
    r"crop|wider|closer|tighter|"
    r"(?:longer|shorter|bigger|smaller|darker|lighter|brighter|"
    r"more\s+realistic|less\s+realistic)\b|"
    r"now\s+(?:make|show|add|remove)\b|"
    r"add\s+(?:a|an|the|some|more|her|him)\b)",
    re.IGNORECASE)

_CORRECTION_RE = re.compile(
    r"^(?:(?:no|nope|nah|wrong)[,.\s]+)"
    r"(.+)|"
    r"^(?:i\s+meant|i\s+said|actually)\b[,]?\s*(.+)",
    re.IGNORECASE)

_FEEDBACK_RE = re.compile(
    r"\b(?:that'?s\s+not\s+what\s+i\s+(?:meant|asked|wanted|said)|"
    r"you\s+(?:lost|ignored|missed|forgot)\s+(?:the\s+)?(?:context|"
    r"what\s+i|my)|stop\s+repeating|you\s+keep\s+(?:saying|repeating|"
    r"forgetting)|wrong\s+(?:image|one|thing)|not\s+what\s+i\s+asked)",
    re.IGNORECASE)

_COMPOUND_SPLIT_RE = re.compile(
    r"\s*(?:,\s*(?:and\s+then|then|and\s+also)|;\s*|\.\s+(?:then|and\s+then)|"
    r"\s+and\s+then\s+|\s+then\s+)\s*",
    re.IGNORECASE)


def detect_image_intent(text: str) -> tuple[bool, float, list[str]]:
    """Natural-language image-request detection.

    Returns (matched, confidence, evidence). Object/output semantics, not
    keyword matching: "show me the code" is not an image request; "show me
    a picture of a dragon" is.
    """
    t = _low(text)
    if not t:
        return False, 0.0, []
    evidence: list[str] = []

    # Inpainting/outpainting argument-heavy ops stay tool-routed.
    if any(x in t for x in ("inpaint", "outpaint")) and "mask" in t:
        return False, 0.0, []

    visual_noun = bool(_VISUAL_NOUN_RE.search(t))
    visual_adj = bool(_VISUAL_ADJ_RE.search(t))
    non_image = bool(_NON_IMAGE_RE.search(t))

    # Explicit edit ops on an existing artifact — always image intent
    # (resolved to edit vs generate by attachment/context downstream).
    if _IMAGE_OP_RE.search(t):
        evidence.append("image operation verb + visual noun")
        return True, 0.9, evidence
    if any(x in t for x in ("remove background", "replace background",
                            "variation of")):
        evidence.append("explicit image operation")
        return True, 0.9, evidence

    show = bool(_SHOW_VERB_RE.search(t))
    gen = bool(_GENERATE_VERB_RE.search(t))

    # Veto: a non-visual requested output object kills image intent even
    # beside a visual verb — "make a picture" vs "generate a report".
    if non_image and not visual_noun and not visual_adj:
        return False, 0.0, []

    if show and visual_noun:
        evidence.append("show/see verb + visual noun")
        return True, 0.95, evidence
    if gen and visual_noun:
        evidence.append("generation verb + visual noun")
        return True, 0.95, evidence
    if show and visual_adj and not non_image:
        # "let me see a photorealistic woman in a red dress"
        evidence.append("show/see verb + visual descriptor")
        return True, 0.85, evidence
    if visual_noun and not non_image:
        # Bare "a picture of X" / "image of X" as the whole request —
        # in a chat box that IS the request.
        if re.match(
            r"^(?:an?\s+|the\s+|some\s+|another\s+)?"
            r"(?:picture|image|photo|pic|portrait|illustration|drawing|"
            r"artwork|render|wallpaper|painting|sketch|depiction|"
            r"visualization|selfie|nude|hentai)s?\s+of\b", t):
            evidence.append("bare visual noun phrase as request")
            return True, 0.85, evidence
    if visual_adj and (show or gen) and not non_image:
        evidence.append("visual verb + descriptor")
        return True, 0.8, evidence
    return False, 0.0, evidence


def _extract_negative_constraints(t: str) -> tuple[list[str], list[str]]:
    """Pull negation/preserve clauses for the envelope — 'no hat',
    'without glasses', 'don't change the face', 'keep the background'."""
    negatives: list[str] = []
    preserves: list[str] = []
    for m in re.finditer(
        r"\b(?:no\s+|without\s+|don'?t\s+(?:change|touch|alter|include|"
        r"add)|do\s+not\s+(?:change|touch|alter|include|add)|"
        r"not\s+the\s+|except\s+(?:for\s+)?|excluding\s+)"
        r"([^,.;?!]{2,60})", t, re.IGNORECASE):
        clause = m.group(1).strip()
        if clause:
            negatives.append(clause)
    for m in re.finditer(
        r"\b(?:keep\s+(?:the\s+|her\s+|his\s+|their\s+|its\s+)?|"
        r"preserve\s+(?:the\s+)?|same\s+|only\s+change\s+(?:the\s+)?)"
        r"([^,.;?!]{2,60})", t, re.IGNORECASE):
        clause = m.group(0).strip()
        if clause:
            preserves.append(clause)
    return negatives, preserves


def _compound_parts(t: str) -> list[str]:
    parts = [p.strip(" ,.;") for p in _COMPOUND_SPLIT_RE.split(t)]
    return [p for p in parts if len(p) > 3]


def understand_turn(text: str, *, active: Any = None,
                    has_attachments: bool = False) -> IntentEnvelope:
    """Produce the IntentEnvelope for one user turn.

    `active` is the conversation's ActiveContext (or None). Detection
    precedence mirrors the contract: corrections and pending
    clarifications first, then explicit actions, contextual follow-ups,
    narrow utility questions, compound splitting, conversation.
    """
    env = IntentEnvelope()
    raw = _norm(text)
    t = _low(raw)
    if not t:
        env.confidence = 1.0
        env.evidence.append("empty")
        return env

    image_ctx = bool(
        active is not None
        and getattr(active, "image_active", lambda **k: False)())

    # --- 0. Routing-failure feedback — never treated as a new task.
    if _FEEDBACK_RE.search(t):
        env.primary_intent = FEEDBACK_SIGNAL
        env.confidence = 0.9
        env.evidence.append("context-failure feedback phrase")
        env.followup_of = "previous_turn"
        if re.search(r"context", t):
            env.correction_of = "lost_context"
        elif re.search(r"image|picture|one", t):
            env.correction_of = "wrong_artifact"
        else:
            env.correction_of = "missed_intent"
        return env

    # --- 1. Corrections override older context outright.
    m = _CORRECTION_RE.match(t)
    if m and not re.match(r"^(?:no|nope|nah)\s*[!?.]*$", t):
        repl = (m.group(1) or m.group(2) or "").strip(" ,.;")
        env.correction_of = "previous_attribute"
        env.correction_value = repl
        env.evidence.append("correction marker")
        if image_ctx:
            env.primary_intent = IMAGE_FOLLOWUP
            env.requested_action = "modify"
            env.subject = getattr(active, "active_image_subject", "")
            env.followup_of = "active_image"
            env.followup_prompt = repl
            env.confidence = 0.9
            return env
        env.primary_intent = CORRECTION
        env.confidence = 0.85
        return env

    # --- 2. Pending clarification — answer continues the SAME intent.
    pending = getattr(active, "pending_clarification", "") if active else ""
    if pending and re.match(
        r"^(?:yes|yeah|yep|yup|sure|ok(?:ay)?|confirm|confirmed|"
        r"she'?s?\s+(?:an?\s+)?adult|he'?s?\s+(?:an?\s+)?adult|"
        r"they'?re?\s+(?:an?\s+)?adults?|adult|18\+|21\+|"
        r"of\s+legal\s+age|go\s+ahead|proceed|do\s+it)\b", t):
        env.primary_intent = CLARIFICATION_RESPONSE
        env.continuation_of = getattr(active, "pending_intent", "") or pending
        env.confidence = 0.9
        env.evidence.append("resolves pending clarification")
        env.references["clarification"] = pending
        # Carry the parked request forward — the user must never have to
        # repeat the original instruction after a clarification.
        env.followup_prompt = getattr(active, "pending_prompt", "") or ""
        env.subject = getattr(active, "pending_subject", "") or ""
        return env

    # --- 3. Explicit image intent — authoritative when high-confidence.
    matched, conf, evidence = detect_image_intent(t)
    if matched:
        env.primary_intent = (
            IMAGE_EDIT if (has_attachments or _IMAGE_OP_RE.search(t))
            else IMAGE_GENERATION)
        env.requested_action = "create" if not has_attachments else "edit"
        env.subject = strip_image_scaffold(raw)
        env.output_type = "image"
        env.confidence = conf
        env.evidence = evidence
        env.attributes = [a for a in _VISUAL_ADJS
                          if re.search(rf"\b{re.escape(a)}\b", t)]
        env.negative_constraints, env.preserve_constraints = (
            _extract_negative_constraints(t))
        if image_ctx:
            env.references["prior_subject"] = getattr(
                active, "active_image_subject", "")
        # Explicit adult terms without an adulthood marker keep the image
        # intent — the policy layer asks the narrow question; routing
        # never erases the request.
        explicit = bool(re.search(
            r"\b(?:nude|naked|topless|nsfw|explicit|sex|erotic)\b", t))
        adult_marker = bool(re.search(
            r"\b(?:adult|grown|mature|18\+|21\+|of\s+age|legal\s+age)\b",
            t))
        if explicit and not adult_marker:
            env.needs_clarification = "adult_subject"
        return env

    # --- 4. Contextual image follow-up — fragment + active image task.
    if image_ctx and _FOLLOWUP_RE.search(t):
        env.primary_intent = IMAGE_FOLLOWUP
        env.requested_action = "modify"
        env.subject = getattr(active, "active_image_subject", "")
        env.followup_of = "active_image"
        env.confidence = 0.85
        env.evidence.append("image follow-up phrase + active image task")
        env.negative_constraints, env.preserve_constraints = (
            _extract_negative_constraints(t))
        env.followup_prompt = t
        return env

    # --- 5. Compound multi-intent — each clause keeps its own intent.
    parts = _compound_parts(t)
    if len(parts) > 1:
        env.compound = True
        subs = []
        for part in parts:
            sub = understand_turn(part, active=None,
                                  has_attachments=has_attachments)
            subs.append({
                "text": part,
                "intent": sub.primary_intent,
                "action": sub.requested_action,
                "confidence": sub.confidence,
            })
        env.secondary_intents = subs
        # The FIRST clause's intent drives the immediate route; the rest
        # are retained for sequencing instead of silently dropped.
        if subs and subs[0]["intent"] not in {CONVERSATION, QUESTION}:
            env.primary_intent = subs[0]["intent"]
            env.confidence = subs[0]["confidence"]
            env.evidence.append("compound request — clauses retained")
            env.requested_action = subs[0]["action"]
            return env

    # --- 6. Tool/git/file actions.
    if re.search(
        r"\b(?:git\s+)?(?:push|pull|commit|merge|rebase|checkout|"
        r"branch|clone|fetch|stash)\b|\bcreate\s+(?:a\s+)?(?:pull\s+"
        r"request|pr|issue|branch)\b|\bpull\s+request\b", t):
        env.primary_intent = GIT_ACTION
        env.requested_action = "git"
        env.confidence = 0.85
        env.evidence.append("git action keyword")
        return env
    if re.search(
        r"^(?:run|open|delete|move|rename|execute|list|restart|stop|"
        r"start|kill|install|update|deploy)\s+", t):
        env.primary_intent = TOOL_ACTION
        env.requested_action = t.split()[0]
        env.confidence = 0.8
        env.evidence.append("imperative tool verb")
        return env
    if re.search(r"\b(?:check|show|list|what'?s)\b.{0,40}"
                 r"\b(?:branch|build|ci|actions?|tests?|status|logs?|"
                 r"errors?|failures?|diff|github|repository)\b", t):
        env.primary_intent = TOOL_ACTION
        env.requested_action = "inspect"
        env.confidence = 0.7
        env.evidence.append("project-state inspection request")
        return env

    # --- 7. Research / coding / writing (secondary-confidence lanes).
    if any(x in t for x in ("search the web", "look up", "research",
                            "latest", "current version", "today's news",
                            "source this", "find out", "look into")):
        env.primary_intent = RESEARCH
        env.requested_action = "research"
        env.confidence = 0.75
        env.evidence.append("research keyword")
        return env
    if any(x in t for x in ("build", "implement", "debug", "fix the",
                            "refactor", "write a function", "write code",
                            "repository", "webpage", "api endpoint")):
        env.primary_intent = CODING
        env.requested_action = "code"
        env.confidence = 0.7
        env.evidence.append("coding keyword")
        return env
    if any(x in t for x in ("write an email", "rewrite", "draft",
                            "write a story", "poem", "caption", "essay",
                            "write a script")):
        env.primary_intent = WRITING
        env.requested_action = "write"
        env.confidence = 0.7
        env.evidence.append("writing keyword")
        return env

    # --- 8. Narrow identity/capability questions — interrogative form
    # only. A request containing identity WORDS ("picture of your
    # creator") is not an identity question.
    if re.match(
        r"^(?:who|what|when|where|why|how)\b.*\?"
        r"|^(?:who\s+(?:are|made|created|built|designed)|what\s+are|"
        r"when\s+(?:is|was|were)|how\s+old)\b", t):
        if re.search(r"\b(?:you|your|nexus)\b", t) and re.search(
            r"\b(?:birthday|born|age|old|father|creator|dad|made|"
            r"created|built|name)\b", t):
            env.primary_intent = IDENTITY_QUERY
            env.requested_action = "answer"
            env.confidence = 0.85
            env.evidence.append("interrogative self-reference")
            return env
        env.primary_intent = QUESTION
        env.confidence = 0.6
        env.evidence.append("interrogative form")
        return env
    if re.match(r"^(?:what|how|why|when|where|who|which|is|are|can|"
                r"could|do|does|did|should|would)\b", t) and \
            t.rstrip("!?.").endswith("?") or t.endswith("?"):
        env.primary_intent = QUESTION
        env.confidence = 0.55
        env.evidence.append("question form")
        return env

    # --- 9. Bare visual noun phrase as the whole turn.
    # (covered by detect_image_intent's bare-noun branch; reaching here
    # means no intent matched at all)
    env.primary_intent = CONVERSATION
    env.confidence = 0.4
    env.evidence.append("no explicit action intent")
    return env
