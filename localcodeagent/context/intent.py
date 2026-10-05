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
GITHUB_STATUS = "github_status"
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
    GITHUB_STATUS, FILE_EDIT, CODING, RESEARCH, WRITING,
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
    # Universal-turn fields: topic moves, comparisons, ordinals,
    # conditionals, alternatives, and honest ambiguity markers.
    topic_shift: bool = False
    topic_target: str = ""
    comparison: bool = False
    comparison_targets: list[str] = field(default_factory=list)
    ordinal_reference: int | None = None
    conditionals: list[dict[str, str]] = field(default_factory=list)
    alternatives: list[str] = field(default_factory=list)
    ambiguity: list[str] = field(default_factory=list)
    implicit: bool = False

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
            "topic_shift": self.topic_shift,
            "topic_target": self.topic_target[:120],
            "comparison": self.comparison,
            "ordinal": self.ordinal_reference,
            "conditionals": self.conditionals[:4],
            "ambiguity": self.ambiguity[:4],
            "temporal": self.temporal_context[:80],
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
    r"see",
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
_SCAFFOLD_REQ_RE = re.compile(
    r"^(?:" + "|".join(_SCAFFOLD_PIECES) + r")\b\s*",
    re.IGNORECASE)
_SCAFFOLD_OBJ_RE = re.compile(
    r"^(?:(?:an?|the|some|another|a\s+few|one)\s+)?"
    r"(?:images?|pictures?|photos?|pics?|photographs?|portraits?|"
    r"illustrations?|drawings?|renders?|wallpapers?|paintings?|"
    r"sketches?|artworks?|depictions?|visualizations?|selfies?|"
    r"avatars?|posters?|banners?|logos?|icons?|concept\s+arts?)\s+"
    r"(?:of|showing|depicting|featuring|that\s+shows?)\s+",
    re.IGNORECASE)


def strip_image_scaffold(text: str) -> str:
    """Strip request scaffolding so only descriptive content survives —
    'please show me a picture of a red cat' → 'a red cat'."""
    t = _norm(text)
    prev = None
    while prev != t:
        prev = t
        t = _SCAFFOLD_REQ_RE.sub("", t, count=1).strip(" ,.:;")
        t = _SCAFFOLD_OBJ_RE.sub("", t, count=1).strip(" ,.:;")
    # "what a castle would look like" → "a castle" — hypothetical
    # phrasing describes the subject, not the prompt.
    t = re.sub(r"^what\s+(.+?)\s+would\s+look\s+like\b.*$", r"\1",
               t, flags=re.IGNORECASE).strip(" ,.:;?")
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

# Person/creature subjects imply a visual artifact under a generation
# verb — "generate an adult woman in a red dress" needs no visual noun.
_PERSON_SUBJECT_RE = re.compile(
    r"\b(?:adult\s+)?(?:woman|women|man|men|girl|boy|person|people|"
    r"character|portrait\s+of|angel|demon|warrior|knight|witch|elf|"
    r"princess|prince|queen|king|goddess|god|cyborg|robot|alien|"
    r"vampire|mermaid|samurai|ninja|pirate|soldier|scientist|"
    r"superhero|villain|model|actress|actor|singer|dancer|cat|dog|"
    r"dragon|creature|monster|beast|wolf|fox|horse|lion|tiger|"
    r"bird|phoenix|unicorn)\b",
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
    "readme", "manifest", "changelog", "diff", "patch", "problem",
    "issue", "menu", "result", "answer", "question", "weather",
    "price", "definition", "meaning", "reason", "difference",
    "options", "choice", "score", "news", "time", "date", "repo",
    "repos", "repositories",
)
_NON_IMAGE_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(n) for n in _NON_IMAGE_OUTPUTS) + r")\b",
    re.IGNORECASE)

