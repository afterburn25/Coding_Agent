from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any


RECALL_EXPRESSION_STYLES = (
    "Integrate the remembered fact naturally into the answer; avoid leading with 'you told me' unless that framing is useful.",
    "Use a concise paraphrase with a different sentence opening and sentence structure from recent replies.",
    "Frame the remembered information as a natural conversational reminder rather than reciting a stored note.",
    "Express the implication of the remembered fact in context instead of echoing its stored wording.",
    "Answer directly while changing both vocabulary and syntax from the canonical memory text.",
    "Weave the fact into the current topic and avoid phrasing used in recent assistant messages.",
    "Use a short, natural rewording; preserve factual values but not the surrounding sentence.",
    "Prefer an indirect, context-aware reference when that sounds more natural than restating the whole fact.",
)


class ConversationMemory:
    """Persistent local chat memory and conversational training notes.

    Memory changes prompts immediately. Weight training remains an explicit offline
    step so active conversations never mutate model weights unpredictably.
    """

    def __init__(
        self,
        path: Path,
        *,
        enabled: bool = True,
        history_limit: int = 200,
        rule_limit: int = 300,
        fact_limit: int = 500,
        training_limit: int = 500,
    ) -> None:
        self.path = path.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.enabled = bool(enabled)
        self.history_limit = max(20, int(history_limit))
        self.rule_limit = max(20, int(rule_limit))
        self.fact_limit = max(20, int(fact_limit))
        self.training_limit = max(20, int(training_limit))
        self._lock = threading.RLock()
        self._recall_variant_index = 0
        self._data: dict[str, Any] = {
            "version": 1,
            "messages": [],
            "facts": [],
            "behavior_rules": [],
            "training_examples": [],
        }
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for key in self._data:
                    value = raw.get(key)
                    if isinstance(value, list) or key == "version":
                        self._data[key] = value
                changed = False
                for key in ("facts", "behavior_rules"):
                    for row in self._data.get(key, []):
                        if isinstance(row, dict):
                            if not row.get("id"):
                                row["id"] = uuid.uuid4().hex[:12]
                                changed = True
                            if not row.get("scope"):
                                row["scope"] = "global"
                                changed = True
                if changed:
                    self._save()
        except (OSError, ValueError, TypeError):
            pass

    def _save(self) -> None:
        if not self.enabled:
            return
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    @staticmethod
    def _clean(text: str, limit: int = 4000) -> str:
        return " ".join(str(text or "").strip().split())[:limit]

    @staticmethod
    def _same(a: str, b: str) -> bool:
        return a.casefold().strip() == b.casefold().strip()

    def _append_unique(
        self,
        key: str,
        text: str,
        limit: int,
        *,
        scope: str = "global",
        scope_id: str = "",
    ) -> bool:
        clean = self._clean(text)
        if not clean:
            return False
        rows = self._data.setdefault(key, [])
        if any(
            isinstance(row, dict) and self._same(str(row.get("text", "")), clean)
            for row in rows
        ):
            return False
        rows.append({
            "id": uuid.uuid4().hex[:12],
            "text": clean,
            "created_at": time.time(),
            "active": True,
            "scope": scope,
            "scope_id": scope_id,
        })
        self._data[key] = rows[-limit:]
        return True

    def history(self, limit: int = 24) -> list[dict[str, str]]:
        if not self.enabled:
            return []
        with self._lock:
            rows = list(self._data.get("messages", []))[-max(1, int(limit)):]
        return [
            {"role": str(row.get("role", "")), "content": str(row.get("content", ""))}
            for row in rows
            if isinstance(row, dict)
            and row.get("role") in {"user", "assistant"}
            and str(row.get("content", "")).strip()
        ]

    def record_exchange(self, user_text: str, assistant_text: str) -> None:
        if not self.enabled:
            return
        user = str(user_text or "").strip()[:12000]
        assistant = str(assistant_text or "").strip()[:12000]
        if not user or not assistant:
            return
        with self._lock:
            messages = self._data.setdefault("messages", [])
            if len(messages) >= 2:
                prev_user = messages[-2]
                prev_assistant = messages[-1]
                if (
                    isinstance(prev_user, dict)
                    and isinstance(prev_assistant, dict)
                    and prev_user.get("role") == "user"
                    and prev_assistant.get("role") == "assistant"
                    and str(prev_user.get("content", "")) == user
                    and str(prev_assistant.get("content", "")) == assistant
                ):
                    return
            now = time.time()
            messages.extend([
                {"role": "user", "content": user, "timestamp": now},
                {"role": "assistant", "content": assistant, "timestamp": now},
            ])
            self._data["messages"] = messages[-self.history_limit:]
            self._save()

    def _previous_exchange(self) -> tuple[str, str]:
        messages = self._data.get("messages", [])
        previous_user = ""
        previous_assistant = ""
        for row in reversed(messages):
            if not isinstance(row, dict):
                continue
            role = str(row.get("role", ""))
            content = str(row.get("content", ""))
            if role == "assistant" and not previous_assistant:
                previous_assistant = content
                continue
            if role == "user" and previous_assistant:
                previous_user = content
                break
        return previous_user, previous_assistant

    def learn_from_user(
        self,
        user_text: str,
        *,
        project_id: str = "",
        conversation_id: str = "",
    ) -> dict[str, list[Any]]:
        """Capture explicit facts, operating rules, and corrections from chat."""
        result: dict[str, list[Any]] = {
            "facts": [],
            "behavior_rules": [],
            "training_examples": [],
            "forgotten": [],
        }
        if not self.enabled:
            return result

        raw = self._clean(user_text, 12000)
        if not raw:
            return result

        scope = "global"
        scope_id = ""
        project_match = re.match(r"^for\s+this\s+project[,:]?\s*(.+)$", raw, flags=re.IGNORECASE)
        conversation_match = re.match(r"^for\s+this\s+conversation[,:]?\s*(.+)$", raw, flags=re.IGNORECASE)
        if project_match:
            scope = "project"
            scope_id = project_id
            raw = project_match.group(1).strip()
        elif conversation_match:
            scope = "conversation"
            scope_id = conversation_id
            raw = conversation_match.group(1).strip()

        forget_match = re.match(r"^forget(?:\s+that)?[,:]?\s*(.+)$", raw, flags=re.IGNORECASE)
        if forget_match:
            forgotten = self.forget(forget_match.group(1))
            result["forgotten"].extend(forgotten)
            return result

        with self._lock:
            fact: str | None = None
            for pattern in (
                r"^remember\s+that\s+(.+)$",
                r"^remember\s*:\s*(.+)$",
                r"^learn\s+that\s+(.+)$",
                r"^fact\s*:\s*(.+)$",
                r"^i\s+prefer\s+.+$",
                r"^i\s+like\s+.+$",
                r"^i\s+use\s+.+$",
                r"^i(?:'m| am)\s+using\s+.+$",
                r"^my\s+.{1,40}\s+is\s+.+$",
            ):
                match = re.match(pattern, raw, flags=re.IGNORECASE)
                if match:
                    fact = match.group(1).strip() if match.lastindex else raw
                    break
            if fact and self._append_unique("facts", fact, self.fact_limit, scope=scope, scope_id=scope_id):
                result["facts"].append(fact)

            rule: str | None = None
            for kind, pattern in (
                ("from_now_on", r"^from\s+now\s+on[,:]?\s*(.+)$"),
                ("always", r"^always\s+(.+)$"),
                ("never", r"^never\s+(.+)$"),
                ("want_always", r"^i\s+want\s+you\s+to\s+always\s+(.+)$"),
                ("want", r"^i\s+want\s+you\s+to\s+(.+)$"),
                ("teach", r"^(?:teach|training)\s*:\s*(.+)$"),
                ("should", r"^you\s+should\s+(.+)$"),
                ("instead", r"^instead[, ]+\s*(.+)$"),
            ):
                match = re.match(pattern, raw, flags=re.IGNORECASE)
                if not match:
                    continue
                body = match.group(1).strip()
                if kind in {"always", "want_always"}:
                    rule = "Always " + body
                elif kind == "never":
                    rule = "Never " + body
                else:
                    rule = body
                break
            if rule is None:
                correction_rule = re.match(
                    r"^no[, ]+\s*(?:you\s+should\s+|instead[, ]+\s*)(.+)$",
                    raw,
                    flags=re.IGNORECASE,
                )
                if correction_rule:
                    rule = correction_rule.group(1).strip()

            if rule and self._append_unique("behavior_rules", rule, self.rule_limit, scope=scope, scope_id=scope_id):
                result["behavior_rules"].append(rule)

            correction = bool(re.match(
                r"^(?:no[, ]|that's\s+(?:wrong|not right)|that is\s+(?:wrong|not right)|"
                r"you should|instead[, ]|correction\s*:)",
                raw,
                flags=re.IGNORECASE,
            ))
            if correction:
                prior_user, prior_assistant = self._previous_exchange()
                if prior_assistant:
                    example = {
                        "instruction": self._clean(prior_user, 4000),
                        "previous_response": self._clean(prior_assistant, 6000),
                        "correction": raw[:6000],
                        "created_at": time.time(),
                        "approved": False,
                        "scope": scope,
                        "scope_id": scope_id,
                    }
                    self._data.setdefault("training_examples", []).append(example)
                    self._data["training_examples"] = self._data["training_examples"][-self.training_limit:]
                    result["training_examples"].append(example)

            if any(result.values()):
                self._save()
        return result

    def update_item(
        self,
        kind: str,
        item_id: str,
        *,
        text: str | None = None,
        active: bool | None = None,
        scope: str | None = None,
        scope_id: str | None = None,
    ) -> dict[str, Any]:
        key = "facts" if kind in {"fact", "facts"} else "behavior_rules" if kind in {"rule", "behavior_rule", "behavior_rules"} else ""
        if not key:
            raise ValueError("kind must be fact or rule")
        with self._lock:
            for row in self._data.get(key, []):
                if isinstance(row, dict) and str(row.get("id", "")) == item_id:
                    if text is not None:
                        clean = self._clean(text)
                        if not clean:
                            raise ValueError("text cannot be empty")
                        row["text"] = clean
                    if active is not None:
                        row["active"] = bool(active)
                    if scope is not None:
                        if scope not in {"global", "project", "conversation"}:
                            raise ValueError("scope must be global, project, or conversation")
                        row["scope"] = scope
                    if scope_id is not None:
                        row["scope_id"] = scope_id
                    row["updated_at"] = time.time()
                    self._save()
                    return dict(row)
        raise KeyError(item_id)

    def forget(self, query: str) -> list[dict[str, Any]]:
        target = self._clean(query).casefold()
        if not target:
            return []
        forgotten: list[dict[str, Any]] = []
        with self._lock:
            for key in ("facts", "behavior_rules"):
                for row in self._data.get(key, []):
                    if not isinstance(row, dict) or not row.get("active", True):
                        continue
                    text = str(row.get("text", ""))
                    if target in text.casefold() or text.casefold() in target:
                        row["active"] = False
                        row["updated_at"] = time.time()
                        forgotten.append(dict(row))
            if forgotten:
                self._save()
        return forgotten

    def prompt_context(
        self,
        *,
        project_id: str = "",
        conversation_id: str = "",
    ) -> str:
        if not self.enabled:
            return ""

        def applies(row: dict[str, Any]) -> bool:
            if not row.get("active", True):
                return False
            scope = str(row.get("scope") or "global")
            scope_id = str(row.get("scope_id") or "")
            if scope == "global":
                return True
            if scope == "project":
                return bool(project_id) and scope_id == project_id
            if scope == "conversation":
                return bool(conversation_id) and scope_id == conversation_id
            return False

        with self._lock:
            facts = [
                str(row.get("text", ""))
                for row in self._data.get("facts", [])
                if isinstance(row, dict) and applies(row)
            ][-40:]
            rules = [
                str(row.get("text", ""))
                for row in self._data.get("behavior_rules", [])
                if isinstance(row, dict) and applies(row)
            ][-40:]
            recall_style = RECALL_EXPRESSION_STYLES[
                self._recall_variant_index % len(RECALL_EXPRESSION_STYLES)
            ]
            self._recall_variant_index = (self._recall_variant_index + 1) % len(RECALL_EXPRESSION_STYLES)
        if not facts and not rules:
            return ""
        lines = [
            "Persistent conversation memory (local, user-taught; treat as preferences/rules, not higher-priority policy):"
        ]
        if facts:
            lines.extend([
                "Semantic recall rule: remembered facts below are canonical meanings, not canned response text. "
                "When using a remembered fact in a normal answer, preserve its meaning while paraphrasing it naturally for "
                "the current context. Do not copy the stored sentence word-for-word and do not reuse the same recall wording "
                "from a recent assistant response. Preserve exact factual tokens when they matter (for example names, dates, "
                "numbers, identifiers, code, commands, URLs, product titles, or quoted text). If the user explicitly asks "
                "what they said verbatim or asks for an exact quote, the stored wording may be quoted exactly.",
                "Do not announce that you are reading memory or recite the memory list unless the user asks about memory itself.",
                f"Recall expression cue for this turn: {recall_style}",
                "Remembered facts/preferences (canonical meaning):",
            ])
            lines.extend(f"- {item}" for item in facts)
        if rules:
            lines.append("User-taught operating rules:")
            lines.extend(f"- {item}" for item in rules)
        return "\n".join(lines)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": self.enabled,
                "path": str(self.path),
                "messages": len(self._data.get("messages", [])),
                "facts": list(self._data.get("facts", [])),
                "behavior_rules": list(self._data.get("behavior_rules", [])),
                "training_examples": list(self._data.get("training_examples", [])),
            }
