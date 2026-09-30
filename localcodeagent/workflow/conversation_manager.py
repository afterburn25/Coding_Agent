from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any


DEFAULT_PERSONALITY = {
    "warmth": 60,
    "humor": 35,
    "verbosity": 50,
    "curiosity": 55,
    "formality": 45,
    "initiative": 50,
    "slang": 15,
    "follow_up_frequency": 35,
}


class ConversationManager:
    """Durable conversation sessions, personality, search, summaries, and feedback."""

    def __init__(self, path: Path, *, summarize_after_messages: int = 24) -> None:
        self.path = path.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.summarize_after_messages = max(8, int(summarize_after_messages))
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {
            "version": 1,
            "active_conversation_id": "",
            "conversations": [],
            "personality": dict(DEFAULT_PERSONALITY),
            "feedback": [],
        }
        self._load()
        if not self._data.get("active_conversation_id"):
            self.create("New chat")

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._data.update(raw)
                personality = dict(DEFAULT_PERSONALITY)
                personality.update(raw.get("personality") or {})
                self._data["personality"] = personality
        except (OSError, ValueError, TypeError):
            pass

    def _save(self) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    @staticmethod
    def _clean(text: str, limit: int = 12000) -> str:
        return str(text or "").strip()[:limit]

    @staticmethod
    def _title(text: str) -> str:
        words = re.sub(r"\s+", " ", text.strip()).split(" ")
        return (" ".join(words[:8]).strip() or "New chat")[:64]

    def _get(self, conversation_id: str | None = None) -> dict[str, Any]:
        target = conversation_id or str(self._data.get("active_conversation_id") or "")
        for row in self._data.get("conversations", []):
            if str(row.get("id")) == target:
                return row
        raise KeyError(target)

    def create(self, title: str = "New chat") -> dict[str, Any]:
        with self._lock:
            now = time.time()
            row = {
                "id": uuid.uuid4().hex[:12],
                "title": self._clean(title, 64) or "New chat",
                "created_at": now,
                "updated_at": now,
                "summary": "",
                "messages": [],
            }
            self._data.setdefault("conversations", []).append(row)
            self._data["conversations"] = self._data["conversations"][-100:]
            self._data["active_conversation_id"] = row["id"]
            self._save()
            return dict(row)

    def set_active(self, conversation_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._get(conversation_id)
            self._data["active_conversation_id"] = row["id"]
            self._save()
            return dict(row)

    def active(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._get())

    def list(self, limit: int = 30) -> list[dict[str, Any]]:
        with self._lock:
            rows = sorted(self._data.get("conversations", []), key=lambda x: float(x.get("updated_at", 0)), reverse=True)
            return [{
                "id": row.get("id"),
                "title": row.get("title"),
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
                "summary": row.get("summary", ""),
                "message_count": len(row.get("messages", [])),
                "active": row.get("id") == self._data.get("active_conversation_id"),
            } for row in rows[:max(1, int(limit))]]

    def history(self, conversation_id: str | None = None, limit: int = 32) -> list[dict[str, str]]:
        with self._lock:
            messages = self._get(conversation_id).get("messages", [])[-max(1, int(limit)):]
            return [
                {"role": str(m.get("role", "")), "content": str(m.get("content", ""))}
                for m in messages
                if m.get("role") in {"user", "assistant"} and str(m.get("content", "")).strip()
            ]

    def record_exchange(self, user: str, assistant: str, *, intent: str = "conversation", model_id: str = "") -> dict[str, Any]:
        user = self._clean(user)
        assistant = self._clean(assistant)
        if not user or not assistant:
            return self.active()
        with self._lock:
            row = self._get()
            now = time.time()
            if row.get("title") in {"", "New chat"}:
                row["title"] = self._title(user)
            row.setdefault("messages", []).extend([
                {"id": uuid.uuid4().hex[:12], "role": "user", "content": user, "timestamp": now},
                {"id": uuid.uuid4().hex[:12], "role": "assistant", "content": assistant, "timestamp": now, "intent": intent, "model_id": model_id},
            ])
            row["messages"] = row["messages"][-400:]
            row["updated_at"] = now
            self._summarize(row)
            self._save()
            return dict(row)

    def _summarize(self, row: dict[str, Any]) -> None:
        messages = row.get("messages", [])
        if len(messages) < self.summarize_after_messages:
            return
        recent = messages[-20:]
        user_topics = [re.sub(r"\s+", " ", str(m.get("content", ""))).strip()[:180] for m in recent if m.get("role") == "user"]
        assistant_topics = [re.sub(r"\s+", " ", str(m.get("content", ""))).strip()[:120] for m in recent if m.get("role") == "assistant"]
        parts = []
        if user_topics:
            parts.append("User topics: " + " | ".join(user_topics[-6:]))
        if assistant_topics:
            parts.append("Recent responses: " + " | ".join(assistant_topics[-4:]))
        row["summary"] = " ".join(parts)[:2000]

    def search(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        q = re.sub(r"\s+", " ", query.strip()).casefold()
        terms = set(re.findall(r"[a-z0-9_+-]{2,}", q))
        if not terms:
            return []
        with self._lock:
            hits = []
            for row in self._data.get("conversations", []):
                hay = " ".join([
                    str(row.get("title", "")),
                    str(row.get("summary", "")),
                    *[str(m.get("content", "")) for m in row.get("messages", [])],
                ]).casefold()
                overlap = len(terms & set(re.findall(r"[a-z0-9_+-]{2,}", hay)))
                if overlap:
                    hits.append((overlap, float(row.get("updated_at", 0)), row))
            hits.sort(key=lambda x: (x[0], x[1]), reverse=True)
            return [{"id": row.get("id"), "title": row.get("title"), "summary": row.get("summary", ""), "updated_at": row.get("updated_at"), "score": score} for score, _updated, row in hits[:max(1, int(limit))]]

    @staticmethod
    def image_generation_intent(text: str) -> bool:
        t = re.sub(r"\\s+", " ", str(text or "").lower()).strip()
        if not t:
            return False

        # Keep edits and other source-image operations on their specialized tool path.
        if any(term in t for term in (
            "edit image", "edit photo", "edit picture", "inpaint", "outpaint",
            "upscale", "remove background", "replace background", "variation of",
        )):
            return False
        if re.search(r"\\b(?:this|my|attached|uploaded|existing|source)\\s+(?:image|photo|picture)\\b", t):
            return False

        # Natural requests often put politeness before the actual generation verb.
        request_prefix = re.compile(
            r"^(?:hey(?:,)?\\s+|please(?:,)?\\s+|"
            r"(?:can|could|would|will)\\s+you\\s+|"
            r"i\\s+want\\s+you\\s+to\\s+|"
            r"i\\s+would\\s+like\\s+you\\s+to\\s+|"
            r"i'd\\s+like\\s+you\\s+to\\s+)"
        )
        while True:
            stripped = request_prefix.sub("", t, count=1).strip()
            if stripped == t:
                break
            t = stripped

        strong_visual_verbs = ("draw", "paint", "illustrate", "sketch")
        visual_verbs = ("generate", "create", "make", "render", "design")
        visual_terms = (
            "image", "picture", "photo", "photograph", "portrait", "illustration",
            "drawing", "artwork", "wallpaper", "logo", "icon", "scene",
            "landscape", "poster", "banner", "avatar", "selfie", "concept art",
            "nude", "naked", "photorealistic",
        )
        non_image_outputs = (
            "code", "function", "class", "component", "script", "report", "email",
            "essay", "document", "spreadsheet", "presentation", "website", "webpage",
            "api", "query", "command", "uuid", "json",
        )

        def contains_any(terms: tuple[str, ...]) -> bool:
            return any(re.search(rf"\\b{re.escape(term)}\\b", t) for term in terms)

        if contains_any(non_image_outputs):
            return False
        if any(re.match(rf"^{verb}\\b", t) for verb in strong_visual_verbs):
            return True
        return (
            any(re.match(rf"^{verb}\\b", t) for verb in visual_verbs)
            and contains_any(visual_terms)
        )

    @staticmethod
    def classify_intent(text: str) -> str:
        t = str(text or "").lower().strip()
        image_operation = any(
            x in t for x in (
                "edit image", "edit photo", "edit picture", "inpaint", "outpaint",
                "upscale image", "upscale photo", "remove background", "replace background",
                "variation of",
            )
        )
        if ConversationManager.image_generation_intent(t) or image_operation:
            return "image"
        if any(x in t for x in ("search the web", "look up", "research", "latest", "current version", "today's news", "source this")):
            return "research"
        if any(x in t for x in ("build", "implement", "debug", "fix", "refactor", "code", "repository", "webpage", "website", "api", "function", "class")):
            return "coding"
        if any(x in t for x in ("write an email", "rewrite", "draft", "story", "poem", "caption", "essay", "script")):
            return "writing"
        if any(x in t for x in ("teach me", "explain like", "quiz me", "help me learn", "lesson", "tutor")):
            return "tutoring"
        if any(x in t for x in ("plan", "roadmap", "schedule", "steps", "strategy", "brainstorm")):
            return "planning"
        if any(x in t for x in ("run ", "open ", "delete ", "move ", "rename ", "commit ", "push ", "create issue")):
            return "tool_action"
        return "conversation"

    @staticmethod
    def intent_prompt(intent: str) -> str:
        prompts = {
            "conversation": "Conversation mode: respond naturally, use context, and maintain continuity.",
            "writing": "Writing mode: focus on clear reusable prose and preserve the user's requested tone.",
            "tutoring": "Tutoring mode: explain progressively, check assumptions, and adapt depth to the user.",
            "planning": "Planning mode: structure options, dependencies, and next actions without pretending actions were performed.",
            "research": "Research mode: distinguish sourced evidence from inference and prefer fresh authoritative sources.",
            "image": "Image mode: use configured image tools when an actual image operation is requested.",
            "coding": "Coding mode: inspect the workspace and use the verified coding workflow when changes are requested.",
            "tool_action": "Tool/action mode: use permissioned tools only when needed and report actual results.",
        }
        return prompts.get(intent, prompts["conversation"])

    def personality(self) -> dict[str, int]:
        with self._lock:
            return dict(self._data.get("personality", DEFAULT_PERSONALITY))

    def update_personality(self, values: dict[str, Any]) -> dict[str, int]:
        with self._lock:
            personality = dict(self._data.get("personality", DEFAULT_PERSONALITY))
            for key in DEFAULT_PERSONALITY:
                if key in values:
                    personality[key] = max(0, min(100, int(values[key])))
            self._data["personality"] = personality
            self._save()
            return dict(personality)

    def personality_prompt(self) -> str:
        p = self.personality()
        return (
            "Conversation style preferences (0-100): "
            + ", ".join(f"{k}={v}" for k, v in p.items())
            + ". Apply these softly unless a direct user instruction overrides them."
        )

    def add_feedback(self, *, message_id: str = "", rating: str, note: str = "", conversation_id: str | None = None) -> dict[str, Any]:
        rating = rating.strip().lower()
        if rating not in {"up", "down", "better", "worse"}:
            raise ValueError("rating must be up, down, better, or worse")
        with self._lock:
            row = {
                "id": uuid.uuid4().hex[:12],
                "conversation_id": conversation_id or self._data.get("active_conversation_id", ""),
                "message_id": str(message_id or ""),
                "rating": rating,
                "note": self._clean(note, 4000),
                "created_at": time.time(),
            }
            self._data.setdefault("feedback", []).append(row)
            self._data["feedback"] = self._data["feedback"][-1000:]
            self._save()
            return dict(row)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "active": dict(self._get()),
                "conversations": self.list(30),
                "personality": self.personality(),
                "feedback_count": len(self._data.get("feedback", [])),
            }