# "show me / let me see / can I see" request verbs — need a visual object.
# Greeting/politeness prefixes may appear in any order: "hey nexus,
# please show me…" and "please, hey nexus, show me…" are equivalent.
_SHOW_VERB_RE = re.compile(
    r"^(?:(?:hey(?:\s+\w+)?[,!]?\s+)|(?:please\s+)|(?:okay[,!]?\s+))*"
    r"(?:(?:can|could|would|may)\s+i\s+|i\s+(?:wanna|want\s+to|"
    r"would\s+like\s+to|'?d\s+like\s+to|need\s+to)\s+|let\s+me\s+|"
    r"(?:can|could|would|will)\s+you\s+(?:please\s+)?|"
    r"i\s+(?:want|need|would\s+like|'?d\s+like)\s+(?:you\s+to\s+)?)?"
    r"(?:show\s+me|let\s+me\s+see|give\s+me|get\s+me|bring\s+me|"
    r"fetch\s+me|pull\s+up|display|see)\b",
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

_ACTION_AFTER_COMMA = (
    r"(?:save|put|copy|move|send|write|run|push|commit|deploy|open|"
    r"add|remove|delete|rename|create|make|show|list|check|fix|update|"
    r"install|upload|download|attach|insert|print|export|import|post|"
    r"share|store|place|set|schedule|email|notify|tell|verify|test|"
    r"build|compile|document|commit|apply|use|try|upload|sync|clean|"
    r"rerun|retest|merge|publish)"
)
_COMPOUND_SPLIT_RE = re.compile(
    r"\s*(?:,\s*(?:and\s+then|then|and\s+also|and)\s+|;\s*|"
    r"\.\s+(?:then|and\s+then)\s+|\s+and\s+then\s+|\s+then\s+|"
    r",\s+(?=" + _ACTION_AFTER_COMMA + r"\b))\s*",
    re.IGNORECASE)

# Whitelisted typo corrections (§16) — vocabulary tokens only; tokens,
# paths, URLs, hashes and symbols are never touched.
_TYPO_MAP = {
    "githib": "github", "gihub": "github", "gitub": "github",
    "repsoitory": "repository", "repositry": "repository",
    "invokai": "invokeai", "comfyi": "comfyui",
    "contex": "context", "picure": "picture", "pictue": "picture",
    "iamge": "image", "imgae": "image", "imaeg": "image",
    "brnach": "branch", "comit": "commit", "fucntion": "function",
    "udpate": "update", "erorr": "error", "teh": "the",
    "picutre": "picture", "genrate": "generate", "mkae": "make",
    "wrok": "work", "fixx": "fix", "tes": "test",
}
_TYPO_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(k) for k in _TYPO_MAP) + r")\b")


def _fix_typos(t: str) -> str:
    """Correct whitelisted vocabulary typos only — never arbitrary text."""
    return _TYPO_RE.sub(lambda m: _TYPO_MAP[m.group(0)], t)


# Verbs whose output is inherently visual — "draw a dragon" needs no
# visual noun, unlike "make a function".
_VISUAL_VERB_RE = re.compile(
    r"\b(?:draw|paint|illustrate|sketch|depict|visuali[sz]e|imagine|"
    r"doodle|render)\b",
    re.IGNORECASE)

# Topic-switch markers (§20) — explicit pivots plus "now <new domain>".
_TOPIC_SHIFT_RE = re.compile(
    r"^(?:anyway|ok(?:ay)?[,]?\s+so|so\s+anyway|different\s+question|"
    r"new\s+topic|changing\s+(?:the\s+)?subject|switching\s+topics|"
    r"moving\s+on|forget\s+(?:that|it|all\s+that)|never\s*mind|"
    r"unrelated|on\s+a\s+different\s+note|separately|"
    r"let'?s\s+(?:work\s+on|move\s+to|switch\s+to))[,.\s]*",
    re.IGNORECASE)

