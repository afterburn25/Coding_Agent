"""Reference and pronoun resolution against active context.

"her", "the woman", "that image", "the branch", "the error" resolve
against the active task first, then recent entities — recency plus
semantic compatibility, never an unrelated older memory.
"""
from __future__ import annotations

import re
from typing import Any

# Referring expressions grouped by the kind of entity they can bind.
REFERENCE_TERMS: dict[str, tuple[str, ...]] = {
    "person": (
        "her", "him", "she", "he", "they", "them", "the woman", "the girl",
        "the man", "the guy", "that woman", "that girl", "same girl",
        "same woman", "same guy", "same person", "the person",
        "the character", "that character", "his", "hers", "their",
    ),
    "image": (
        "the image", "that image", "this image", "the picture",
        "that picture", "the photo", "that photo", "that one",
        "the same", "the previous image", "the last image",
        "previous image", "last image", "that render",
        "what you just made", "the one you just made",
    ),
    "artifact": (
        "the app", "the project", "the repository", "the repo",
        "that repo", "the branch", "that branch", "the build",
        "the function", "that function", "the code", "the file",
        "that file", "the model", "that model", "the page",
        "that page", "the setting", "that setting", "the commit",
        "that commit", "the response", "that response",
        "what we were working on", "what we were talking about",
        "the thing we discussed", "that thing we discussed",
    ),
    "error": (
        "the error", "that error", "the bug", "that bug", "the failure",
        "that failure", "the issue", "that issue", "it broke",
        "the problem", "that problem", "what broke", "what just broke",
    ),
    "ordinal": (
        "the first one", "the second one", "the third one",
        "the other one", "the previous one", "the last one",
        "the next one", "another one", "the earlier one",
        "the one from earlier", "the one you just mentioned",
        "the other version", "the other model", "the other option",
        "the first option", "the second option", "the third option",
        "the other image", "the other file", "the other branch",
        "the rest", "the middle one", "not that one",
        "yesterday's option", "the earlier option",
    ),
}

# Bare pronouns resolve by ACTIVE DOMAIN, not by nearest noun — "fix it"
# binds the active error; "make it darker" binds the active image.
_BARE_PRONOUN_RE = re.compile(
    r"\b(it|that|this|them|they|those)\b", re.IGNORECASE)

# Verb hints that disambiguate which domain a bare pronoun points at.
_VERB_DOMAIN = (
    (re.compile(r"\b(?:fix|repair|debug|solve|resolve|patch|unbreak|"
                r"diagnose|investigate)\b", re.I), "error"),
    (re.compile(r"\b(?:darker|brighter|bigger|smaller|wider|closer|"
                r"blonde|red|blue|full\s+body|zoom|crop|background|"
                r"make|change|add|remove|edit|regenerate|redraw|"
                r"recreate|upscale|enhance)\b", re.I), "image"),
    (re.compile(r"\b(?:push|commit|deploy|merge|rebase|checkout|"
                r"delete|rename|move|open|run|restart|stop|start|"
                r"install|update|revert|undo)\b", re.I), "artifact"),
)

_TERM_TO_KIND: list[tuple[re.Pattern, str]] = [
    (re.compile(rf"\b{re.escape(term)}\b", re.IGNORECASE), kind)
    for kind, terms in REFERENCE_TERMS.items()
    for term in terms
]


def find_references(text: str) -> list[tuple[str, str]]:
    """All referring expressions in text as [(term, entity_kind)]."""
    t = str(text or "")
    return [(m.group(0), kind)
            for pat, kind in _TERM_TO_KIND
            if (m := pat.search(t))]


def resolve_references(text: str, active: Any) -> dict[str, str]:
    """Map referring expressions to concrete entities in `active`.

    Returns {matched_term: resolved_label} for the trace. Ambiguity
    returns the term unresolved — the caller decides whether to ask.
    """
    if active is None:
        return {}
    resolved: dict[str, str] = {}
    for term, kind in find_references(text):
        label = ""
        if kind == "person":
            label = getattr(active, "active_image_subject", "") or ""
        elif kind == "image":
            label = getattr(active, "active_image_subject", "") or \
                getattr(active, "active_image_prompt", "") or ""
            if not label:
                recent = getattr(active, "entities", lambda: [])()
                img = next(
                    (e for e in recent if e.get("kind") == "image"), None)
                label = str((img or {}).get("label") or "")
        elif kind == "error":
            label = getattr(active, "active_error", "") or \
                getattr(active, "last_error", "") or ""
        elif kind == "artifact":
            label = getattr(active, "active_project", "") or \
                getattr(active, "active_artifact", "") or ""
        elif kind == "ordinal":
            # Ordinals resolve to the entity list — the caller maps
            # first/second/other onto recency order.
            recent = getattr(active, "entities", lambda: [])()
            if recent:
                idx = {"first": 0, "last": -1, "previous": 0,
                       "second": 1, "third": 2, "other": 1,
                       "next": 0, "another": 1}.get(
                    re.sub(r"^(?:the\s+)?", "", term.lower())
                    .replace(" one", ""), 0)
                try:
                    label = str(recent[idx].get("label") or "")
                except (IndexError, KeyError, TypeError):
                    label = ""
        if label:
            resolved[term] = str(label)[:160]
    return resolved


def _domain_label(active: Any, domain: str) -> str:
    if domain == "image":
        return getattr(active, "active_image_subject", "") or \
            getattr(active, "active_image_prompt", "") or ""
    if domain == "error":
        return getattr(active, "active_error", "") or \
            getattr(active, "last_error", "") or ""
    if domain == "artifact":
        return getattr(active, "active_project", "") or \
            getattr(active, "active_artifact", "") or \
            getattr(active, "active_subject", "") or ""
    return ""


def resolve_with_report(text: str, active: Any) -> dict[str, Any]:
    """Rich resolution — typed terms plus bare pronouns bound by active
    domain. Returns {"resolved": {term: label}, "ambiguous": [term]}.

    A bare pronoun only binds when exactly one live domain matches —
    two plausible antecedents mean ASK, never guess.
    """
    report: dict[str, Any] = {"resolved": {}, "ambiguous": []}
    if active is None:
        return report
    report["resolved"] = resolve_references(text, active)
    already = {t.lower() for t in report["resolved"]}
    t = str(text or "")
    pronouns = [m.group(0) for m in _BARE_PRONOUN_RE.finditer(t)
                if m.group(0).lower() not in already]
    if not pronouns:
        return report
    hint = next((dom for pat, dom in _VERB_DOMAIN if pat.search(t)), "")
    candidates: dict[str, str] = {}
    for dom in ("error", "image", "artifact"):
        label = _domain_label(active, dom)
        if label:
            candidates[dom] = label
    image_live = bool(getattr(active, "image_active", lambda **k: False)())
    for pron in pronouns:
        if hint and candidates.get(hint):
            report["resolved"][pron] = candidates[hint]
            continue
        live = {d: l for d, l in candidates.items()
                if d != "image" or image_live}
        if len(live) == 1:
            report["resolved"][pron] = next(iter(live.values()))
        elif len(live) > 1:
            report["ambiguous"].append(pron)
        # zero candidates: unbound, not ambiguous — nothing to bind to
    return report
