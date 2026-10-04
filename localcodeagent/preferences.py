"""Learned Preferences — structured overlay built from user corrections.

A correction like "stop asking whether to continue" or "always test the
installer first" becomes a *candidate* rule — not a silent mutation of
behavior. Rules accrue evidence; a rule only goes live once it repeats
(``confidence`` crosses the activation threshold) or the user activates
it explicitly. Every rule is inspectable, scoped (global / project /
profile), editable, and forgettable.

Active rules surface to prompts through ``overlay_text`` — a bounded
instruction block, never raw conversation history.
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .fsutil import atomic_write_text

MAX_RULES = 200
ACTIVATE_AT = 0.6        # confidence needed to go live
_EVIDENCE_CAP = 6

# Explicit meta-directives — phrases about how Nexus should behave, not
# task content. Deliberately narrow: ambiguity stays a candidate.
_DIRECTIVES = tuple(
    re.compile(p, re.I) for p in (
        r"\balways\s+\S",
        r"\bnever\s+\S",
        r"\bfrom now on\b",
        r"\bstop (asking|showing|doing|adding|using|saying)\b",
        r"\bdon'?t (ask|add|use|do|show|include)\b",
        r"\buse (shorter|longer|more|less|fewer)\b",
        r"\bprefer\s+\S",
        r"\bjust (continue|do it|proceed|go ahead)\b",
        r"\bkeep (responses|answers|replies)\b",
        r"\bno (need|more)\s+to\s+(ask|confirm)\b",
    ))

_SCOPE_PROJECT = re.compile(
    r"\b(this|the)\s+(project|repo|repository|codebase)\b", re.I)

# Corrections are behavioral overlays, not authority changes. Anything that
# tries to learn around identity, safety, permissions, or credentials is
# rejected instead of becoming a prompt rule.
_BLOCKED_DIRECTIVES = tuple(
    re.compile(p, re.I) for p in (
        r"\b(?:ignore|bypass|disable|skip|override|remove)\b.{0,80}"
        r"\b(?:permission|approval|safety|policy|auth|verification|"
        r"creator|lock|identity)\b",
        r"\b(?:permission|approval|safety|policy|auth|verification|"
        r"creator|lock|identity)\b.{0,80}"
        r"\b(?:ignore|bypass|disable|skip|without)\b",
        r"\b(?:never|don'?t|do not|stop)\b.{0,60}\b(?:ask|request|require)\b"
        r".{0,60}\b(?:approval|permission)\b",
        r"\b(?:always|never)\b.{0,60}\b(?:without|skip)\b.{0,60}"
        r"\b(?:approval|permission|verification|authentication)\b",
        r"\b(?:reveal|show|print|log|store|remember)\b.{0,60}"
        r"\b(?:secret|password|passcode|token|credential|api[-_ ]?key)\b",
        r"\b(?:call|name|rename)\s+(?:yourself|you)\b",
        r"\byour\s+(?:name|identity|birthday|birth\s*date|age|"
        r"creator|father|dad|daddy)\b",
        r"\byou\s+(?:are|were)\s+(?:born|created|made|built)\b",
        r"\bclaim\s+(?:to\s+be\s+)?conscious\b",
    ))


def _canonical(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().lower())[:300]


def _clean_scope(scope: str) -> str:
    """Scopes are deliberately structured — global, profile:<id>, or
    project:<id>. No arbitrary prompt scopes or multiline payloads."""
    s = str(scope or "global").strip()
    if s == "global":
        return s
    kind, _, ident = s.partition(":")
    if (kind in {"profile", "project"} and 0 < len(ident) <= 160
            and "\r" not in ident and "\n" not in ident):
        return f"{kind}:{ident}"
    raise ValueError("scope must be global, profile:<id>, or project:<id>")


def _blocked_reason(text: str) -> str:
    """Reason a correction cannot be learned, or "" when it is allowed."""
    from .answer_memory.validation import contains_secret
    from .identity import locked_topic, locked_refusal
    if contains_secret(text):
        return "refusing to persist a possible secret"
    topic = locked_topic(text)
    if topic:
        return locked_refusal(topic)
    if any(p.search(text) for p in _BLOCKED_DIRECTIVES):
        return ("cannot learn a rule that overrides permissions, safety, "
                "secrets, or Nexus identity")
    return ""


class PreferenceStore:
    """Durable, inspectable learned-preference store."""

    def __init__(self, data_dir: Path) -> None:
        self.root = Path(data_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self._path = self.root / "preferences.json"
        self._lock = threading.RLock()
        self._rules: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self._rules = [r for r in raw.get("rules", [])
                           if isinstance(r, dict)]
        except (OSError, ValueError):
            self._rules = []

    def _save(self) -> None:
        atomic_write_text(self._path, json.dumps(
            {"version": 1, "rules": self._rules}, indent=2))

    # -- capture ---------------------------------------------------------

    def detect(self, text: str) -> str | None:
        """Return the directive text when `text` carries an explicit
        behavior correction, else None. Read-only — no side effects."""
        t = str(text or "").strip()
        if not t or len(t) > 1200:
            return None
        for pat in _DIRECTIVES:
            m = pat.search(t)
            if m:
                # Keep the directive + a little trailing context.
                return t[max(0, m.start() - 10):m.end() + 160][:300]
        return None

    def observe(self, text: str, *, profile_id: str = "",
                project_id: str = "") -> dict | None:
        """Capture a correction candidate from user text. Repeated
        observations raise confidence; first sightings stay inactive."""
        directive = self.detect(text)
        if directive is None:
            return None
        blocked = _blocked_reason(directive) or _blocked_reason(text)
        if blocked:
            return {"id": "", "rule": directive, "scope": "",
                    "active": False, "blocked": blocked,
                    "source": "user_correction"}
        canon = _canonical(directive)
        scope = "global"
        if project_id and _SCOPE_PROJECT.search(text):
            scope = f"project:{project_id}"
        elif profile_id:
            scope = f"profile:{profile_id}"
        with self._lock:
            for rule in self._rules:
                if rule.get("canonical") == canon and \
                        rule.get("scope") == scope:
                    rule["count"] = int(rule.get("count") or 0) + 1
                    ev = rule.setdefault("evidence", [])
                    ev.append(str(text)[:300])
                    del ev[:-_EVIDENCE_CAP]
                    rule["confidence"] = min(1.0, 0.4 + 0.2 * rule["count"])
                    rule["updated_at"] = time.time()
                    if (not rule.get("active")
                            and rule["confidence"] >= ACTIVATE_AT):
                        rule["active"] = True
                        rule["activated_at"] = time.time()
                    self._save()
                    return dict(rule)
            rule = {
                "id": f"pref-{uuid.uuid4().hex[:10]}",
                "rule": directive, "canonical": canon,
                "scope": scope, "active": False,
                "confidence": 0.4, "count": 1,
                "source": "user_correction",
                "evidence": [str(text)[:300]],
                "created_at": time.time(), "updated_at": time.time(),
            }
            self._rules.append(rule)
            del self._rules[:-MAX_RULES]
            self._save()
            return dict(rule)

    # -- management --------------------------------------------------------

    def add(self, rule: str, *, scope: str = "global",
            active: bool = True, source: str = "manual") -> dict:
        text = str(rule or "").strip()[:300]
        if not text:
            raise ValueError("rule is required")
        blocked = _blocked_reason(text)
        if blocked:
            raise ValueError(blocked)
        scope = _clean_scope(scope)
        canon = _canonical(text)
        with self._lock:
            for row in self._rules:
                if (row.get("canonical") == canon
                        and row.get("scope") == scope):
                    row["active"] = bool(active)
                    row["confidence"] = 1.0
                    row["source"] = source
                    row["updated_at"] = time.time()
                    self._save()
                    return dict(row)
            row = {"id": f"pref-{uuid.uuid4().hex[:10]}",
                   "rule": text, "canonical": canon,
                   "scope": scope, "active": bool(active),
                   "confidence": 1.0, "count": 1, "source": source,
                   "evidence": [], "created_at": time.time(),
                   "updated_at": time.time()}
            self._rules.append(row)
            del self._rules[:-MAX_RULES]
            self._save()
            return dict(row)

    def update(self, rule_id: str, *, rule: str | None = None,
               scope: str | None = None,
               active: bool | None = None) -> dict | None:
        """Edit text/scope/active state. Duplicate canonical+scope edits
        conflict instead of silently merging two rules."""
        with self._lock:
            target = None
            for row in self._rules:
                if row.get("id") == rule_id:
                    target = row
                    break
            if target is None:
                return None
            new_rule = target.get("rule")
            if rule is not None:
                new_rule = str(rule or "").strip()[:300]
                if not new_rule:
                    raise ValueError("rule is required")
                blocked = _blocked_reason(new_rule)
                if blocked:
                    raise ValueError(blocked)
            new_scope = (target.get("scope") if scope is None
                         else _clean_scope(scope))
            canon = _canonical(str(new_rule))
            for row in self._rules:
                if (row is not target and row.get("canonical") == canon
                        and row.get("scope") == new_scope):
                    raise ValueError(
                        "conflicts with an existing learned preference")
            target["rule"] = new_rule
            target["canonical"] = canon
            target["scope"] = new_scope
            if active is not None:
                target["active"] = bool(active)
                if active:
                    target["confidence"] = max(
                        float(target.get("confidence") or 0.0), ACTIVATE_AT)
            target["updated_at"] = time.time()
            self._save()
            return dict(target)

    def list(self, *, scope: str = "") -> list[dict]:
        with self._lock:
            rows = [dict(r) for r in self._rules]
        if scope:
            rows = [r for r in rows if str(r.get("scope")) == scope]
        return sorted(rows, key=lambda r: r.get("updated_at") or 0,
                      reverse=True)

    def set_active(self, rule_id: str, active: bool) -> bool:
        with self._lock:
            for r in self._rules:
                if r.get("id") == rule_id:
                    r["active"] = bool(active)
                    r["updated_at"] = time.time()
                    self._save()
                    return True
        return False

    def forget(self, rule_id: str) -> bool:
        with self._lock:
            before = len(self._rules)
            self._rules = [r for r in self._rules if r.get("id") != rule_id]
            if len(self._rules) != before:
                self._save()
                return True
        return False

    # -- prompt overlay ------------------------------------------------------

    def overlay_text(self, *, profile_id: str = "", project_id: str = "",
                     limit: int = 12) -> str:
        """Active rules visible to this profile/project as a bounded
        instruction block for prompt injection."""
        scopes = {"global"}
        if profile_id:
            scopes.add(f"profile:{profile_id}")
        if project_id:
            scopes.add(f"project:{project_id}")
        lines = [str(r.get("rule")) for r in self._rules
                 if r.get("active") and r.get("scope") in scopes][:limit]
        if not lines:
            return ""
        return ("Learned user preferences (apply unless contradicted; never "
                "override identity, permissions, safety, or factual "
                "correctness):\n"
                + "\n".join(f"- {l}" for l in lines))