# Return-to-topic markers (§21) — the target may be anaphoric; the
# resolver decides what it restores.
_TOPIC_RETURN_RE = re.compile(
    r"^(?:back\s+to|let'?s\s+(?:go\s+)?back\s+to|return(?:ing)?\s+to|"
    r"go\s+back\s+to|get(?:ting)?\s+back\s+to|resume|resuming|"
    r"continu(?:e|ing)\s+(?:the|that|with))\b[,\s]*(.{0,80})",
    re.IGNORECASE)

# Conditional requests (§13) — "if tests pass, push it"; "use InvokeAI
# unless it fails, then try ComfyUI".
_CONDITIONAL_RE = re.compile(
    r"^if\s+(.{3,100}?)(?:,\s*then\b|,|\s+then\b)\s*(.{3,160})$",
    re.IGNORECASE)
_UNLESS_RE = re.compile(
    r"^(.{3,160}?)\s+unless\s+(.{3,100}?)(?:,\s*(?:then\s+)?(.{3,120}))?$",
    re.IGNORECASE)

# Temporal language (§11) — tied to state downstream; marked here so the
# resolver can bind it instead of inventing history.
_TEMPORAL_RE = re.compile(
    r"\b(?:earlier|before|previously|last\s+time|just\s+now|"
    r"a\s+moment\s+ago|yesterday|overnight|since\s+\w+|"
    r"after\s+the\s+restart|before\s+the\s+update|the\s+previous\s+run|"
    r"the\s+latest|the\s+newest|recent(?:ly)?|from\s+earlier|"
    r"a\s+while\s+ago|last\s+night|this\s+morning)\b",
    re.IGNORECASE)

# Comparisons (§9) — operands resolved against the active candidate set.
_COMPARISON_RE = re.compile(
    r"\b(?:which\s+(?:one|is|version|model|option)\b.{0,40}"
    r"(?:better|faster|cheaper|bigger|smaller|worse)|"
    r"compare|comparison|what'?s\s+(?:the\s+)?differen|"
    r"what\s+changed|how\s+does\s+.{0,30}\s+compare|\bvs\b\.?|versus|"
    r"(?:better|worse|faster|slower|cheaper)\s+than|"
    r"or\s+(?:better|would)\b.{0,20}\bbetter)\b",
    re.IGNORECASE)
_COMPARISON_OPERAND_RE = re.compile(
    r"\b(?:than|versus|vs\.?|or)\s+([a-z0-9_.\-/ ]{2,40})",
    re.IGNORECASE)

# Ordinal / list references (§10) — bound to the recent candidate set.
_ORDINALS = {
    "first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3,
    "fourth": 4, "4th": 4, "fifth": 5, "5th": 5,
    "last": -1, "latest": -1, "newest": -1, "previous": -2,
    "earlier": -2, "middle": 0,
}
_ORDINAL_RE = re.compile(
    r"\b(?:the\s+)?(first|1st|second|2nd|third|3rd|fourth|4th|fifth|5th|"
    r"last|latest|newest|previous|earlier|middle)\s+"
    r"(?:one|option|version|image|picture|branch|commit|file|model|"
    r"candidate|choice|result|reply|response|message|approach)\b|"
    r"\bnumber\s+(\d+)\b|\bthe\s+other\s+one\b|\bthe\s+same\s+one\b|"
    r"\bthe\s+first\s+one\b|\bthe\s+second\s+one\b",
    re.IGNORECASE)

# GitHub/repository inspection — natural language over the active repo
# (§53): "check GitHub", "what did Devin push", "anything new land?".
_GITHUB_RE = re.compile(
    r"(?:"
    r"\b(?:github|repo(?:sitory)?|remote|origin|upstream)\b[^.?!]{0,50}"
    r"(?:status|commits?|push(?:ed)?|branch|ci|actions?|workflows?|"
    r"land(?:ed)?|merged?|changed?|new|latest|happening|going\s+on|"
    r"state|connect|working|sync(?:ed)?|update|activity)|"
    r"\b(?:check|see|look\s+at|inspect|peek\s+at|open|watch)\b"
    r"[^.?!]{0,40}\b(?:github|repo|repository|branch|commits?|ci|"
    r"workflow|actions|remote|origin)\b|"
    r"\bwhat\s+did\s+\w+\s+(?:just\s+)?(?:push|commit|merge|land)|"
    r"\banything\s+(?:new\s+)?(?:land|push|merge|commit)|"
    r"\b(?:latest|recent|new)\s+(?:commits?|push|merge|changes)|"
    r"\bcurrent\s+branch\b|\brepo\s+status\b|"
    r"\bwhat'?s\s+(?:happening|going\s+on)\s+(?:with|on|in)\s+"
    r"(?:the\s+)?(?:repo|github|branch|project)\b|"
    r"\bwhere\s+(?:are|is)\s+we\s+at\b"
    r")",
    re.IGNORECASE)

