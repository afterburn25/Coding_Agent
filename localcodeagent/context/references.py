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
    r"\b(it|that|this|them|they|those|she|he|him|her)\b", re.IGNORECASE)

# 'her voice', 'the installer' — possessive/article + noun resolved
# against the entity graph's aliases before domain defaults apply.
_NOUN_REF_RE = re.compile(
    r"\b(the|that|this|her|his|your|my|our|their)\s+"
    r"([a-z][\w.-]{1,30})\b", re.IGNORECASE)

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


# Entity-graph resolution — the second stage. Verb/pronoun domains map
# onto entity *types* extracted by state.extract_entities, then ranked
# by salience × recency. Two live candidates within the margin are
# ambiguous: ask, never guess.
_DOMAIN_TYPES: dict[str, tuple[str, ...]] = {
    "error": ("error",),
    "image": ("image",),
    "artifact": ("artifact", "project", "file", "version", "lib",
                 "runtime", "app", "system", "tool", "mission"),
    "person": ("person", "voice", "agent"),
    "model": ("model",),
    "voice": ("voice", "engine"),
}

# Closeness in salience that still counts as "can't tell them apart".
_AMBIGUITY_MARGIN = 0.18
# A referent never resolves below this combined score.
_RESOLVE_FLOOR = 0.30


def _entity_candidates(active: Any, domain: str,
                       *, now: float | None = None) -> list[dict]:
    """Salience-ranked graph entities compatible with the verb domain."""
    import time as _t
    now = _t.time() if now is None else now
    graph = getattr(active, "entity_graph", None) or {}
    types = _DOMAIN_TYPES.get(domain, ())
    rows = []
    for row in graph.values():
        if types and row.get("type") not in types:
            continue
        age_h = max(0.0, (now - float(row.get("last_ts") or now)) / 3600)
        recency = max(0.0, 1.0 - age_h / 96.0)  # ~4 day halving window
        score = float(row.get("salience", 0)) * 0.6 + recency * 0.4
        rows.append({**row, "score": round(score, 3)})
    rows.sort(key=lambda r: (-r["score"],
                             -float(r.get("last_ts") or 0)))
    return rows


def _entity_resolve(active: Any, domain: str,
                    now: float | None = None) -> tuple[str, bool]:
    """(label, ambiguous) — empty label means no candidate."""
    cands = _entity_candidates(active, domain, now=now)
    if not cands:
        return "", False
    if len(cands) == 1:
        return str(cands[0].get("label") or ""), False
    top, nxt = cands[0], cands[1]
    if top["score"] >= _RESOLVE_FLOOR and \
            top["score"] - nxt["score"] <= _AMBIGUITY_MARGIN:
        return str(top.get("label") or ""), True
    if top["score"] < _RESOLVE_FLOOR:
        return "", False
    return str(top.get("label") or ""), False


