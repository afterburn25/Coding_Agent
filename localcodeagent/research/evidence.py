"""Evidence-quality layer: score components, source independence,
corroboration, conflict detection, claim→source mapping, confidence.

Five pages repeating one story are not five confirmations — the
independence analyzer collapses copied/derivative content into evidence
clusters. Confidence is computed from the *strongest independent*
evidence, not raw source count.

Everything here is provenance metadata, never chain-of-thought.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from . import trust as trust_mod
from .types import ResearchSource

# ---------------------------------------------------------------------------
# Freshness importance per topic (Part 28)
# ---------------------------------------------------------------------------
FRESHNESS_IMPORTANCE: dict[str, float] = {
    trust_mod.NEWS: 1.0,
    trust_mod.POLITICS: 0.95,
    trust_mod.FINANCE: 0.9,
    trust_mod.ECONOMICS: 0.85,
    trust_mod.SOFTWARE: 0.75,
    trust_mod.CYBERSECURITY: 0.9,
    trust_mod.PROGRAMMING: 0.7,
    trust_mod.PRODUCT_SPECS: 0.8,
    trust_mod.CONSUMER_REVIEW: 0.75,
    trust_mod.TRAVEL: 0.7,
    trust_mod.MEDICAL: 0.55,
    trust_mod.HEALTH: 0.55,
    trust_mod.LEGAL: 0.6,
    trust_mod.AUTOMOTIVE: 0.5,
    trust_mod.HOME: 0.4,
    trust_mod.SCIENCE: 0.45,
    trust_mod.ACADEMIC: 0.4,
    trust_mod.COOKING: 0.3,
    trust_mod.HISTORY: 0.15,
    trust_mod.GENERAL: 0.45,
}

# Corroboration states (Part 17)
SINGLE_PRIMARY = "single_primary"
MULTIPLE_PRIMARY = "multiple_primary"
PRIMARY_PLUS_SECONDARY = "primary_plus_secondary"
MULTIPLE_INDEPENDENT_SECONDARY = "multiple_independent_secondary"
COMMUNITY_ONLY = "community_only"
CONFLICTED = "conflicted"
UNVERIFIED = "unverified"

# Confidence levels (Part 32)
HIGH = "high"
MODERATE = "moderate"
LOW = "low"
CONF_CONF = "conflicted"

# Reliability labels (Part 31)
REL_PRIMARY_CONFIRMED = "Primary Confirmed"
REL_STRONG = "Strong"
REL_SUPPORTED = "Supported"
REL_COMMUNITY = "Community Supported"
REL_CONFLICTED = "Conflicted"
REL_WEAK = "Weak"
REL_UNVERIFIED = "Unverified"
REL_BLOCKED = "Blocked"

_STRONG_CLASSES = {
    trust_mod.PRIMARY_OFFICIAL,
    trust_mod.OFFICIAL_DOCUMENTATION,
    trust_mod.GOVERNMENT,
    trust_mod.STANDARD_BODY,
    trust_mod.PEER_REVIEWED_RESEARCH,
    trust_mod.ORIGINAL_PROJECT,
    trust_mod.ORIGINAL_DATA,
    trust_mod.MAJOR_PROFESSIONAL_SOURCE,
}
_WEAK_CLASSES = {
    trust_mod.BLOG,
    trust_mod.AGGREGATOR,
    trust_mod.SCRAPED_CONTENT,
    trust_mod.SEO_CONTENT,
    trust_mod.UNKNOWN,
    trust_mod.FORUM,
}

_WORD_RE = re.compile(r"[a-z0-9]+")
_VERSION_RE = re.compile(r"\b\d+\.\d+(?:\.\d+)*(?:[-\w.]*)?\b")
_NEGATION_RE = re.compile(
    r"\b(?:not|no|never|removed|drops?|dropped|deprecat\w+|discontinued|"
    r"unsupported|doesn'?t|don'?t|won'?t|can'?t|cannot|isn'?t|aren'?t|wasn'?t|"
    r"without|fails?|failed|broken|incompatible)\b", re.I)
_CURRENT_CLAIM_RE = re.compile(
    r"\b(?:latest|newest|current|most recent|as of|up[- ]to[- ]date)\b", re.I)
_DATE_PATTERNS = [
    re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"),
    re.compile(r"\b(\d{4})/(\d{2})/(\d{2})\b"),
    re.compile(
        r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d{1,2}),?\s+(\d{4})\b",
        re.I),
]
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
# Wire/agency attribution — stories copied off a wire are dependent.
_WIRE_RE = re.compile(
    r"\b(?:associated\s+press|ap\s+news|reuters|afp\b|bloomberg\s+wire|"
    r"press\s+release|pr\s+newswire|business\s*wire|via\s+\w+|"
    r"originally\s+(?:published|appeared)\s+(?:on|in|at))\b", re.I)


def parse_date(text: str) -> float | None:
    """Best-effort timestamp from a published/updated date string."""
    s = str(text or "").strip()
    if not s:
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s[:26].replace("Z", "+0000"), fmt)
            return dt.replace(tzinfo=dt.tzinfo or timezone.utc).timestamp()
        except ValueError:
            continue
    for pat in _DATE_PATTERNS:
        m = pat.search(s)
        if not m:
            continue
        try:
            if pat is _DATE_PATTERNS[2]:
                month = _MONTHS[m.group(1)[:3].lower()]
                return datetime(int(m.group(3)), month, int(m.group(2)),
                                tzinfo=timezone.utc).timestamp()
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                            tzinfo=timezone.utc).timestamp()
        except (ValueError, KeyError):
            continue
    return None


def text_fingerprint(text: str, shingle: int = 5) -> frozenset[int]:
    """Hashed word-shingle set — used to detect copied/duplicate content."""
    words = _WORD_RE.findall(str(text or "").lower())[:4000]
    if len(words) < shingle:
        return frozenset({hash(" ".join(words))}) if words else frozenset()
    out = set()
    for i in range(0, len(words) - shingle + 1, 2):
        h = hashlib.blake2b(" ".join(words[i:i + shingle]).encode(), digest_size=8)
        out.add(int.from_bytes(h.digest(), "big"))
    return frozenset(out)


def jaccard(a: frozenset[int], b: frozenset[int]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def freshness_score(
    published_at: str,
    retrieved_at: float,
    topics: list[str],
    *,
    now: float | None = None,
) -> float:
    """0..1 — how fresh the source is relative to what the topic needs.
    Undated sources score neutral-low rather than zero: plenty of good docs
    omit dates; penalize them via evidence_density instead."""
    importance = max((FRESHNESS_IMPORTANCE.get(t, 0.45) for t in topics), default=0.45)
    ts = parse_date(published_at)
    if ts is None:
        return 0.5
    age_days = max(0.0, ((now or time.time()) - ts) / 86400.0)
    # Half-life tuned by importance: news decays in days, history barely.
    half_life = 2.0 + (1.0 - importance) * 720.0
    recency = 0.5 ** (age_days / half_life)
    return round(0.4 + 0.6 * recency * importance + 0.3 * (1.0 - importance), 3)


def evidence_density(excerpt: str) -> float:
    """0..1 — does the page contain substantive information or thin filler?"""
    text = str(excerpt or "")
    if not text:
        return 0.0
    words = _WORD_RE.findall(text.lower())
    if len(words) < 40:
        return 0.15
    density = min(1.0, len(words) / 600.0)
    # Specificity bonus: numbers, versions, code, dates, technical tokens.
    specifics = len(_VERSION_RE.findall(text)) + len(re.findall(
        r"\b(?:\d{4}|[A-Z][a-z]+Error|\w+\(\)|`[^`]+`|§\s*\d+)\b", text))
    density += min(0.35, specifics * 0.05)
    # Keyword-stuffing penalty: one term > 8% of a long page is suspicious.
    if len(words) > 300:
        from collections import Counter
        top = Counter(w for w in words if len(w) > 3).most_common(1)
        if top and top[0][1] / len(words) > 0.08:
            density -= 0.3
    return round(max(0.0, min(1.0, density)), 3)


def content_farm_penalty(source: ResearchSource) -> float:
    """0..1 penalty for SEO/content-farm signals (Part 26)."""
    text = f"{source.title} {source.excerpt}".lower()
    penalty = 0.0
    if len(source.excerpt) < 300:
        penalty += 0.25
    if not source.published_at and not parse_date(source.excerpt[:400]):
        penalty += 0.15
    if re.search(r"\b(?:top\s+\d+\s+best|\d+\s+best\s+\w+\s+in\s+\d{4}|"
                 r"you\s+won'?t\s+believe|clickbait|sponsored\s+content)\b", text):
        penalty += 0.25
    # Templated keyword repetition of the query-shaped title.
    words = _WORD_RE.findall(text)
    if len(words) > 200:
        from collections import Counter
        top = Counter(w for w in words if len(w) > 4).most_common(1)
        if top and top[0][1] / len(words) > 0.07:
            penalty += 0.2
    return round(min(1.0, penalty), 3)


# ---------------------------------------------------------------------------
# Independence (Part 15/16/27)
# ---------------------------------------------------------------------------
class IndependenceAnalyzer:
    """Collapse dependent sources into evidence groups. Same domain,
    near-identical text, or shared wire/origin attribution → one group."""

    def analyze(self, sources: list[ResearchSource]) -> dict[str, int]:
        groups: dict[str, int] = {}
        fingerprints: dict[str, frozenset[int]] = {}
        group_of: list[str] = []
        for s in sources:
            fingerprints[s.id] = text_fingerprint(s.excerpt)
            groups[s.id] = -1
        next_group = 0
        for i, s in enumerate(sources):
            host = source_host(s)
            group_key = f"host:{host}" if host else f"solo:{s.id}"
            # Wire attribution shared across different domains → dependent.
            wire = bool(_WIRE_RE.search(s.excerpt[:1500]))
            if wire:
                group_key = f"wire:{s.metadata.get('wire_signature') or 'shared'}"
            assigned = groups.get(s.id, -1)
            if assigned < 0:
                assigned = next_group
                next_group += 1
            for j in range(i, len(sources)):
                other = sources[j]
                if groups.get(other.id, -1) >= 0 and j != i:
                    continue
                same_host = source_host(other) == host and host
                same_text = jaccard(fingerprints[s.id], fingerprints[other.id]) >= 0.35
                same_wire = wire and bool(_WIRE_RE.search(other.excerpt[:1500]))
                if j == i or same_host or same_text or same_wire:
                    groups[other.id] = assigned
                    other.metadata["independence_group"] = assigned
                    if j != i and same_text:
                        other.metadata["duplicate_of"] = s.id
                    elif j != i and same_wire:
                        other.metadata["wire_dependent"] = True
            s.metadata["independence_group"] = groups[s.id]
        return groups


def source_host(source: ResearchSource) -> str:
    url = str(source.url or "").lower()
    if "://" in url:
        url = url.split("://", 1)[1]
    return url.split("/", 1)[0].split(":", 1)[0].removeprefix("www.")


# ---------------------------------------------------------------------------
# Claims (Part 18/56)
# ---------------------------------------------------------------------------
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")


def _claim_key(sentence: str) -> frozenset[str]:
    return frozenset(w for w in _WORD_RE.findall(sentence.lower()) if len(w) > 3)


def extract_claims(
    sources: list[ResearchSource],
    query: str,
    *,
    max_claims: int = 20,
) -> list[dict[str, Any]]:
    """Map candidate factual claims → supporting/contradicting source ids.

    Heuristic: claim sentences are those containing query key terms or a
    concrete artifact (version, error code, number). Two claims sharing a
    key with opposing polarity are marked contradicting.
    """
    qterms = {w for w in _WORD_RE.findall(str(query or "").lower()) if len(w) > 3}
    claims: list[dict[str, Any]] = []
    seen_keys: dict[frozenset[str], int] = {}
    for source in sources:
        for sentence in _SENTENCE_SPLIT_RE.split(source.excerpt):
            sent = sentence.strip()
            if len(sent) < 30 or len(sent) > 400:
                continue
            words = set(_WORD_RE.findall(sent.lower()))
            shared = words & qterms
            has_artifact = bool(_VERSION_RE.search(sent) or
                                re.search(r"\b[A-Za-z_]*Error\b|\berror\s*\d+", sent))
            if len(shared) < 2 and not (has_artifact and shared):
                continue
            key = _claim_key(sent)
            negated = bool(_NEGATION_RE.search(sent))
            existing = seen_keys.get(key)
            if existing is not None:
                c = claims[existing]
                if negated:
                    c["contradicting"].append(source.id)
                else:
                    c["supporting"].append(source.id)
                continue
            idx = len(claims)
            seen_keys[key] = idx
            claims.append({
                "claim_id": f"c{idx}",
                "claim_text": sent[:280],
                "supporting": [source.id] if not negated else [],
                "contradicting": [source.id] if negated else [],
                "key_terms": sorted(shared)[:8],
                "confidence": "",
                "freshness": source.published_at or "",
            })
            if len(claims) >= max_claims:
                return claims
    return claims


# ---------------------------------------------------------------------------
# Conflicts (Part 19/50)
# ---------------------------------------------------------------------------
class ConflictDetector:
    """Surface credible disagreement — never pick a side silently."""

    def detect(
        self,
        sources: list[ResearchSource],
        claims: list[dict[str, Any]],
        query: str,
    ) -> list[dict[str, Any]]:
        conflicts: list[dict[str, Any]] = []

        # 1) Claims with both supporting and contradicting sources.
        for claim in claims:
            if claim["supporting"] and claim["contradicting"]:
                conflicts.append({
                    "kind": "contradiction",
                    "claim": claim["claim_text"],
                    "supporting": claim["supporting"],
                    "contradicting": claim["contradicting"],
                })
                claim["confidence"] = "conflicted"

        # 2) "Latest/current version" disagreement across sources.
        if _CURRENT_CLAIM_RE.search(str(query or "")):
            reported: dict[str, list[str]] = {}
            for s in sources:
                for sent in _SENTENCE_SPLIT_RE.split(s.excerpt):
                    if not _CURRENT_CLAIM_RE.search(sent):
                        continue
                    for v in _VERSION_RE.findall(sent):
                        reported.setdefault(v, []).append(s.id)
            distinct = sorted(reported)
            if len(distinct) > 1:
                conflicts.append({
                    "kind": "version_disagreement",
                    "claim": "reported current version",
                    "versions": {v: ids for v, ids in reported.items()},
                })

        # 3) Same-domain newer vs older material → mark stale source.
        by_host: dict[str, list[ResearchSource]] = {}
        for s in sources:
            ts = parse_date(s.published_at)
            if ts:
                by_host.setdefault(source_host(s), []).append(s)
        for host, rows in by_host.items():
            if len(rows) < 2:
                continue
            dated = sorted(rows, key=lambda s: parse_date(s.published_at) or 0)
            newest = parse_date(dated[-1].published_at) or 0
            for s in dated[:-1]:
                ts = parse_date(s.published_at) or 0
                if newest - ts > 180 * 86400:
                    s.metadata["outdated"] = True
                    conflicts.append({
                        "kind": "outdated",
                        "source_id": s.id,
                        "detail": f"older {host} material superseded by newer {host} page",
                    })
        return conflicts


# ---------------------------------------------------------------------------
# Corroboration + confidence (Part 17/32)
# ---------------------------------------------------------------------------
_RELEVANCE_FLOOR = 6.0  # ~20% of query terms — junk homepages don't corroborate


def _topically_relevant(sources: list[ResearchSource]) -> list[ResearchSource]:
    """Drop sources whose fetched content doesn't address the query.

    A page must actually be about the question to corroborate anything —
    six unrelated news homepages are not six independent confirmations.
    Sources without a relevance score (locally injected evidence) are
    not filtered: the floor applies to web-scored material only."""
    out: list[ResearchSource] = []
    for s in sources:
        comp = s.score_components or {}
        if "relevance" in comp and float(comp.get("relevance") or 0) < _RELEVANCE_FLOOR:
            continue
        out.append(s)
    return out


def corroboration_state(
    sources: list[ResearchSource],
    claims: list[dict[str, Any]],
    conflicts: list[dict[str, Any]],
    profiles: dict[str, Any],
) -> str:
    if conflicts:
        return CONFLICTED
    sources = _topically_relevant(sources)
    if not sources:
        return UNVERIFIED
    groups = {s.metadata.get("independence_group", i) for i, s in enumerate(sources)}
    classes = {getattr(profiles.get(s.id), "category", "") for s in sources}
    primary_ids = {
        s.id for s in sources
        if getattr(profiles.get(s.id), "category", "") in _STRONG_CLASSES
        or getattr(profiles.get(s.id), "primary_source", False)
    }
    # Independent groups supporting the top claim.
    best_support = 0
    group_of = {s.id: s.metadata.get("independence_group") for s in sources}
    for claim in claims:
        sup_groups = {
            group_of[sid] for sid in claim["supporting"] if sid in group_of
        }
        best_support = max(best_support, len(sup_groups))
    n_primary_groups = len({s.metadata.get("independence_group") for s in sources
                            if s.id in primary_ids})
    if n_primary_groups >= 2:
        return MULTIPLE_PRIMARY
    if n_primary_groups == 1:
        if best_support >= 2 or len(groups) >= 2:
            return PRIMARY_PLUS_SECONDARY
        return SINGLE_PRIMARY
    if best_support >= 2 and not classes <= _WEAK_CLASSES:
        return MULTIPLE_INDEPENDENT_SECONDARY
    if classes <= _WEAK_CLASSES or classes <= {
        trust_mod.FORUM, trust_mod.COMMUNITY_HIGH_SIGNAL,
        trust_mod.BLOG, trust_mod.UNKNOWN,
    }:
        return COMMUNITY_ONLY if len(groups) >= 2 else UNVERIFIED
    if best_support >= 2 or len(groups) >= 2:
        return MULTIPLE_INDEPENDENT_SECONDARY
    return UNVERIFIED


def evidence_confidence(
    sources: list[ResearchSource],
    claims: list[dict[str, Any]],
    conflicts: list[dict[str, Any]],
    profiles: dict[str, Any],
    *,
    high_stakes: bool = False,
) -> tuple[str, list[str]]:
    """Session-level confidence — separate from per-source scores."""
    sources = _topically_relevant(sources)
    state = corroboration_state(sources, claims, conflicts, profiles)
    reasons: list[str] = []
    if state == CONFLICTED:
        return CONF_CONF, ["credible sources disagree"]
    groups = {s.metadata.get("independence_group", i) for i, s in enumerate(sources)}
    strong_ids = {
        s.id for s in sources
        if getattr(profiles.get(s.id), "category", "") in _STRONG_CLASSES
        or getattr(profiles.get(s.id), "primary_source", False)
    }
    if high_stakes:
        # Medical/legal/financial: require primary-class evidence.
        if state in {MULTIPLE_PRIMARY, PRIMARY_PLUS_SECONDARY}:
            reasons.append("primary/official evidence present")
            return HIGH, reasons
        if state == SINGLE_PRIMARY or strong_ids:
            reasons.append("single primary/strong source — corroborate before acting")
            return MODERATE, reasons
        reasons.append("no primary/official evidence — high-stakes topic")
        return LOW, reasons
    if state in {MULTIPLE_PRIMARY, PRIMARY_PLUS_SECONDARY}:
        reasons.append("primary source with corroboration")
        return HIGH, reasons
    if state == SINGLE_PRIMARY:
        reasons.append("one authoritative source")
        return MODERATE, reasons
    if state == MULTIPLE_INDEPENDENT_SECONDARY:
        reasons.append(f"{len(groups)} independent credible sources agree")
        return MODERATE, reasons
    if state == COMMUNITY_ONLY:
        reasons.append("community reports only — not officially confirmed")
        return LOW, reasons
    reasons.append("insufficient independent evidence")
    return LOW, reasons


def reliability_label(
    source: ResearchSource,
    profile: Any,
    *,
    conflicts: list[dict[str, Any]] | None = None,
) -> str:
    """Per-source label for UI badges (Part 31)."""
    if source.reliability == REL_BLOCKED or source.score <= -500:
        return REL_BLOCKED
    conflicts = conflicts or []
    for c in conflicts:
        if source.id in c.get("supporting", []) + c.get("contradicting", []):
            return REL_CONFLICTED
    if source.metadata.get("outdated") or source.version_relevance == "mismatch":
        return REL_WEAK
    cls = getattr(profile, "category", "")
    if cls in _STRONG_CLASSES or getattr(profile, "primary_source", False):
        if source.version_relevance in {"exact", "compatible"} or not source.version_relevance:
            return REL_PRIMARY_CONFIRMED
        return REL_STRONG
    if cls == trust_mod.COMMUNITY_HIGH_SIGNAL:
        return REL_COMMUNITY
    if cls in _WEAK_CLASSES:
        return REL_UNVERIFIED if cls != trust_mod.FORUM else REL_WEAK
    return REL_SUPPORTED


def source_badges(
    source: ResearchSource,
    profile: Any,
    topics: list[str],
) -> list[str]:
    """Compact badge list for UI source cards (Part 36)."""
    badges: list[str] = []
    cls = getattr(profile, "category", "")
    label = trust_mod.CLASS_LABEL.get(cls, "")
    if getattr(profile, "primary_source", False):
        badges.append("PRIMARY")
    if label and label != "UNKNOWN":
        badges.append(label)
    if getattr(profile, "preprint", False):
        badges.append("PREPRINT")
    if source.version_relevance == "exact":
        badges.append("EXACT VERSION")
    elif source.version_relevance in {"mismatch", "conflict"}:
        badges.append("VERSION MISMATCH")
    if source.metadata.get("outdated"):
        badges.append("OUTDATED")
    if source.metadata.get("duplicate_of"):
        badges.append("DUPLICATE")
    if source.metadata.get("wire_dependent"):
        badges.append("WIRE COPY")
    if source.published_at:
        ts = parse_date(source.published_at)
        if ts and time.time() - ts < 30 * 86400:
            badges.append("CURRENT")
    return badges[:5]


@dataclass
class EvidenceReport:
    """Session-level evidence summary stored on the research session."""

    confidence: str = LOW
    confidence_reasons: list[str] = field(default_factory=list)
    corroboration: str = UNVERIFIED
    independent_groups: int = 0
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    claims: list[dict[str, Any]] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    high_stakes: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "confidence": self.confidence,
            "confidence_reasons": self.confidence_reasons,
            "corroboration": self.corroboration,
            "independent_groups": self.independent_groups,
            "conflicts": self.conflicts[:8],
            "claims": self.claims[:20],
            "topics": self.topics,
            "high_stakes": self.high_stakes,
        }