# Repair/coding requests — natural language over an active error/task.
_REPAIR_RE = re.compile(
    r"\b(?:fix|repair|sort\s+(?:that|this|it)\s+out|take\s+care\s+of|"
    r"get\s+.{0,25}\s+working|make\s+.{0,20}\s+work|unbreak|"
    r"get\s+.{0,20}\s+running|fix\s+(?:the|that|this|it)\b|"
    r"solve|resolve|patch|debug|diagnose|investigate)\b",
    re.IGNORECASE)

# Informal/implied trouble reports (§14/§15) — statements that imply a
# fix without an imperative verb.
_IMPLICIT_BROKEN_RE = re.compile(
    r"\b(?:still\s+(?:broken|not\s+working|failing|doesn'?t\s+work|"
    r"isn'?t\s+working|not\s+right)|"
    r"(?:it|that|this)\s*(?:'s|is)?\s+(?:busted|broken|dead|fried|"
    r"messed\s+up|screwed|hosed)|"
    r"(?:it|that)\s+(?:blew\s+up|crashed|died|broke|fails?|"
    r"won'?t\s+(?:work|run|build|compile|start|load)|"
    r"doesn'?t\s+work|isn'?t\s+working|ain'?t\s+working)|"
    r"that\s+ain'?t\s+it|looks?\s+like\s+crap|"
    r"(?:way|far)\s+too\s+(?:slow|fast|close|far|big|small|long|"
    r"dark|bright|loud|quiet|much|little)|"
    r"what\s+the\s+hell\s+happened|you\s+lost\s+me|"
    r"same\s+(?:error|problem|issue|bug)|"
    r"keeps?\s+(?:failing|crashing|breaking|erroring))\b|"
    r"\b(?:image|picture|photo|render|output|result|it|that|this|"
    r"she|he|they|the\s+\w+)\s+(?:is|looks?|seems?|feels?)\s+"
    r"(?:too|way\s+too|a\s+bit|kinda|sort\s+of)\s+\w+",
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
    # Literal "edit image <path>" ops stay on the model+tool lane — only
    # the tools layer can assemble the op arguments. "edit THIS image"
    # is different: the artifact reference is conversational.
    if any(x in t for x in ("edit image", "edit photo",
                            "edit picture")):
        return False, 0.0, []

    # A question ABOUT images asks for information, not an artifact —
    # "which model do you generate images with?" is never a job.
    # Request-forms ("can you draw…") keep their verbs, so only
    # information-seeking lead words veto.
    if re.match(r"^(?:which|what|who|whose|whom|how|when|where|why)\b",
                t) and not _IMAGE_OP_RE.search(t):
        return False, 0.0, []

    visual_noun = bool(_VISUAL_NOUN_RE.search(t))
    visual_adj = bool(_VISUAL_ADJ_RE.search(t))
    non_image = bool(_NON_IMAGE_RE.search(t))

    # Artifact precedence: when the requested output is a non-visual
    # artifact that merely CONTAINS a visual element ("a website with a
    # logo", "a report with a picture"), the first-mentioned artifact
    # decides — the website is being built, not the logo.
    _vn = _VISUAL_NOUN_RE.search(t)
    _ni = _NON_IMAGE_RE.search(t)
    if _vn and _ni and _ni.start() < _vn.start():
        return False, 0.0, []

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
    # Generation verb + a person/creature subject — "generate an adult
    # woman in a red dress", "create a dragon".
    if gen and _PERSON_SUBJECT_RE.search(t) and not non_image:
        evidence.append("generation verb + visual subject")
        return True, 0.85, evidence
    # Inherently visual verbs need no visual noun — "draw a dragon",
    # "visualize a castle", "sketch her face".
    if _VISUAL_VERB_RE.search(t) and not non_image:
        evidence.append("inherently visual verb")
        return True, 0.85, evidence
    # "I want to see what a castle would look like" — hypothetical
    # visual inspection is an image request.
    if show and re.search(
            r"\b(?:would|might|could|will)\s+look\s+like\b", t):
        evidence.append("see-verb + hypothetical appearance")
        return True, 0.8, evidence
    # Show-verb + a concrete requested object that isn't a known
    # non-visual output — "let me see a castle", "can I see the sunset".
    # Object semantics decide: "show me the logs" is vetoed upstream.
    if show and re.search(
            r"\b(?:a|an|the|some|what)\s+\w", t) and not non_image:
        evidence.append("see-verb + concrete non-tool object")
        return True, 0.7, evidence
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


def _inherit_markers(outer: IntentEnvelope, inner: IntentEnvelope) -> None:
    """Copy contextual annotations discovered on the outer turn onto a
    recursively-classified clause — conditions/alternatives wrap, not
    replace, the action's own metadata."""
    inner.topic_shift = inner.topic_shift or outer.topic_shift
    inner.topic_target = inner.topic_target or outer.topic_target
    inner.comparison = inner.comparison or outer.comparison
    for x in outer.comparison_targets:
        if x not in inner.comparison_targets:
            inner.comparison_targets.append(x)
    if inner.ordinal_reference is None:
        inner.ordinal_reference = outer.ordinal_reference
    inner.temporal_context = inner.temporal_context or outer.temporal_context
    for k, v in outer.references.items():
        inner.references.setdefault(k, v)
    for a in outer.ambiguity:
        if a not in inner.ambiguity:
            inner.ambiguity.append(a)
    for e in outer.evidence:
        if e not in inner.evidence:
            inner.evidence.append(e)


def understand_turn(text: str, *, active: Any = None,
                    has_attachments: bool = False) -> IntentEnvelope:
    """Classify one turn, then resolve its references against active
    context. Resolution is post-pass so every lane benefits — a repair
    verb binds "it" to the active error; a modify verb binds "it" to
    the active image."""
    env = _classify_turn(text, active=active,
                         has_attachments=has_attachments)
    if active is not None:
        try:
            from .references import resolve_with_report
            report = resolve_with_report(text, active)
            for term, label in report["resolved"].items():
                env.references.setdefault(term, label)
            for term in report["ambiguous"]:
                note = f"ambiguous reference: {term!r}"
                if note not in env.ambiguity:
                    env.ambiguity.append(note)
        except Exception:
            pass
    return env


def _classify_turn(text: str, *, active: Any = None,
                   has_attachments: bool = False) -> IntentEnvelope:
    """Produce the IntentEnvelope for one user turn.

    `active` is the conversation's ActiveContext (or None). Detection
    precedence mirrors the contract: corrections and pending
    clarifications first, then explicit actions, contextual follow-ups,
    narrow utility questions, compound splitting, conversation.
    """
    env = IntentEnvelope()
    raw = _norm(text)
    t = _fix_typos(_low(raw))
    if not t:
        env.confidence = 1.0
        env.evidence.append("empty")
        return env

    image_ctx = bool(
        active is not None
        and getattr(active, "image_active", lambda **k: False)())

    # --- Contextual markers — annotate the envelope without routing.
    tm = _TEMPORAL_RE.search(t)
    if tm:
        env.temporal_context = tm.group(0)
    om = _ORDINAL_RE.search(t)
    if om:
        if om.group(2):
            env.ordinal_reference = int(om.group(2))
        elif om.group(1):
            env.ordinal_reference = _ORDINALS.get(om.group(1).lower(), 0)
        else:
            # "the other one" / "the same one" — contrastive reference.
            env.ordinal_reference = -3 if "other" in om.group(0) else -4
        env.references["ordinal_phrase"] = om.group(0)
        env.ambiguity.append("ordinal/contrastive reference needs the "
                             "recent candidate set")
    if _COMPARISON_RE.search(t):
        env.comparison = True
        for opm in _COMPARISON_OPERAND_RE.finditer(t):
            operand = opm.group(1).strip(" ,.?!")
            if operand and operand not in env.comparison_targets:
                env.comparison_targets.append(operand)

    # --- Topic markers — flag the move, then classify the new subject.
    rm = _TOPIC_RETURN_RE.match(t)
    if rm:
        env.topic_shift = True
        env.topic_target = (rm.group(1) or "").strip(" ,.?!")
        env.evidence.append("return-to-topic marker")
        t2 = env.topic_target or t
        if not t2:
            env.primary_intent = CONVERSATION
            env.confidence = 0.6
            return env
        t = _fix_typos(_low(t2))
    else:
        sm = _TOPIC_SHIFT_RE.match(t)
        if sm:
            env.topic_shift = True
            env.evidence.append("topic-shift marker")
            t2 = _TOPIC_SHIFT_RE.sub("", t, count=1).strip()
            if not t2:
                env.primary_intent = CONVERSATION
                env.confidence = 0.6
                return env
            t = t2
        else:
            # "now check github" — weak pivot marker; strip and let the
            # intent itself prove the domain move.
            nm = re.match(r"^now[,.! ]+(\S.{2,})$", t)
            if nm:
                t = nm.group(1)
                env.topic_shift = True
                env.evidence.append("'now' pivot marker")
            # "and GitHub?" / "what about the tests?" — fragment that
            # carries its own topic.
            fm = re.match(
                r"^(?:and|what\s+about|how\s+about|but)\s+(\S.{2,80})\??$",
                t)
            if fm:
                t = fm.group(1).rstrip("?")
                env.evidence.append("fragment topic carrier")

    # --- Conditional requests (§13) — the condition is preserved as
    # first-class structure; routing still classifies the ACTION clause.
    cm = _CONDITIONAL_RE.match(t)
    if cm:
        cond, act = cm.group(1).strip(" ,."), cm.group(2).strip(" ,.")
        sub = understand_turn(act, active=active,
                              has_attachments=has_attachments)
        _inherit_markers(env, sub)
        sub.conditionals.append({"condition": cond, "then": act})
        sub.evidence.append("conditional request — action gated")
        return sub
    um = _UNLESS_RE.match(t)
    if um:
        prim = um.group(1).strip(" ,.")
        unless_cond = um.group(2).strip(" ,.")
        fallback = (um.group(3) or "").strip(" ,.")
        sub = understand_turn(prim, active=active,
                              has_attachments=has_attachments)
        _inherit_markers(env, sub)
        sub.conditionals.append({"unless": unless_cond, "primary": prim})
        if fallback:
            sub.alternatives.append(fallback)
        sub.evidence.append("conditional request — fallback preserved")
        return sub

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

    # --- 2.5 Compound multi-intent — BEFORE single-intent lanes so a
    # clause sequence never collapses into its first action ("make the
    # image, save it, and put it in the project folder" keeps all
    # three). Each clause classifies against the same active context.
    parts = _compound_parts(t)
    if len(parts) > 1:
        env.compound = True
        subs = []
        first_env: IntentEnvelope | None = None
        for part in parts:
            sub = _classify_turn(part, active=active,
                                 has_attachments=has_attachments)
            if first_env is None:
                first_env = sub
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
            if first_env is not None:
                # The leading clause carries its extracted payload —
                # subject, prompt, references and constraints all flow
                # to routing even though the turn is compound.
                env.subject = first_env.subject
                env.followup_prompt = first_env.followup_prompt
                env.followup_of = first_env.followup_of
                env.output_type = first_env.output_type
                env.attributes = list(first_env.attributes)
                env.negative_constraints = list(
                    first_env.negative_constraints)
                env.preserve_constraints = list(
                    first_env.preserve_constraints)
                env.source_images = list(first_env.source_images)
                for k, v in first_env.references.items():
                    env.references.setdefault(k, v)
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

    # --- 4.5 Return-to-topic restore — "back to that angel image"
    # re-activates a parked entity whose label shares words with the
    # target. No matching entity → stay conversation; never invent one.
    if env.topic_target and active is not None:
        stop = {"that", "the", "this", "one", "image", "picture",
                "photo", "back", "thing", "stuff", "work"}
        target_words = {w for w in re.findall(
            r"[a-z]{3,}", env.topic_target.lower()) if w not in stop}
        if target_words:
            # Live image subject still active — "back to" is redundant
            # but still routes there.
            live_subject = getattr(active, "active_image_subject", "")
            live_words = set(re.findall(
                r"[a-z]{3,}", str(live_subject).lower()))
            if live_subject and (target_words & live_words):
                env.primary_intent = IMAGE_FOLLOWUP
                env.requested_action = "resume"
                env.subject = live_subject
                env.followup_of = "topic_return"
                env.followup_prompt = env.topic_target
                env.confidence = 0.8
                env.evidence.append("returned topic = active image")
                return env
            for ent in getattr(active, "entities", lambda: [])():
                label_words = set(re.findall(
                    r"[a-z]{3,}", str(ent.get("label") or "").lower()))
                if not (target_words & label_words):
                    continue
                env.followup_of = "topic_return"
                env.confidence = 0.75
                env.evidence.append(
                    f"returned topic matched {ent.get('kind')} entity")
                if ent.get("kind") == "image":
                    env.primary_intent = IMAGE_FOLLOWUP
                    env.requested_action = "resume"
                    env.subject = str(ent.get("label") or "")
                    # Restore the image task — follow-ups apply again.
                    env.followup_prompt = env.topic_target
                    if ent.get("job"):
                        env.source_images.append(str(ent["job"]))
                else:
                    env.primary_intent = str(ent.get("kind") or CONVERSATION)
                return env
            env.ambiguity.append(
                f"return target {env.topic_target!r} matched no "
                "retained entity")

    # --- 5.5 GitHub/repository inspection — natural language over the
    # active repo ("check GitHub", "what did Devin push", "anything
    # new land?"). Repo-word-free forms stay lower-confidence.
    if _GITHUB_RE.search(t):
        env.primary_intent = GITHUB_STATUS
        env.requested_action = "inspect_github"
        has_repo_word = bool(re.search(
            r"\b(?:github|repo(?:sitory)?|remote|origin|upstream|"
            r"branch|commit|ci|workflow|actions|push|merge|land)\b",
            t))
        env.confidence = 0.85 if has_repo_word else 0.6
        if not has_repo_word:
            env.ambiguity.append(
                "repository reference implied, not explicit")
        env.evidence.append("repository/GitHub inspection language")
        return env

    # --- 5.6 Generic continuation — "do it", "try again", "keep going"
    # against an active task. Without one, the words carry nothing.
    if re.match(
        r"^(?:do\s+(?:it|that)|go\s+ahead|try\s+again|retry|"
        r"keep\s+going|continue|proceed|run\s+it|yes\s+do\s+it)\s*[.!]?$",
            t) and active is not None:
        prior = getattr(active, "last_intent", "") or CONVERSATION
        env.primary_intent = prior if prior != CONVERSATION else CONVERSATION
        env.continuation_of = prior
        env.confidence = 0.75 if prior != CONVERSATION else 0.5
        env.evidence.append("bare continuation phrase + active context")
        if prior == CONVERSATION:
            env.ambiguity.append("continuation phrase with no clear task")
        if image_ctx:
            env.primary_intent = IMAGE_FOLLOWUP
            env.requested_action = "modify"
            env.followup_of = "active_image"
            env.followup_prompt = t
            env.confidence = 0.85
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
        r"start|kill|install|update|deploy|save|copy|export|"
        r"put|place|attach|insert|upload|download|schedule|post|"
        r"share|store|verify|apply|clean|sync|publish|merge)\s+", t):
        env.primary_intent = TOOL_ACTION
        env.requested_action = t.split()[0]
        env.confidence = 0.8
        env.evidence.append("imperative tool verb")
        return env
    if re.match(
        r"^(?:use|switch\s+to|try)\s+(invokeai|comfyui|"
        r"[a-z][\w.\-]*)\b", t):
        env.primary_intent = TOOL_ACTION
        env.requested_action = "select_tool"
        env.subject = t.split(None, 2)[-1] if len(t.split()) > 2 else ""
        env.confidence = 0.7
        env.evidence.append("tool/model selection verb")
        return env
    if re.search(r"\b(?:check|show|list|what'?s)\b.{0,40}"
                 r"\b(?:branch|build|ci|actions?|tests?|status|logs?|"
                 r"errors?|failures?|diff|github|repository)\b", t):
        env.primary_intent = TOOL_ACTION
        env.requested_action = "inspect"
        env.confidence = 0.7
        env.evidence.append("project-state inspection request")
        return env

    # --- 6.5 Repair requests — "fix the error", "sort that out", "get
    # that working". Resolve against the active error when one exists.
    if _REPAIR_RE.search(t) and re.search(
        r"\b(?:error|bug|issue|problem|fail(?:ing|ure)?|broken|crash|"
        r"exception|traceback|warning|test|build|code|it|that|this|"
        r"thing|the|broke|breaking|went\s+wrong)\b", t):
        env.primary_intent = CODING
        env.requested_action = "repair"
        env.confidence = 0.78 if active is not None else 0.65
        env.evidence.append("repair verb + failure reference")
        if re.search(r"\b(?:it|that|this|the)\b", t):
            env.references["repair_target"] = (
                getattr(active, "active_error", "")
                if active is not None else "")
            if not env.references["repair_target"]:
                env.ambiguity.append(
                    "repair target is anaphoric — needs active error")
        return env

    # --- 6.6 Implicit trouble reports — statements implying a fix
    # without an imperative ("it's busted", "still not working",
    # "way too slow"). Confidence stays honest.
    if _IMPLICIT_BROKEN_RE.search(t):
        env.implicit = True
        if image_ctx:
            env.primary_intent = IMAGE_FOLLOWUP
            env.requested_action = "modify"
            env.subject = getattr(active, "active_image_subject", "")
            env.followup_of = "active_image"
            env.followup_prompt = t
            env.confidence = 0.7
            env.evidence.append(
                "negative report against active image")
            return env
        if active is not None:
            env.primary_intent = CODING
            env.requested_action = "diagnose"
            env.continuation_of = getattr(active, "last_intent", "") or ""
            env.confidence = 0.6
            env.evidence.append(
                "implicit trouble report + active task")
            env.ambiguity.append(
                "implied request — inferred from negative report")
            return env
        env.primary_intent = FEEDBACK_SIGNAL
        env.confidence = 0.55
        env.evidence.append("negative report, no identifiable target")
        env.ambiguity.append("target of complaint is unclear")
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
                r"could|do|does|did|should|would|shall|will)\b", t):
        env.primary_intent = QUESTION
        env.confidence = 0.6 if t.endswith("?") else 0.5
        env.evidence.append("interrogative open")
        return env

    # --- 9. Bare visual noun phrase as the whole turn.
    # (covered by detect_image_intent's bare-noun branch; reaching here
    # means no intent matched at all)
    env.primary_intent = CONVERSATION
    env.confidence = 0.4
    env.evidence.append("no explicit action intent")
    return env