def resolve_with_report(text: str, active: Any) -> dict[str, Any]:
    """Rich resolution — typed terms plus bare pronouns bound by active
    domain first, then by the entity graph. Returns
    {"resolved": {term: label}, "ambiguous": [term]}.

    A bare pronoun only binds when one candidate clearly dominates —
    two plausible antecedents mean ASK, never guess.
    """
    report: dict[str, Any] = {"resolved": {}, "ambiguous": []}
    if active is None:
        return report
    report["resolved"] = resolve_references(text, active)
    already = {t.lower() for t in report["resolved"]}
    t = str(text or "")
    # Alias pass — 'her voice' / 'the installer' bind a graph entity by
    # alias or label, no string-matching every historical mention.
    for m in _NOUN_REF_RE.finditer(t):
        phrase = m.group(0).strip()
        if phrase.lower() in already:
            continue
        row = (graph_entity_for(active, phrase)
               or graph_entity_for(active, m.group(2)))
        if row is not None:
            report["resolved"][phrase] = str(row.get("label") or phrase)
            already.add(phrase.lower())
    # Open-loop recall — 'what happened with that?', 'any update on it'
    # resolve the pronoun to the pending loop, not the last noun.
    if open_loops := [l for l in
                      (getattr(active, "open_loops", None) or [])
                      if l.get("status") == "open"]:
        if re.search(r"\bwhat\s+happened\s+(?:with|to)|\bany\s+"
                     r"(?:update|news|progress)\b|\bstatus\s+of\b",
                     t, re.I):
            for m in _BARE_PRONOUN_RE.finditer(t):
                p = m.group(0)
                if p.lower() not in already:
                    loop = open_loops[-1]
                    row = (getattr(active, "entity_graph", None)
                           or {}).get(str(loop.get("entity") or ""))
                    report["resolved"][p] = (
                        str(row.get("label")) if row else
                        str(loop.get("text") or ""))[:160]
                    already.add(p.lower())
    # Ordinal fallback — 'the second one'/'the other one' against the
    # graph when the flat recent-entity list didn't cover it.
    for term, kind in find_references(t):
        if kind != "ordinal" or term in report["resolved"]:
            continue
        label = _ordinal_from_graph(term, active)
        if label:
            report["resolved"][term] = label
    pronouns: list[str] = []
    for m in _BARE_PRONOUN_RE.finditer(t):
        p = m.group(0).lower()
        if p in already:
            continue
        # 'her voice' — possessive, not a bare pronoun; the alias pass
        # above already handled it.
        if p in ("her", "his", "their") and re.match(
                r"\s+\w", t[m.end():]):
            continue
        # Expletive/dummy 'it' — 'what time is it', 'it's raining',
        # 'how is it going' — has no referent; never bind.
        if p == "it" and re.search(
                r"\b(?:time|date|day|weather|raining|cold|hot|late|"
                r"early|going|means?|seems?|looks?|sounds?|feels?|"
                r"works?|worth|possible|ok(?:ay)?|fine|done|true|"
                r"clear|safe|legal|allowed|hard|easy)\b",
                t, re.I) and re.search(
                r"\b(?:is|was|does|do|did|what|how|why|where|when|"
                r"'s|s)\b", t, re.I):
            continue
        pronouns.append(m.group(0))
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
            continue
        if len(live) > 1:
            report["ambiguous"].append(pron)
            continue
        # No active-domain label — try the entity graph. The verb hint
        # picks the compatible type; salience × recency ranks.
        dom = hint or (
            "person" if pron.lower() in ("she", "he", "him", "her",
                                         "they", "them")
            else "artifact")
        label, amb = _entity_resolve(active, dom)
        if label and not amb:
            report["resolved"][pron] = label
        elif amb:
            report["ambiguous"].append(pron)
    return report


_ORDINAL_INDEX = {"first": 0, "second": 1, "third": 2, "fourth": 3,
                  "fifth": 4, "last": -1, "previous": -2, "earlier": -2,
                  "middle": -2}


def _ordinal_from_graph(term: str, active: Any) -> str:
    """Ordinal/'other' referents against graph entities ordered by
    last mention — 'the other one' is the same-type entity that ISN'T
    the last bound referent."""
    graph = getattr(active, "entity_graph", None) or {}
    if not graph:
        return ""
    t = term.lower()
    m = re.search(r"\b(first|second|third|fourth|fifth|last|previous|"
                  r"earlier|middle|other|another|next)\b", t)
    if not m:
        return ""
    ord_word = m.group(1)
    # Same-type context: if a prior referent exists, prefer entities
    # of that referent's type.
    bound = getattr(active, "referents", None) or {}
    same_type = ""
    for v in bound.values():
        if isinstance(v, str) and v in graph:
            same_type = graph[v].get("type", "")
    rows = sorted(graph.values(),
                  key=lambda r: float(r.get("last_ts") or 0))
    if same_type:
        typed = [r for r in rows if r.get("type") == same_type]
        if len(typed) >= 2:
            rows = typed
    if not rows:
        return ""
    if ord_word in ("other", "another"):
        # the one that ISN'T the most recent same-type mention
        label = str(rows[-1].get("label") or "")
        for v in bound.values():
            if isinstance(v, str) and v in graph and \
                    graph[v].get("label") == label and len(rows) >= 2:
                label = str(rows[-2].get("label") or "")
                break
        return label
    idx = _ORDINAL_INDEX.get(ord_word, 0)
    try:
        return str(rows[idx].get("label") or "")
    except IndexError:
        return ""


def graph_entity_for(active: Any, label_or_alias: str) -> dict | None:
    """Look up an entity by label/alias — used by 'back to X' returns
    and decision recall. Exact first, then containment either way so
    'the ai community idea' still lands on Moltbook."""
    graph = getattr(active, "entity_graph", None) or {}
    low = str(label_or_alias or "").lower().strip()
    if not low:
        return None
    for row in graph.values():
        if row.get("label", "").lower() == low:
            return row
        if low in [a.lower() for a in (row.get("aliases") or [])]:
            return row
    # Containment — 'ai community idea' contains alias 'ai community'.
    best = None
    for row in graph.values():
        cands = [row.get("label", "")] + list(row.get("aliases") or [])
        for c in cands:
            cl = c.lower().strip()
            if len(cl) >= 4 and (cl in low or low in cl):
                if best is None or len(cl) > len(
                        str(best.get("_hit", ""))):
                    best = {**row, "_hit": cl}
    return best
