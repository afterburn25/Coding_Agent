from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any


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

    def _append_unique(self, key: str, text: str, limit: int) -> bool:
        clean = self._clean(text)
        if not clean:
            return False
        rows = self._data.setdefault(key, [])
        if any(
            isinstance(row, dict) and self._same(str(row.get("text", "")), clean)
            for row in rows
        ):
            return False
        rows.append({"text": clean, "created_at": time.time(), "active": True})
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

    def learn_from_user(self, user_text: str) -> dict[str, list[Any]]:
        """Capture explicit facts, operating rules, and corrections from chat."""
        result: dict[str, list[Any]] = {
            "facts": [],
            "behavior_rules": [],
            "training_examples": [],
        }
        if not self.enabled:
            return result

        raw = self._clean(user_text, 12000)
        if not raw:
            return result

        with self._lock:
            fact: str | None = None
            for pattern in (
                r"^remember\s+that\s+(.+)$",
                r"^remember\s*:\s*(.+)$",
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
            if fact and self._append_unique("facts", fact, self.fact_limit):
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

            if rule and self._append_unique("behavior_rules", rule, self.rule_limit):
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
                    }
                    self._data.setdefault("training_examples", []).append(example)
                    self._data["training_examples"] = self._data["training_examples"][-self.training_limit:]
                    result["training_examples"].append(example)

            if any(result.values()):
                self._save()
        return result

    def prompt_context(self) -> str:
        if not self.enabled:
            return ""
        with self._lock:
            facts = [
                str(row.get("text", ""))
                for row in self._data.get("facts", [])
                if isinstance(row, dict) and row.get("active", True)
            ][-40:]
            rules = [
                str(row.get("text", ""))
                for row in self._data.get("behavior_rules", [])
                if isinstance(row, dict) and row.get("active", True)
            ][-40:]
        if not facts and not rules:
            return ""
        lines = [
            "Persistent conversation memory (local, user-taught; treat as preferences/rules, not higher-priority policy):"
        ]
        if facts:
            lines.append("Remembered facts/preferences:")
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
