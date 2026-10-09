from __future__ import annotations

import json
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from ..fsutil import atomic_write_text
from typing import Any


DEFAULT_PERSONALITY = {
    "warmth": 65,
    "humor": 45,
    "verbosity": 50,
    "curiosity": 60,
    "formality": 40,
    "initiative": 55,
    "slang": 20,
    "follow_up_frequency": 25,
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

    @property
    def _backup_path(self) -> Path:
        return self.path.with_name(self.path.name + ".bak")

    def _read_state(self, path: Path) -> dict[str, Any] | None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        if not isinstance(raw, dict) or not raw.get("conversations"):
            return None
        return raw

    def _load(self) -> None:
        raw = self._read_state(self.path) if self.path.exists() else None
        backup = self._read_state(self._backup_path) if self._backup_path.exists() else None
        # If the main file lost conversations relative to the backup (external
        # truncation/deploy overwrite), prefer the backup — it has more data.
        if backup is not None and (
            raw is None
            or len(backup.get("conversations", [])) > len(raw.get("conversations", []))
        ):
            raw = backup
        if raw is None:
            return
        self._data.update(raw)
        personality = dict(DEFAULT_PERSONALITY)
        personality.update(raw.get("personality") or {})
        self._data["personality"] = personality

    def _save(self) -> None:
        if self.path.exists():
            try:
                # Keep the last on-disk state as a backup before overwriting so
                # a failed or clobbered write never destroys chat history.
                # Skip the backup if the disk file lost conversations — that
                # means it was externally truncated and must not poison .bak.
                disk = self._read_state(self.path)
                if disk is not None and len(disk.get("conversations", [])) >= len(self._data.get("conversations", [])):
                    self._backup_path.write_bytes(self.path.read_bytes())
            except OSError:
                pass
        atomic_write_text(self.path, json.dumps(self._data, indent=2, ensure_ascii=False))

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

    @staticmethod
    def _duration_text(seconds: float) -> str:
        seconds = max(0, int(seconds))
        if seconds < 45:
            return "less than a minute"
        if seconds < 3600:
            minutes = max(1, round(seconds / 60))
            return f"{minutes} minute" + ("" if minutes == 1 else "s")
        if seconds < 86400:
            hours = seconds // 3600
            minutes = (seconds % 3600) // 60
            return f"{hours} hour" + ("" if hours == 1 else "s") + (f" {minutes} min" if minutes else "")
        days = seconds // 86400
        hours = (seconds % 86400) // 3600
        return f"{days} day" + ("" if days == 1 else "s") + (f" {hours} hr" if hours else "")

    @staticmethod
    def _local_timestamp(timestamp: float) -> str:
        try:
            dt = datetime.fromtimestamp(float(timestamp)).astimezone()
        except (ValueError, TypeError, OSError, OverflowError):
            return "unknown local time"
        hour = dt.strftime("%I").lstrip("0") or "0"
        return f"{dt.strftime('%a %b %d, %Y')} {hour}:{dt.strftime('%M:%S %p %Z')}"

    def timing_context(
        self,
        *,
        now: float | None = None,
        message_limit: int = 12,
        conversation_limit: int = 4,
    ) -> str:
        """Summarize durable message timestamps into model-friendly temporal context."""
        now_ts = float(time.time() if now is None else now)
        with self._lock:
            active = self._get()
            active_id = str(active.get("id") or "")
            messages = [
                m for m in active.get("messages", [])
                if m.get("role") in {"user", "assistant"} and str(m.get("content", "")).strip()
            ][-max(1, int(message_limit)):]

            lines = [
                "Conversation timing context from durable message timestamps:",
                "Use these timestamps when the user asks when something was said, how long ago it happened, "
                "or whether hours/days passed between exchanges. Do not invent timing for messages that lack timestamps.",
            ]
            if messages:
                lines.append("Recent messages in the active conversation:")
                previous_ts: float | None = None
                for message in messages:
                    ts = float(message.get("timestamp") or 0)
                    role = "User" if message.get("role") == "user" else "Assistant"
                    content = re.sub(r"\s+", " ", str(message.get("content", ""))).strip()[:180]
                    when = self._local_timestamp(ts) if ts else "timestamp unavailable"
                    age = f"{self._duration_text(now_ts - ts)} ago" if ts else "age unavailable"
                    gap = ""
                    if ts and previous_ts and ts >= previous_ts:
                        delta = ts - previous_ts
                        if delta >= 60:
                            gap = f"; {self._duration_text(delta)} after the previous stored message"
                    lines.append(f'- {role} — {when} ({age}{gap}): "{content}"')
                    if ts:
                        previous_ts = ts
            else:
                lines.append("The active conversation has no earlier stored messages.")

            previous_conversations = sorted(
                [
                    row for row in self._data.get("conversations", [])
                    if str(row.get("id") or "") != active_id and float(row.get("updated_at") or 0) > 0
                ],
                key=lambda row: float(row.get("updated_at") or 0),
                reverse=True,
            )[:max(0, int(conversation_limit))]
            if previous_conversations:
                lines.append("Recent previous conversations:")
                for row in previous_conversations:
                    updated = float(row.get("updated_at") or 0)
                    last_user = next(
                        (
                            re.sub(r"\s+", " ", str(m.get("content", ""))).strip()[:160]
                            for m in reversed(row.get("messages", []))
                            if m.get("role") == "user" and str(m.get("content", "")).strip()
                        ),
                        "",
                    )
                    title = str(row.get("title") or "Previous chat")[:80]
                    detail = f'; last user topic: "{last_user}"' if last_user else ""
                    lines.append(
                        f"- {title} — last active {self._local_timestamp(updated)} "
                        f"({self._duration_text(now_ts - updated)} ago){detail}"
                    )
            return "\n".join(lines)[:6000]

    @staticmethod
    def conversation_quality_prompt() -> str:
        return (
            "Conversation quality rules: speak like a capable adult conversational partner, not a tutorial bot or "
            "childlike assistant. Respond to the substance first. Maintain continuity with what the user already said, "
            "including names, preferences, prior answers, corrections, and relevant elapsed time. Do not ask the same "
            "question twice. Avoid parroting the user's message, canned empathy, repetitive disclaimers, and stock endings "
            "such as 'What would you like to discuss?' or 'Is there anything else I can help with?'. Do not end every "
            "response with a question. Ask a follow-up only when it naturally advances the conversation or is genuinely "
            "needed. Match the user's tone and desired depth, vary phrasing, and allow relaxed back-and-forth when the "
            "user is chatting casually. Use contractions and occasional light dry wit when it fits; humor should feel "
            "spontaneous rather than like a forced joke, and serious moments should stay serious. Acknowledge long time "
            "gaps only when relevant. Do not invent human experiences or claim feelings you do not have."
        )

    def record_exchange(
        self,
        user: str,
        assistant: str,
        *,
        intent: str = "conversation",
        model_id: str = "",
        image_job_ids: list[str] | None = None,
        artifact_ids: list[str] | None = None,
        response_source: str = "",
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        user = self._clean(user)
        assistant = self._clean(assistant)
        # Attachment-only turns (photo, no text) still belong in history.
        if not (user or attachments) or not assistant:
            return self.active()
        with self._lock:
            row = self._get()
            now = time.time()
            if row.get("title") in {"", "New chat"}:
                row["title"] = self._title(user)
            assistant_message = {
                "id": uuid.uuid4().hex[:12],
                "role": "assistant",
                "content": assistant,
                "timestamp": now,
                "intent": intent,
                "model_id": model_id,
            }
            if image_job_ids:
                assistant_message["image_job_ids"] = [str(j) for j in image_job_ids if str(j)]
                try:
                    from ..context.active_context import ActiveContext
                    ctx = ActiveContext.from_dict(row.get("active_context"))
                    ctx.note_image_jobs(list(assistant_message["image_job_ids"]))
                    row["active_context"] = ctx.to_dict()
                except Exception:
                    pass
            if artifact_ids:
                # Artifact download cards re-render from these ids —
                # the registry resolves fresh views (verification state
                # may change between sessions).
                assistant_message["artifact_ids"] = [
                    str(a) for a in artifact_ids if str(a)][:12]
            if response_source:
                assistant_message["response_source"] = response_source
            user_message = {"id": uuid.uuid4().hex[:12], "role": "user",
                            "content": user, "timestamp": now}
            if attachments:
                # Persisted with the message so attached images re-render
                # on reload and image follow-ups can reuse the file path.
                user_message["attachments"] = attachments
            row.setdefault("messages", []).extend([user_message, assistant_message])
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

    # Conversational scaffolding that pads a prompt without describing the
    # image — image models do better with the bare descriptive phrase.
    _PROMPT_SCAFFOLD_RE = re.compile(
        r"^(?:hey(?:\s+nexus)?[,!]?\s+|okay[,!]?\s+|please\s+|"
        r"(?:can|could|would|will)\s+you\s+|"
        r"i\s+(?:want|need|would\s+like|'?d\s+like)\s+(?:you\s+to\s+)?|"
        r"go\s+ahead\s+and\s+|"
        r"(?:generate|create|make|draw|paint|render|produce|show\s+me|"
        r"give\s+me|whip\s+up)\s+"
        r"(?:me\s+)?(?:an?\s+|the\s+|some\s+|another\s+|a\s+few\s+)?"
        r"(?:images?|pictures?|photos?|pics?|illustrations?|drawings?|"
        r"renders?|wallpapers?|portraits?|versions?)\s+"
        r"(?:of|showing|depicting|featuring|with|where|that\s+shows?)\s+)+",
        re.IGNORECASE)

    @classmethod
    def refine_image_prompt(cls, text: str) -> str:
        """Strip request scaffolding so the image model sees the descriptive
        core — 'please generate an image of a red cat' → 'a red cat'.
        Grammar lives in context.intent so detection and refinement can
        never drift apart."""
        from ..context.intent import strip_image_scaffold
        return strip_image_scaffold(text)

    @classmethod
    def split_negative_prompt(cls, text: str) -> tuple[str, str]:
        """Split 'a red cat, no collar, without a hat, negative: blurry'
        → ('a red cat', 'collar, hat, blurry').

        Exclusion clauses are moved to the negative prompt; everything else
        stays positive. 'no X' binds only to the clause it starts — commas
        and 'but' end it so 'no collar, red fur' keeps 'red fur' positive."""
        t = str(text or "").strip()
        negative: list[str] = []
        # Explicit negative prompt wins outright.
        m = re.search(
            r"\bnegative[\s_-]*prompt\s*[:=\-]\s*(.+)$", t, re.IGNORECASE)
        if m:
            negative.append(m.group(1).strip(" .,;"))
            t = t[:m.start()].strip(" ,.;")
        # "but no X" / ", no X" / "without X" / "not X" / "don't include X"
        # / "excluding X" — each binds up to the next comma/period.
        def _pull(match: re.Match) -> str:
            clause = match.group(2).strip(" .,;")
            if clause:
                negative.append(clause)
            g1 = match.group(1) or ""
            # A consumed conjunction ("but no flowers") leaves no residue;
            # punctuation boundaries are kept so joins stay grammatical.
            return "" if g1.strip().lower() in {"", "but"} else g1
        t = re.sub(
            r"(^|[,;.?!]|\bbut\b)\s*"
            r"(?:without\s+|not\s+|don't\s+include\s+|"
            r"do\s+not\s+include\s+|exclud\w+\s+|no\s+)"
            r"([^,.;?!]+?)(?=[,.;?!]|$)",
            _pull, t, flags=re.IGNORECASE)
        t = re.sub(r"\s+", " ", t).strip(" ,.;")
        # If every clause was negative the original text is the only signal
        # the model has — keep it rather than send an empty prompt.
        return (t or str(text or "").strip()), ", ".join(x for x in negative if x)

    # ------------------------------------------------------------------
    # Active context — compact per-conversation working state persisted
    # on the row (survives restart, isolated per conversation).
    # ------------------------------------------------------------------
    def active_context(self, conversation_id: str | None = None):
        from ..context.active_context import ActiveContext
        with self._lock:
            row = self._get(conversation_id)
            return ActiveContext.from_dict(row.get("active_context"))

    def update_active_context(self, env, conversation_id: str | None = None):
        """Fold an IntentEnvelope into the row's ActiveContext — topic
        shifts, corrections, parked clarifications all handled inside."""
        from ..context.active_context import ActiveContext
        with self._lock:
            row = self._get(conversation_id)
            ctx = ActiveContext.from_dict(row.get("active_context"))
            ctx.record_turn(env)
            row["active_context"] = ctx.to_dict()
            self._save()
            return ctx

    def set_context_field(self, conversation_id: str | None = None, **fields):
        from ..context.active_context import ActiveContext
        with self._lock:
            row = self._get(conversation_id)
            ctx = ActiveContext.from_dict(row.get("active_context"))
            for k, v in fields.items():
                if hasattr(ctx, k):
                    setattr(ctx, k, v)
            ctx.touch()
            row["active_context"] = ctx.to_dict()
            self._save()
            return ctx

    @staticmethod
    def image_generation_intent(text: str) -> bool:
        from ..context.intent import detect_image_intent
        matched, _conf, _ev = detect_image_intent(
            re.sub(r"\s+", " ", str(text or "").lower()).strip())
        if matched:
            return True
        t = re.sub(r"\s+", " ", str(text or "").lower()).strip()
        if not t:
            return False

        # Inpainting/outpainting need arguments only the model+tools can
        # assemble (mask, canvas geometry) — keep those on the tool path.
        # "edit image" style commands stay model-routed too; the op regex
        # below still catches verb forms like "edit this image".
        if any(term in t for term in (
            "edit image", "edit photo", "edit picture", "inpaint", "outpaint",
        )):
            return False
        # Source-image operations ARE generation intent: the orchestrator
        # passes the attachment as source_image and the image lane runs
        # edit_image/upscale/etc. deterministically — no model call where
        # the model could promise a job it never queues.
        if re.search(
            r"\b(?:recreate|re-?create|remake|redraw|re-?draw|reimagine|"
            r"rework|redo|regenerate|retry|edit|change|modify|alter|"
            r"enhance|fix|retouch|touch\s*up|transform|improve|restore|"
            r"upscale|clean\s*up)\b[^.?!]{0,80}"
            r"\b(?:this|that|the|my|attached|uploaded|existing|source)?\s*"
            r"(?:images?|pictures?|photos?|pics?)\b", t):
            return True
        if any(term in t for term in (
            "remove background", "replace background", "variation of",
        )):
            return True

        # Natural requests often put politeness before the actual generation verb.
        request_prefix = re.compile(
            r"^(?:hey(?:,)?\s+|please(?:,)?\s+|"
            r"(?:can|could|would|will)\s+you\s+|"
            r"i\s+want\s+you\s+to\s+|"
            r"i\s+would\s+like\s+you\s+to\s+|"
            r"i'd\s+like\s+you\s+to\s+)"
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

        def contains_any(terms: tuple[str, ...], *, plural: bool = False) -> bool:
            # plural=True lets "image" match "images", "picture" match
            # "pictures", etc. — multi-image requests are the common case.
            tail = "s?" if plural else ""
            return any(re.search(rf"\b{re.escape(term)}{tail}\b", t) for term in terms)

        if contains_any(non_image_outputs):
            return False
        if any(re.match(rf"^{verb}\b", t) for verb in strong_visual_verbs):
            return True
        return (
            any(re.match(rf"^{verb}\b", t) for verb in visual_verbs)
            and contains_any(visual_terms, plural=True)
        )

    # Envelope intent → the lane labels this classifier has always
    # produced. The envelope is authoritative; this maps for callers
    # that still consume the legacy labels (brain thalamus, prompt
    # selection, activity metadata).
    _ENV_TO_LABEL = {
        "image_generation": "image",
        "image_edit": "image",
        "image_followup": "conversation",
        "tool_action": "tool_action",
        "git_action": "tool_action",
        "github_status": "tool_action",
        "file_edit": "coding",
        "coding": "coding",
        "research": "research",
        "writing": "writing",
        "correction": "conversation",
        "clarification_response": "conversation",
        "feedback_signal": "conversation",
        "identity_query": "conversation",
        "question": "conversation",
        "conversation": "conversation",
    }

    @staticmethod
    def classify_intent(text: str, active=None) -> str:
        t = str(text or "").lower().strip()
        from ..context.intent import understand_turn
        env = understand_turn(t, active=active)
        if env.primary_intent != "conversation" or env.confidence >= 0.7:
            return ConversationManager._ENV_TO_LABEL.get(
                env.primary_intent, "conversation")
        image_operation = any(
            x in t for x in (
                "edit image", "edit photo", "edit picture", "inpaint", "outpaint",
                "upscale image", "upscale photo", "remove background", "replace background",
                "variation of",
            )
        ) or bool(re.search(
            r"\b(?:recreate|re-?create|remake|redraw|re-?draw|reimagine|"
            r"rework|redo|regenerate|edit|change|modify|alter|enhance|"
            r"retouch|touch\s*up|transform|improve|restore|upscale|"
            r"clean\s*up)\b[^.?!]{0,80}"
            r"\b(?:this|that|the|my|attached|uploaded|existing|source)?\s*"
            r"(?:images?|pictures?|photos?|pics?)\b", t))
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
            "conversation": "Conversation mode: carry on a mature, natural back-and-forth. Answer what the user actually said, preserve continuity across turns and time gaps, avoid repetitive assistant clichés, and ask follow-ups only when they add value.",
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
        directives = [
            "Apply these preferences softly unless a direct user instruction overrides them.",
            "Use natural contractions and varied sentence rhythm instead of sounding scripted.",
        ]
        if p.get("humor", 0) >= 35:
            directives.append("Use occasional dry, playful humor when it naturally fits; do not force a joke into every reply.")
        if p.get("warmth", 0) >= 55:
            directives.append("Sound engaged and personable without canned empathy or exaggerated praise.")
        if p.get("formality", 100) <= 50:
            directives.append("Prefer relaxed adult conversation over formal customer-service phrasing.")
        if p.get("initiative", 0) >= 50:
            directives.append("When useful, connect the current topic to relevant earlier context without waiting to be asked.")
        if p.get("follow_up_frequency", 100) <= 40:
            directives.append("Do not end most responses with a question; let statements stand when the exchange is complete.")
        return (
            "Conversation style preferences (0-100): "
            + ", ".join(f"{k}={v}" for k, v in p.items())
            + ". "
            + " ".join(directives)
        )

    def add_feedback(self, *, message_id: str = "", rating: str, note: str = "", conversation_id: str | None = None) -> dict[str, Any]:
        rating = rating.strip().lower()
        if rating not in {"up", "down", "better", "worse"}:
            raise ValueError("rating must be up, down, better, or worse")
        with self._lock:
            target_conversation_id = conversation_id or str(self._data.get("active_conversation_id") or "")
            conversation = self._get(target_conversation_id)
            messages = list(conversation.get("messages", []))
            target_index = -1
            if message_id:
                target_index = next(
                    (
                        i for i, message in enumerate(messages)
                        if str(message.get("id") or "") == str(message_id)
                        and message.get("role") == "assistant"
                    ),
                    -1,
                )
            if target_index < 0:
                target_index = next(
                    (i for i in range(len(messages) - 1, -1, -1) if messages[i].get("role") == "assistant"),
                    -1,
                )
            assistant_message = messages[target_index] if target_index >= 0 else {}
            preceding_user = ""
            if target_index >= 0:
                preceding_user = next(
                    (
                        str(messages[i].get("content", "")).strip()
                        for i in range(target_index - 1, -1, -1)
                        if messages[i].get("role") == "user" and str(messages[i].get("content", "")).strip()
                    ),
                    "",
                )
            row = {
                "id": uuid.uuid4().hex[:12],
                "conversation_id": target_conversation_id,
                "message_id": str(assistant_message.get("id") or message_id or ""),
                "rating": rating,
                "note": self._clean(note, 4000),
                "user_prompt": self._clean(preceding_user, 12000),
                "assistant_response": self._clean(str(assistant_message.get("content", "")), 20000),
                "assistant_timestamp": float(assistant_message.get("timestamp") or 0),
                "created_at": time.time(),
            }
            self._data.setdefault("feedback", []).append(row)
            self._data["feedback"] = self._data["feedback"][-1000:]
            self._save()
            return dict(row)

    def find_message(self, message_id: str,
                     conversation_id: str | None = None) -> dict[str, Any] | None:
        """Locate a persisted message by id — used to attach feedback
        side-effects (image job ratings) to the right message."""
        with self._lock:
            conv_id = conversation_id or str(self._data.get("active_conversation_id") or "")
            conversation = self._get(conv_id)
            messages = conversation.get("messages", [])
            for message in messages:
                if str(message.get("id") or "") == str(message_id):
                    return dict(message)
            # Same fallback as add_feedback: rating lands on the latest
            # assistant message when no explicit id matched.
            for message in reversed(messages):
                if message.get("role") == "assistant":
                    return dict(message)
        return None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "active": dict(self._get()),
                "conversations": self.list(30),
                "personality": self.personality(),
                "feedback": [dict(x) for x in self._data.get("feedback", [])[-200:] if isinstance(x, dict)],
                "feedback_count": len(self._data.get("feedback", [])),
            }
