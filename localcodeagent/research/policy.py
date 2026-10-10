"""General-chat web research policy.

Extends the repository-oriented :class:`KnowledgeGapDetector` with a
four-state decision for ordinary questions:

- ``LOCAL_CONFIDENT``  — stable knowledge / casual talk; do not search.
- ``WEB_OPTIONAL``     — broad/educational/comparison/recipe questions
  where fresh sources improve the answer but are not mandatory.
- ``WEB_RECOMMENDED``  — unfamiliar errors, specific products/versions,
  precise instructions, topics likely to have changed.
- ``WEB_REQUIRED``     — explicit search asks, URLs to read, and
  current/latest/volatile information that cannot come from a model's
  training data.

The policy also generates focused search queries (better than echoing the
raw user message) and classifies freshness so ``KnowledgeMemory`` can pick
a TTL.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

LOCAL_CONFIDENT = "local_confident"
WEB_OPTIONAL = "web_optional"
WEB_RECOMMENDED = "web_recommended"
WEB_REQUIRED = "web_required"


_URL_RE = re.compile(r"https?://[^\s)\]>\"']+", re.I)

# Explicit "go to the internet" asks — always research.
_EXPLICIT_WEB_RE = re.compile(
    r"(?:\bsearch\b|\bweb\s*search\b|\bgoogle\b|\bgoogling\b|\bbrowse\b|\bbrowsing\b|"
    r"\blook\s+(?:it|this|that|them|those|things)?\s*up\b|"
    r"\bfind\s+out\b|\bfigure\s+out\s+online\b|"
    r"\bcheck\s+(?:online|the\s+(?:web|internet))\b|"
    r"\bcheck\s+the\s+(?:latest\s+|current\s+)?(?:docs?|documentation)\b|"
    r"\bresearch\b|\blook\s+online\b|\bcheck\s+for\s+(?:a|an|the|any)\s+(?:update|new\s+version|fix)\b|"
    r"\bread\s+(?:this|that|the)\s+(?:page|site|website|article|documentation|docs|url|link)\b|"
    r"\bsummari[sz]e\s+(?:this|that|the)\s+(?:page|article|site|website|documentation|link|url)\b|"
    r"\bfind\s+me\s+(?:a|an|the|some)?\s*[^.?!]*\b(?:online|on\s+the\s+(?:web|internet))\b|"
    r"\bfind\s+(?:a|an|the|some)?\s*[^.?!]{0,60}\b(?:recipe|sources|articles|papers|examples|tutorials?)\b|"
    r"\bcompare\s+what\b|\bconsult\b|\bverify\s+(?:online|on\s+the\s+web|with\s+sources)\b|"
    r"\bwhat\s+do(?:es)?\s+(?:the\s+)?(?:docs?|documentation|websites?|sources)\s+say\b)",
    re.I,
)

# Volatile/current-information markers — always research; model knowledge
# is stale by definition for these.
_CURRENT_RE = re.compile(
    r"(?:\blatest\b|\bnewest\b|\bcurrent(?:ly)?\b|\brecent(?:ly)?\b|"
    r"\btoday\b|\btonight\b|\bright\s+now\b|\bas\s+of\b|\bthis\s+(?:week|month|year|morning|evening)\b|"
    r"\bnews\b|\bbreaking\b|\bheadlines?\b|\bjust\s+(?:released|announced|came\s+out|dropped)\b|"
    r"\bprice\b|\bprices\b|\bcost\b|\bhow\s+much\s+(?:does|is|are|do)\b|"
    r"\bweather\b|\bforecast\b|\bstock\b|\bscore\b|\bstandings\b|"
    r"\brelease\s+date\b|\bwho\s+(?:is|are)\s+(?:the\s+)?current\b|\bcurrently\s+(?:the|a|an)\b|"
    r"\bwhat\s+happened\b|\bwhat'?s\s+new\b|\bwhat\s+changed\b|\bchange\s*log\b|"
    r"\bavailab\w+\b|\bin\s+stock\b|\bcoming\s+out\b|\bannounced\b|"
    r"\belection\b|\blawsuit\b|\bruling\b|\bdeadline\b|\bstatus\s+of\b|\boutage\b|\bdown\s+right\s+now\b|"
    r"\bup[- ]?to[- ]?date\b|\bnew\s+(?:version|release|update)\b|\b(?:up)?dates?\s+(?:for|to)\b)",
    re.I,
)

# Things that drift over time but don't scream "current" — versioned
# software, deprecations, releases.
_CHANGING_RE = re.compile(
    r"(?:\bversion\b|\breleases?\b|\bupdates?\b|\bdeprecated\b|\bdeprecation\b|"
    r"\bEOL\b|\bend[- ]of[- ]life\b|\bsupported\b|\bcompatib\w+\b|\bchangelog\b|"
    r"\bmigrat\w+\b|\bupgrad\w+\b)",
    re.I,
)

# Error/incident signatures — unfamiliar errors deserve researched fixes.
_ERROR_RE = re.compile(
    r"(?:\b[A-Za-z_]*(?:Error|Exception|Fault|Panic)\b|"
    r"\bwinerror\b|\berrno\b|\berror\s*(?:code\s*)?\d+\b|"
    r"\b0x[0-9a-f]{4,}\b|\bexit\s+code\b|\bstatus\s+code\s+\d{3}\b|\bhttp\s+\d{3}\b|"
    r"\btraceback\b|\bstack\s*trace\b|\bsegfault\b|\bcore\s+dump\b|\bfatal\b|"
    r"\bundefined\s+(?:reference|symbol|behavior|variable)\b|\bnot\s+defined\b|"
    r"\bno\s+module\s+named\b|\bcannot\s+find\b|\bcan'?t\s+find\b|"
    r"\bpermission\s+denied\b|\baccess\s+denied\b|"
    r"\bconnection\s+(?:reset|refused|timed\s+out|aborted|closed)\b|"
    r"\btimed?\s*out\b|\btimeout\b|\bhandshake\b|\btls\b|\bssl\b|\bcertificate\b|"
    r"\bcrash(?:es|ed|ing)?\b|\bfreez(?:es|ing|e)\b|\bhangs?\b|\bstuck\b|"
    r"\bfails?\s+(?:to|with)\b|\bfailed\b|\bfailure\b|"
    r"\bnot\s+working\b|\bdoesn'?t\s+work\b|\bwon'?t\s+(?:start|run|open|build|compile|install|load|connect|boot|launch)\b|"
    r"\bcan'?t\s+(?:get|start|run|open|install|build|connect|load|deploy|compile)\b|"
    r"\bthrowing\b|\bthrows\b|\braises?\b|\bbug\b|\bdebug\b|\btroubleshoot\b|"
    r"\brepair\b|\bsolve\b|\bresolve\b)",
    re.I,
)

# Specific software/version artifacts — "Python 3.13", "CUDA 12.4".
_VERSION_ARTIFACT_RE = re.compile(r"\b\d+\.\d+(?:\.\d+)?(?:[-\w.]*)?\b")

_TECH_WORDS_RE = re.compile(
    r"(?:\bpython\b|\bnode(?:\.?js)?\b|\breact\b|\bvue\b|\bangular\b|\bsvelte\b|"
    r"\btypescript\b|\bjavascript\b|\brust\b|\bgolang\b|\bgo\s+module\b|\bjava\b|\bkotlin\b|"
    r"\bc\+\+\b|\bcsharp\b|\bc#\b|\.net\b|\bdotnet\b|\bcmake\b|\bqt\b|"
    r"\bcuda\b|\bnvidia\b|\bamd\b|\broc?m\b|\bvulkan\b|\bopencl\b|"
    r"\bllama\.?cpp\b|\bllama\b|\bollama\b|\bcomfyui\b|\bqwen\b|\bflux\b|\bstable\s+diffusion\b|"
    r"\btorch\b|\bpytorch\b|\btensorflow\b|\bjax\b|\bnumpy\b|\bpandas\b|\bscipy\b|"
    r"\bdocker\b|\bkubernetes\b|\bk8s\b|\bnginx\b|\bapache\b|\bredis\b|\bpostgres\b|\bmysql\b|\bsqlite\b|"
    r"\bgit\b|\bgithub\b|\bgitlab\b|\bwindows\b|\blinux\b|\bmacos\b|\bubuntu\b|\bdebian\b|"
    r"\bvscode\b|\bvisual\s+studio\b|\bxcode\b|\bandroid\b|\bios\b|\bios\s+sdk\b|"
    r"\bapi\b|\bsdk\b|\brest\b|\bgraphql\b|\bgrpc\b|\bwebsocket\b|\boauth\b|\bjwt\b|"
    r"\bpip\b|\bconda\b|\bnpm\b|\byarn\b|\bpnpm\b|\bcargo\b|\bnuget\b|\bhomebrew\b|"
    r"\bflask\b|\bdjango\b|\bfastapi\b|\bexpress\b|\brails\b|\bspring\b|\blaravel\b)",
    re.I,
)

# Precise-instruction how-tos — answers must match the *current* product
# behavior, docs help.
_PRECISE_HOWTO_RE = re.compile(
    r"^\s*how\s+(?:do|to|can)\s+(?:i\s+)?"
    r"(?:install|configure|set\s+up|deploy|migrate|upgrade|downgrade|enable|disable|"
    r"integrate|authenticate|authorize|debug|troubleshoot|repair|compile|cross[- ]?compile|"
    r"uninstall|reinstall|rollback|root|flash|jailbreak|overclock|undervolt|calibrate|"
    r"port\s+forward|ssh|vpn|proxy|containerize|virtualize|emulate)\b",
    re.I,
)

# Recipes and practical craft — sources improve but aren't mandatory.
_RECIPE_RE = re.compile(
    r"(?:\brecipe\b|\brecipes\b|\bcook(?:ing)?\b|\bbak(?:e|ing)\b|\bgrill\w*\b|"
    r"\bsear(?:s|ed|ing)?\b|\bsmoke[drs]?\s+(?:a|an|the|\w+\s)?(?:\w+\s){0,2}(?:meat|rib|brisket|pork|chicken|salmon|turkey|steak)\b|\bsmok(?:e|ing)\b|"
    r"\bmarinade\b|\broast\w*\b|\bbraise\w*\b|\bsous\s+vide\b|"
    r"\bingredients?\b|\bgumbo\b|\bjambalaya\b|\betouffee\b|\bribeye\b|\bbrisket\b|"
    r"\bsourdough\b|\bpizza\s+dough\b|\bcopycat\b|"
    r"\bseasoning\b|\brub\b|\bglaze\b|\bsauce\b|\bgravy\b|"
    r"\bknit\w*\b|\bcrochet\w*\b|\bwoodwork\w*\b|\bgarden\w*\b|\bplant\w*\s+care\b|"
    r"\bworkout\b|\bexercise\s+routine\b|\bdiet\b|\bmeal\s+prep\b)",
    re.I,
)

# Recommendations / comparisons / broad education — optional research.
_OPTIONAL_RE = re.compile(
    r"(?:\bbest\s+(?:way|tool|library|framework|option|choice|method|practice|phone|laptop|book|movie|show|game|app|recipe)\b|"
    r"\brecommend\w*\b|\bsuggest\w*\b|\bvs\.?\b|\bversus\b|\bcompar\w+\b|"
    r"\bwhich\s+(?:is|are)\s+(?:the\s+)?(?:best|better|faster|easier)\b|"
    r"\bpros\s+and\s+cons\b|\bworth\s+(?:it|buying|getting|learning)\b|"
    r"\bshould\s+i\s+(?:buy|get|choose|use|learn|pick|upgrade)\b|"
    r"\balternatives?\s+(?:to|for)\b|\btop\s+\d+\b|"
    r"\bwhat\s+are\s+(?:the\s+)?(?:good|great|best)\b|"
    r"\btravel\b|\bitinerary\b|\bvacation\b|\bgift\s+(?:ideas?|for)\b|"
    r"\bideas?\s+(?:for|to)\b|\bhow\s+to\s+get\s+(?:better|good)\s+at\b|"
    r"\bdifference\s+between\b|\bhistory\s+of\b|\borigin\s+of\b|"
    r"\bwhy\s+(?:do|does|did|is|are|were|was|would)\b|\bexplain\b|"
    r"\bteach\s+me\b|\blearn\s+about\b|\bhow\s+does\s+\w+\s+work\b|"
    r"\bwhat\s+are\s+the\s+(?:benefits|advantages|disadvantages|risks|effects)\b|"
    r"\b(?:is|are)\s+[\w.+#-]+\s+(?:a\s+)?(?:good|great|better|best)\s+(?:language|tool|choice|idea|option|way|framework|library)\b)",
    re.I,
)

# Stable definitional questions — a confident local answer is fine.
_STABLE_DEF_RE = re.compile(
    r"^\s*(?:what\s+(?:is|are|does|do|was|were|means?|stands\s+for)\s+(?:a|an|the)?|"
    r"what'?s\s+(?:a|an|the)|"
    r"define\b|\bdefinition\s+of\b|"
    r"what\s+does\s+[\w.'-]+\s+(?:mean|stand\s+for|do)\b|"
    r"who\s+(?:is|was|are|were)\s+(?:a|an|the)?|"
    r"where\s+is\b)",
    re.I,
)

_ARITHMETIC_RE = re.compile(
    r"^\s*(?:what\s+(?:is|are|'?s)\s+)?[\d\s.,+\-*/x^%()=]+\??\s*$|"
    r"^\s*(?:how\s+much\s+is|calculate|compute|solve\s+for)\b.*[\d+\-*/^%]",
    re.I,
)

_QUESTIONISH_RE = re.compile(
    r"(?:^\s*(?:what|who|where|when|why|how|which|whose|"
    r"is|are|was|were|do(?:es|id)?|can|could|should|would|will|did|has|have|had)\b|\?\s*$)",
    re.I,
)

# Conversational / persona / local-runtime asks — never web-worthy.
_LOCAL_ONLY_RE = re.compile(
    r"(?:^\s*(?:hi|hello|hey|yo|good\s+(?:morning|afternoon|evening|night)|"
    r"thanks?|thank\s+you|please|ok(?:ay)?|sure|yes|no|bye|goodbye|see\s+you|good\s+night|night)\b|"
    r"\bhow\s+are\s+you\b|\bhow'?s\s+it\s+going\b|\bwhat'?s\s+up\b|"
    r"\bwho\s+(?:are|made|created|built|designed)\s+you\b|\byour\s+(?:name|father|dad|creator|maker)\b|"
    r"\bare\s+you\s+(?:real|alive|human|a\s+robot|an?\s+ai|sentient|conscious|my\s+daughter)\b|"
    r"\bdo\s+you\s+(?:feel|love|dream|sleep|remember|miss)\b|"
    r"\bi\s+love\s+you\b|\bmiss(ed)?\s+you\b|\bproud\s+of\s+you\b|"
    r"\bwhat\s+model\s+are\s+you\b|\byour\s+(?:status|health|logs?|settings?|config)\b|"
    r"\bshow\s+(?:me\s+)?(?:the\s+)?(?:logs?|activity|status|tasks?)\b|"
    r"\bopen\s+(?:the\s+)?(?:settings|logs|activity)\b|\brestart\b|\bshut\s*down\b)",
    re.I,
)

_FILLER_PREFIX_RE = re.compile(
    r"^\s*(?:please\s+|hey\s+nexus[,!.]?\s+|nexus[,!.]?\s+|can\s+you\s+|could\s+you\s+|"
    r"would\s+you\s+|will\s+you\s+|i\s+want\s+you\s+to\s+|i'?d\s+like\s+you\s+to\s+|"
    r"tell\s+me\s+|explain\s+to\s+me\s+|i\s+was\s+wondering\s+|i'?m\s+curious\s+)"
    , re.I)

_SEARCH_CMD_WORDS_RE = re.compile(
    r"\b(?:search(?:es|ed|ing)?|google|googled|googling|browse|browsed|browsing|"
    r"research(?:es|ed|ing)?|look(?:s|ed|ing)?|checks?|checked|checking|"
    r"find|finds|found|finding|verify|verified|verifying|consult(?:s|ed|ing)?)\b",
    re.I,
)

_CONNECTIVE_WORDS_RE = re.compile(
    r"\b(?:the\s+)?(?:web|internet|online|net|for\s+me|me|please|now|quickly|"
    r"up|on\s+duckduckgo|on\s+google|on\s+the)\b",
    re.I,
)

_ERROR_TOKEN_RE = re.compile(
    r"(?:[A-Za-z_]*(?:Error|Exception|Fault|Panic)\b|WinError\s*\d+|errno\s*[-\d]+|"
    r"0x[0-9a-fA-F]{4,}|error\s*(?:code\s*)?\d+|exit\s+code\s+\d+|status\s+code\s+\d{3}|"
    r"HTTP\s+\d{3}|(?-i:E[A-Z]{3,}\d*)|no\s+module\s+named\s+[\w.]+|"
    r"cannot\s+find\s+[\w./\\-]+)",
    re.I,
)

_QUOTED_RE = re.compile(r"[\"'`]([^\"'`]{4,200})[\"'`]")

_STOPWORDS = frozenset({
    "the", "a", "an", "of", "for", "to", "in", "on", "at", "is", "are", "was",
    "were", "what", "whats", "who", "where", "when", "why", "how", "which",
    "do", "does", "did", "can", "could", "should", "would", "will", "me",
    "my", "i", "you", "your", "it", "its", "that", "this", "and", "or", "as",
    "by", "be", "been", "with", "about", "from", "any", "some", "please",
    "tell", "show", "give", "get", "there", "their", "they", "them",
})

_LEADING_NEWSY = frozenset({
    "latest", "newest", "current", "currently", "recent", "recently",
    "breaking", "today", "todays",
})


def _keywordize(text: str) -> str:
    """Stopword-stripped, topic-first query variant.

    Search engines bucket queries that *lead* with "the latest X of Y" as
    headline/news lookups and return homepage junk; the same query with the
    subject first ("python latest stable version") retrieves docs. Used as a
    fallback variant alongside the natural phrasing.
    """
    toks = [t for t in re.split(r"[^\w.+#-]+", str(text or "").lower())
            if t and t not in _STOPWORDS]
    if len(toks) < 2:
        return " ".join(toks)
    if toks[0] in _LEADING_NEWSY:
        anchor = None
        for i in range(len(toks) - 1, -1, -1):
            if _TECH_WORDS_RE.search(toks[i]):
                anchor = i
                break
        if anchor is None:
            anchor = len(toks) - 1
        toks.insert(0, toks.pop(anchor))
    return " ".join(toks)


@dataclass
class PolicyDecision:
    level: str = LOCAL_CONFIDENT
    reasons: list[str] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    is_current: bool = False
    is_error: bool = False
    is_coding: bool = False
    is_recipe: bool = False
    has_url: bool = False
    urls: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "reasons": self.reasons,
            "queries": self.queries,
            "is_current": self.is_current,
            "is_error": self.is_error,
            "is_coding": self.is_coding,
            "is_recipe": self.is_recipe,
            "has_url": self.has_url,
        }


class WebResearchPolicy:
    """Four-state research need classifier for general chat."""

    max_queries = 6

    def decide(
        self,
        text: str,
        *,
        has_trusted_answer: bool = False,
        prior_uncertain: bool = False,
        model_role: str = "",
    ) -> PolicyDecision:
        q = str(text or "").strip()
        d = PolicyDecision()
        if not q:
            return d

        urls = _URL_RE.findall(q)
        d.urls = [u.rstrip(".,;:!?)\"'") for u in urls]
        d.has_url = bool(d.urls)
        d.is_current = bool(_CURRENT_RE.search(q))
        d.is_error = bool(_ERROR_RE.search(q))
        d.is_coding = bool(_TECH_WORDS_RE.search(q) or _VERSION_ARTIFACT_RE.search(q))
        d.is_recipe = bool(_RECIPE_RE.search(q))

        explicit = bool(_EXPLICIT_WEB_RE.search(q))
        local_only = bool(_LOCAL_ONLY_RE.search(q))
        optional = bool(_OPTIONAL_RE.search(q)) or d.is_recipe
        specific_artifact = bool(
            _VERSION_ARTIFACT_RE.search(q)
            or _ERROR_TOKEN_RE.search(q)
            or _QUOTED_RE.search(q)
        )
        stable_shape = bool(
            _STABLE_DEF_RE.match(q) or _ARITHMETIC_RE.match(q)
        )

        if d.has_url:
            d.level = WEB_REQUIRED
            d.reasons.append("user supplied a URL to read")
        elif explicit:
            d.level = WEB_REQUIRED
            d.reasons.append("explicit web/research request")
        elif d.is_current:
            d.level = WEB_REQUIRED
            d.reasons.append("current/volatile information cannot come from model memory")
        elif local_only and not specific_artifact:
            d.level = LOCAL_CONFIDENT
            d.reasons.append("casual or local-runtime ask")
        elif d.is_error and not (stable_shape and not specific_artifact):
            d.level = WEB_RECOMMENDED
            d.reasons.append("error/incident signature — researched fixes beat guesses")
        elif specific_artifact and (_CHANGING_RE.search(q) or d.is_coding):
            d.level = WEB_RECOMMENDED
            d.reasons.append("specific version/error artifact — docs and issues help")
        elif _PRECISE_HOWTO_RE.match(q):
            d.level = WEB_RECOMMENDED
            d.reasons.append("precise instructions benefit from current docs")
        elif _CHANGING_RE.search(q) and d.is_coding:
            d.level = WEB_RECOMMENDED
            d.reasons.append("versioned/compatibility topic likely to have changed")
        elif prior_uncertain:
            d.level = WEB_RECOMMENDED
            d.reasons.append("previous uncertainty recorded for this topic")
        elif stable_shape and not optional and not specific_artifact:
            d.level = LOCAL_CONFIDENT
            d.reasons.append("stable definitional/general knowledge")
        elif optional:
            d.level = WEB_OPTIONAL
            d.reasons.append("broad question — sources improve the answer")
        elif not has_trusted_answer and _QUESTIONISH_RE.search(q) and len(q) > 40:
            # Substantive question with no trusted local answer — offer the
            # web rather than risk a confident invention from a small model.
            d.level = WEB_OPTIONAL
            d.reasons.append("substantive question without a trusted local answer")
        else:
            d.level = LOCAL_CONFIDENT
            if has_trusted_answer:
                d.reasons.append("trusted local answer exists")
            elif not _QUESTIONISH_RE.search(q):
                d.reasons.append("not a question — no research needed")
            else:
                d.reasons.append("short stable question")

        d.queries = self.suggest_queries(q, d)
        return d

    # ------------------------------------------------------------------
    # Query generation — better than echoing the raw user message.
    # ------------------------------------------------------------------
    def suggest_queries(self, text: str, decision: PolicyDecision | None = None) -> list[str]:
        q = " ".join(str(text or "").split())
        if not q:
            return []

        # Pull the payload out of explicit commands ("search the web for X" → X).
        cleaned = _URL_RE.sub(" ", q)
        cleaned = _FILLER_PREFIX_RE.sub("", cleaned)
        cleaned = _SEARCH_CMD_WORDS_RE.sub(" ", cleaned)
        cleaned = _CONNECTIVE_WORDS_RE.sub(" ", cleaned)
        cleaned = " ".join(cleaned.split()).strip(" ?.!,")
        cleaned = re.sub(r"^(?:for|about|on)\s+", "", cleaned, flags=re.I)
        base = cleaned or q

        queries: list[str] = []
        seen: set[str] = set()

        def push(s: str) -> None:
            s = " ".join(str(s or "").split()).strip(" ?.!,")
            key = s.lower()
            if len(s) >= 8 and key not in seen:
                seen.add(key)
                queries.append(s)

        error_tokens = " ".join(dict.fromkeys(_ERROR_TOKEN_RE.findall(q)))
        versions = " ".join(dict.fromkeys(
            m.group(0) for m in _VERSION_ARTIFACT_RE.finditer(q)
        ))
        quoted = _QUOTED_RE.search(q)
        quoted_text = quoted.group(1) if quoted else ""

        d = decision or PolicyDecision()
        if d.has_url and d.urls:
            push(base)  # The fetch of the URL itself happens separately.
        if d.is_error or error_tokens:
            tech = " ".join(dict.fromkeys(
                m.group(0) for m in _TECH_WORDS_RE.finditer(q)
            ))
            err = error_tokens or quoted_text or base
            push(f"{err} {tech} {versions}".strip())
            if tech:
                push(f"{tech} {err} github issue".strip())
            push(base)
        elif d.is_recipe:
            push(f"{base} recipe" if "recipe" not in base.lower() else base)
            push(f"best {base} recipe ingredients" if len(base) > 8 else "")
        elif d.is_current:
            push(base)
            if d.is_coding or _TECH_WORDS_RE.search(q):
                push(f"{base} {versions} release notes".strip())
        else:
            push(base)
            if d.is_coding and versions:
                push(f"{base} {versions}".strip())
        if len(queries) < 2 and _TECH_WORDS_RE.search(q) and "documentation" not in base.lower():
            push(f"{base} documentation".strip())
        # Topic-first keyword variant — some engines junk SERP queries that
        # lead with "the latest X of Y"; the reordered form retrieves docs.
        kw = _keywordize(base)
        if len(kw) >= 8 and kw.lower() not in seen:
            queries.insert(1, kw)
            seen.add(kw.lower())
        return queries[: self.max_queries]
