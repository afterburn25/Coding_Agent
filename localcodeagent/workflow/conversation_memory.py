from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path

from ..fsutil import atomic_write_text
from typing import Any


_OPTION_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
}

# A user reply that is nothing more than a reference to an option the
# assistant just proposed — "option 1", "the second option", "go with 3",
# "pick the first one", bare "2". Kept deliberately tight: anything longer
# or content-bearing stays a normal message.
_OPTION_SELECTION_RES = (
    re.compile(
        r"^\s*(?:go with|let'?s do|do|pick|choose|take|i'?ll take|use|run|"
        r"yes[ ,]*)?\s*(?:the\s+)?option\s*#?\s*(\d{1,2})\s*[.!?\s]*$",
        re.IGNORECASE),
    re.compile(
        r"^\s*(?:go with|let'?s do|do|pick|choose|take|i'?ll take|use|run)?"
        r"\s*(?:the\s+)?option\s+(one|two|three|four|five|six|seven|eight|"
        r"nine|ten|first|second|third|fourth|fifth|sixth|seventh|eighth|"
        r"ninth|tenth)\s*[.!?\s]*$", re.IGNORECASE),
    re.compile(
        r"^\s*(?:go with|let'?s do|do|pick|choose|take|i'?ll take|use|run)?"
        r"\s*(?:the\s+)?(first|second|third|fourth|fifth|sixth|seventh|"
        r"eighth|ninth|tenth)\s*(?:option|one)\s*[.!?\s]*$",
        re.IGNORECASE),
    re.compile(r"^\s*(\d{1,2})\s*[.!?\s]*$"),
    re.compile(
        r"^\s*(one|two|three|four|five|six|seven|eight|nine|ten)"
        r"\s*[.!?\s]*$", re.IGNORECASE),
)


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
            "pending_options": [],
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
        atomic_write_text(self.path, json.dumps(self._data, indent=2, ensure_ascii=False))

    @staticmethod
    def _clean(text: str, limit: int = 4000) -> str:
        return " ".join(str(text or "").strip().split())[:limit]

    @staticmethod
    def _same(a: str, b: str) -> bool:
        return a.casefold().strip() == b.casefold().strip()

    _CONTENT_STOPWORDS = frozenset({
        "what", "when", "where", "which", "that", "this", "these", "those",
        "with", "from", "your", "yours", "mine", "does", "said", "tell",
        "know", "about", "would", "could", "should", "there", "their",
        "they", "them", "then", "than", "have", "has", "are", "was",
        "were", "will", "just", "like", "mean", "meant", "remember",
        "before", "earlier", "yesterday", "today", "now", "use", "uses",
        "the", "and", "for", "you", "did", "didnt", "it's", "its",
        # Generic nouns — a lone shared term like "project" must never
        # count as relevance, or every "Project X" fact rides every
        # "Project Y" question (the §4 intrusion leak the QA loop caught).
        "project", "thing", "things", "stuff", "something", "anything",
        "way", "ways", "kind", "type", "sort", "part", "parts", "case",
        "cases", "name", "names", "issue", "issues", "problem",
        "problems", "question", "questions", "answer", "answers",
    })

    @classmethod
    def _content_terms(cls, text: str) -> set[str]:
        """Content-bearing terms used for relevance-gated recall."""
        return {
            t for t in re.findall(r"[a-z0-9_+.#-]{3,}", str(text or "").lower())
            if t not in cls._CONTENT_STOPWORDS and not t.isdigit()
        }

    _SUBJECT_ADVERBS = frozenset({
        "now", "currently", "today", "recently", "actually", "finally",
        "just", "also", "still", "really", "meanwhile", "anyway",
    })

    @classmethod
    def _clean_subject(cls, raw: str) -> str:
        """Normalize a declarative-fact subject: drop trailing discourse
        adverbs ('Orion now uses X' -> 'Orion') and 'off/from X' migration
        tails so the supersession slot stays 'orion:use'."""
        subj = re.sub(r"\s+", " ", str(raw or "").strip())
        subj = re.sub(r"^(?:the|our|a|an)\s+", "", subj,
                      flags=re.IGNORECASE)
        subj = re.sub(
            r"\s+(?:off|from|away from)\s+[a-z0-9][a-z0-9 ._-]{0,38}$",
            "", subj, flags=re.IGNORECASE)
        # Strip trailing discourse adverbs only — 'Orion now' -> 'Orion'
        # while a mid-subject 'still' in a real name stays put.
        words = subj.split()
        while words and words[-1].lower() in cls._SUBJECT_ADVERBS:
            words.pop()
        return " ".join(words).strip()

    @classmethod
    def _clean_value(cls, raw: str) -> str:
        """Strip trailing punctuation and discourse adverbs from a fact
        value — 'MySQL now.' -> 'MySQL' so the stored fact stays
        canonical and dedup/supersession compare cleanly."""
        value = str(raw or "").strip().rstrip(".!?")
        words = value.split()
        while words and words[-1].lower() in cls._SUBJECT_ADVERBS:
            words.pop()
        return " ".join(words).strip()

    def _correct_fact_value(self, old_value: str, new_value: str) -> str | None:
        """'X not Y' correction — rewrite the fact that ends in Y.

        Returns the corrected fact text when exactly one active fact's
        value tail matches `old_value` (ambiguous matches and no-matches
        stay untouched — a correction must never guess). The caller
        stores it via the normal append path so slot supersession
        retires the stale row rather than editing history in place.
        """
        old = str(old_value or "").strip().lower()
        new = str(new_value or "").strip()
        if not old or not new or old == new.lower():
            return None
        with self._lock:
            hits = [
                row for row in self._data.get("facts", [])
                if isinstance(row, dict) and row.get("active", True)
                and str(row.get("text", "")).strip().lower().endswith(old)
            ]
        if len(hits) != 1:
            return None
        text = str(hits[0].get("text", "")).strip()
        return text[: len(text) - len(old)].rstrip() + " " + new

    @staticmethod
    def _fact_subject_ok(subj: str) -> bool:
        """Question/command-shaped openings are not fact subjects."""
        if not subj:
            return False
        return subj.split()[0] not in {
            "i", "we", "you", "they", "he", "she", "it",
            "what", "which", "who", "why", "how", "when", "where",
            "does", "do", "did", "can", "could", "should", "would",
            "is", "are", "was", "were", "will", "this", "that",
            "tell", "show", "if", "let",
            "isnt", "arent", "doesnt"}

    @staticmethod
    def _fact_slot(text: str) -> str:
        """Supersession slot for entity-attribute facts.

        Facts naming a concrete subject+relation ("orion uses postgresql",
        "my editor is vim") occupy a slot; a newer fact in the same slot
        retires the older one. Vague/open facts ("i prefer tea") keep no
        slot so unrelated preferences are never clobbered.
        """
        t = str(text or "").strip().lower()
        m = re.match(r"^my\s+([a-z0-9][a-z0-9 ._-]{0,38}?)\s+(?:is|are|was|were)\b", t)
        if m:
            return "my:" + re.sub(r"\s+", " ", m.group(1)).strip()
        m = re.match(
            r"^(?:project\s+)?([a-z0-9][a-z0-9 ._-]{0,38}?)\s+"
            r"(uses?|runs on|is built on|is written in|depends on|prefers?|"
            r"should stay|should be|must be|will be|shall be|stays?|remains?)\b",
            t,
        )
        if m:
            subj = re.sub(r"\s+", " ", m.group(1)).strip()
            subj = re.sub(r"^(?:the|our|a|an)\s+", "", subj)
            pred = re.sub(r"\s+", " ", m.group(2)).strip()
            if subj and subj not in {"i", "we", "you", "they", "he", "she", "it"}:
                return f"{subj}:{pred}"
        return ""

    def _append_unique(
        self,
        key: str,
        text: str,
        limit: int,
        *,
        scope: str = "global",
        scope_id: str = "",
        slot: str = "",
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
        now = time.time()
        row_id = uuid.uuid4().hex[:12]
        if slot:
            for row in rows:
                if (
                    isinstance(row, dict)
                    and row.get("active", True)
                    and str(row.get("slot") or "") == slot
                    and str(row.get("scope") or "global") == scope
                    and str(row.get("scope_id") or "") == scope_id
                ):
                    row["active"] = False
                    row["superseded"] = True
                    row["superseded_at"] = now
                    row["superseded_by"] = row_id
                    row["updated_at"] = now
        rows.append({
            "id": row_id,
            "text": clean,
            "created_at": now,
            "active": True,
            "scope": scope,
            "scope_id": scope_id,
            "slot": slot,
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
            # Track option lists from the latest assistant reply so a short
            # follow-up like "option 1" can be resolved instead of falling
            # through to the model as an ambiguous fragment.
            self._data["pending_options"] = self.extract_options(assistant)
            self._save()

    @staticmethod
    def extract_options(assistant_text: str) -> list[str]:
        """Numbered options from an assistant proposal — "Option 1: …",
        "**Option 2** — …", or bare "1. …" lists. Only counts as an option
        list when numbering starts at 1 and runs consecutively; lines
        inside code fences are ignored."""
        options: list[tuple[int, str]] = []
        in_fence = False
        for line in str(assistant_text or "").splitlines():
            stripped = line.strip()
            if stripped.startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence or len(stripped) > 400:
                continue
            m = re.match(
                r"^(?:[-*•]\s*)?(?:\*\*)?(?:option\s*)?(\d{1,2})\s*"
                r"[\.\)\:]?\s*(?:\*\*)?\s*[:\-–—]?\s*(?:\*\*)?\s*(.+?)\s*$",
                stripped, flags=re.IGNORECASE)
            if not m:
                continue
            text = re.sub(r"\*\*([^*]+)\*\*", r"\1", m.group(2)).strip()
            text = text.strip("*_ ").strip()
            if text:
                options.append((int(m.group(1)), text[:300]))
        # Only a genuine numbered list: starts at 1, consecutive, ≥2 items.
        if len(options) < 2:
            return []
        if [n for n, _ in options] != list(range(1, len(options) + 1)):
            return []
        return [t for _, t in options]

    def resolve_option_selection(self, user_text: str) -> str | None:
        """Expand a bare selection ("option 1", "the second option",
        "go with 2") into the option text from the most recent assistant
        proposal, so downstream routing sees the real intent."""
        options = self._data.get("pending_options") or []
        if len(options) < 2:
            return None
        text = self._clean(user_text, 200)
        if not text:
            return None
        number = None
        for pattern in _OPTION_SELECTION_RES:
            match = pattern.match(text)
            if match:
                token = match.group(1).lower()
                number = (int(token) if token.isdigit()
                          else _OPTION_NUMBER_WORDS.get(token))
                break
        if number is None or not 1 <= number <= len(options):
            return None
        return f"Option {number} — {options[number - 1]}"

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

        # Creator-locked identity facts (birthday/age/creator) cannot be
        # taught, overridden, or forgotten — drop them before any storage.
        # The refusal only surfaces for an actual write attempt; a plain
        # question ("how old are you?") must fall through to the normal
        # answer lanes instead of replying with lock wording.
        from ..identity import locked_topic, locked_refusal, is_write_intent
        topic = locked_topic(raw)
        if topic and is_write_intent(raw):
            result.setdefault("locked", []).append(locked_refusal(topic))
            return result

        forget_match = re.match(
            r"^(?:forget(?:\s+(?:that|about))?|nevermind(?:\s+about)?|"
            r"stop\s+remembering|delete\s+(?:the\s+)?facts?"
            r"(?:\s+about)?)[,:]?\s*(.+)$",
            raw, flags=re.IGNORECASE)
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
            if fact is None:
                # Declarative entity-attribute statements — "Project Orion
                # uses PostgreSQL", "Orion runs on Linux" — and updates like
                # "we switched Orion to SQLite" / "Orion moved to Redis".
                # All of these canonicalize to "subject predicate value" so
                # a newer statement occupies the same supersession slot.
                body = re.sub(
                    r"^(?:actually|by the way|btw|also|so|fyi|note|quick note|"
                    r"for the record|just so you know)[,:\s]+",
                    "", raw, flags=re.IGNORECASE)
                switch = re.match(
                    r"^we\s+(?:switched|moved|migrated|changed)\s+"
                    r"([a-z0-9][a-z0-9 ._-]{0,38}?)\s+to\s+(.+)$",
                    body, flags=re.IGNORECASE,
                )
                corr_body = ""
                if switch:
                    subj = self._clean_subject(switch.group(1))
                    value = self._clean_value(switch.group(2))
                    if self._fact_subject_ok(subj) and value:
                        fact = f"{subj} uses {value}"
                elif fact is None:
                    # Corrections — "correction: the port is 5433",
                    # "no, I meant SQLite", "actually it was Redis not
                    # Postgres". The 'X not Y' form supersedes the fact
                    # carrying Y when Y uniquely identifies one.
                    corr = re.match(
                        r"^(?:correction|i\s+meant|no[,]?\s+i\s+meant|"
                        r"to\s+clarify|sorry[,]?\s*i\s+meant)[,:]?\s*(.+)$",
                        body, flags=re.IGNORECASE,
                    )
                    nyc = re.match(
                        r"^(?:actually[,]?\s+|no[,]?\s+)?(?:it|that|this)\s+"
                        r"(?:is|was)\s+(.+?)\s+not\s+(.+?)[.!?]?$",
                        body, flags=re.IGNORECASE,
                    )
                    if nyc:
                        new_v = self._clean_value(nyc.group(1))
                        old_v = self._clean_value(nyc.group(2))
                        fact = self._correct_fact_value(old_v, new_v)
                    elif corr:
                        inner = corr.group(1).strip()
                        inner_nyc = re.match(
                            r"^(.+?)\s+not\s+(.+?)[.!?]?$", inner,
                            flags=re.IGNORECASE)
                        if inner_nyc:
                            new_v = self._clean_value(inner_nyc.group(1))
                            old_v = self._clean_value(inner_nyc.group(2))
                            fact = self._correct_fact_value(old_v, new_v)
                        elif fact is None:
                            # Re-body the correction so the normal
                            # declarative/decision pipeline canonicalizes
                            # it ("the store uses Redis" -> "store uses
                            # Redis"); corr_body is the verbatim fallback
                            # when nothing structured matches.
                            body = inner
                            corr_body = inner.rstrip(".!?")
                if fact is None:
                    # Decision statements — "we decided to use SQLite for
                    # the store", "the plan is Postgres for production",
                    # "let's go with Redis". Canonicalize to
                    # "subject uses value" so a revised decision retires
                    # the earlier one in the same slot.
                    dec = re.match(
                        r"^(?:we\s+(?:decided|chose|settled|opted|picked|went)|"
                        r"(?:let'?s|let\s+us)\s+(?:go|decide|settle|opt)|"
                        r"the\s+plan\s+is)\s*"
                        r"(?:to\s+use|to\s+go\s+with|on|with|for)?\s*"
                        r"([a-z0-9][a-z0-9 ._+/#-]{0,38}?)"
                        r"(?:\s+(?:for|as|in|on)\s+(?:the\s+|our\s+|a\s+)?"
                        r"([a-z0-9][a-z0-9 ._-]{0,38}?))?[.!?]?$",
                        body, flags=re.IGNORECASE,
                    )
                    if dec:
                        value = self._clean_value(dec.group(1))
                        subj = self._clean_subject(dec.group(2) or "")
                        if not subj:
                            subj = "this project"
                        # "decided to refactor X" is an action decision,
                        # not a tool/choice statement — skip it rather
                        # than canonicalize garbage.
                        if (value and self._fact_subject_ok(subj)
                                and not re.match(
                                    r"^(?:option|to|the|that|a|an|we|it|i)\b",
                                    value, flags=re.IGNORECASE)):
                            fact = f"{subj} uses {value}"
                    if fact is None:
                        # "we agreed (that) the API should stay REST"
                        agreed = re.match(
                            r"^we\s+agreed\s+(?:that\s+)?"
                            r"(?:the\s+|our\s+)?"
                            r"([a-z0-9][a-z0-9 ._-]{0,38}?)\s+"
                            r"(should|must|will|shall)\s+"
                            r"(stay|be|remain|keep|use|have)\s+(.+)$",
                            body, flags=re.IGNORECASE,
                        )
                        if agreed:
                            subj = self._clean_subject(agreed.group(1))
                            value = self._clean_value(agreed.group(4))
                            if self._fact_subject_ok(subj) and value:
                                fact = (f"{subj} {agreed.group(2).lower()} "
                                        f"{agreed.group(3).lower()} {value}")
                if fact is None:
                    # Subject-led update: "Orion moved to Redis",
                    # "Orion migrated off Postgres to Redis".
                    subj_switch = re.match(
                        r"^(?:project\s+)?([a-z0-9][a-z0-9 ._-]{0,38}?)\s+"
                        r"(?:switched|moved|migrated|changed)"
                        r"(?:\s+(?:off|from|away from)\s+[a-z0-9][a-z0-9 ._-]{0,38}?)?"
                        r"\s+to\s+(.+)$",
                        body, flags=re.IGNORECASE,
                    )
                    decl = None
                    subj = ""
                    if subj_switch:
                        subj = self._clean_subject(subj_switch.group(1))
                        value = self._clean_value(subj_switch.group(2))
                        if self._fact_subject_ok(subj) and value:
                            fact = f"{subj} uses {value}"
                    else:
                        decl = re.match(
                            r"^(?:project\s+)?([a-z0-9][a-z0-9 ._-]{0,38}?)\s+"
                            r"(uses?|runs on|is built on|is written in|"
                            r"depends on|prefers?)\s+(.+)$",
                            body, flags=re.IGNORECASE,
                        )
                        if decl:
                            subj = self._clean_subject(decl.group(1))
                            if self._fact_subject_ok(subj):
                                pred = re.sub(
                                    r"\s+", " ", decl.group(2).strip().lower())
                                value = self._clean_value(decl.group(3))
                                if value:
                                    fact = f"{subj} {pred} {value}"
                if fact is None and corr_body:
                    fact = corr_body
            if fact and locked_topic(fact):
                result.setdefault("locked", []).append(
                    locked_refusal(locked_topic(fact)))
                fact = None
            if fact and self._append_unique(
                    "facts", fact, self.fact_limit,
                    scope=scope, scope_id=scope_id, slot=self._fact_slot(fact),
            ):
                result["facts"].append(fact)

            # Rule revocation — "stop responding in JSON", "don't use
            # emojis anymore" retires the matching active rule rather
            # than leaving a contradiction live in every prompt.
            revoke = re.match(
                r"^(?:stop|quit)\s+(.+?)(?:\s+please)?[.!?]?$"
                r"|^(?:do\s+not|don't|dont|please\s+do\s+not)\s+(.+?)\s+anymore[.!?]?$"
                r"|^no\s+longer\s+(.+?)[.!?]?$"
                r"|^you\s+can\s+stop\s+(.+?)[.!?]?$",
                raw, flags=re.IGNORECASE)
            if revoke:
                action = next(g for g in revoke.groups() if g)
                revoked = self._revoke_rules(action)
                result["forgotten"].extend(revoked)
                if not revoked and self._rule_terms(action):
                    # Nothing to retract — the phrase is itself a durable
                    # prohibition ("stop responding in JSON" with no prior
                    # mandate means "never do that").
                    rule = f"Never {action.strip()}"
                    if not locked_topic(rule) and self._append_unique(
                            "behavior_rules", rule, self.rule_limit,
                            scope=scope, scope_id=scope_id):
                        result["behavior_rules"].append(rule)
                if any(result.values()):
                    self._save()
                return result

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

            if rule and locked_topic(rule):
                result.setdefault("locked", []).append(
                    locked_refusal(locked_topic(rule)))
                rule = None
            if rule and self._append_unique("behavior_rules", rule, self.rule_limit, scope=scope, scope_id=scope_id):
                result["behavior_rules"].append(rule)

            correction = bool(re.match(
                r"^(?:no,|that's\s+(?:wrong|not right)|that is\s+(?:wrong|not right)|"
                r"you should|instead[, ]|correction\s*:"
                # "no <correction-shaped continuation>" — bare "no "
                # alone swallowed "no offense but…", "no problem".
                r"|no\s+(?:that's|that\s+is|you\s+should|the\s+answer|"
                r"the\s+correct|try|use|it\s+should|don't|do\s+not|"
                r"not\s+quite|make\s+it|go\s+back)\b)",
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

    _RULE_STEMS_DROP = {
        "in", "the", "a", "an", "to", "and", "or", "of", "my", "your",
        "with", "on", "for", "it", "that", "this", "be", "is", "are",
        "me", "you", "always", "never", "from", "now", "please",
    }

    @staticmethod
    def _stem_token(token: str) -> str:
        if token.endswith("ing") and len(token) > 4:
            return token[:-3]
        if token.endswith("ed") and len(token) > 4:
            return token[:-2]
        if token.endswith("es") and len(token) > 4:
            return token[:-2]
        if token.endswith("s") and len(token) > 3 and not token.endswith("ss"):
            return token[:-1]
        return token

    @staticmethod
    def _stem_norm(stem: str) -> str:
        # 'us'=='use', 'writ'=='write', 'runn'=='run' — silent-e and
        # doubled-final-consonant differences must not split a match.
        stem = stem.rstrip("e")
        while len(stem) > 2 and stem[-1] == stem[-2] and stem[-1].isalpha():
            stem = stem[:-1]
        return stem

    def _rule_terms(self, text: str) -> set[str]:
        return {
            stem for token in re.findall(r"[a-z0-9]+", text.casefold())
            if (stem := self._stem_token(token))
            and stem not in self._RULE_STEMS_DROP
        }

    def _revoke_rules(self, action: str) -> list[dict[str, Any]]:
        """Retire active behavior rules whose terms cover every content
        stem of the revoked action. Requires real specificity — 'stop it'
        must not wipe rules."""
        terms = self._rule_terms(action)
        if not terms or (len(terms) == 1 and len(next(iter(terms))) < 4):
            return []
        normed = {self._stem_norm(t) for t in terms}
        revoked: list[dict[str, Any]] = []
        for row in self._data.get("behavior_rules", []):
            if not isinstance(row, dict) or not row.get("active", True):
                continue
            text = str(row.get("text", ""))
            # A rule that already *prohibits* the action agrees with the
            # revocation — retiring it would silently permit the action.
            if re.match(
                    r"^(?:never|no\b|not\b|don't|do\s+not|avoid|stop|"
                    r"refrain|without)", text.strip(), flags=re.IGNORECASE):
                continue
            rule_normed = {
                self._stem_norm(t)
                for t in self._rule_terms(text)
            }
            if normed <= rule_normed:
                row["active"] = False
                row["updated_at"] = time.time()
                revoked.append(dict(row))
        return revoked

    def forget(self, query: str) -> list[dict[str, Any]]:
        target = self._clean(query).casefold()
        # "the editor fact" / "about my editor" — filler words around the
        # real referent must not break the substring match.
        normalized = re.sub(
            r"^(?:(?:the|that|about|my|our|a|an|fact|facts|memory)\s+)+",
            "", target).strip()
        probes = [p for p in (target, normalized) if p]
        if not probes:
            return []
        forgotten: list[dict[str, Any]] = []
        with self._lock:
            for key in ("facts", "behavior_rules"):
                for row in self._data.get(key, []):
                    if not isinstance(row, dict) or not row.get("active", True):
                        continue
                    text = str(row.get("text", ""))
                    folded = text.casefold()
                    if any(p in folded or folded in p for p in probes):
                        row["active"] = False
                        row["updated_at"] = time.time()
                        forgotten.append(dict(row))
            if forgotten:
                self._save()
        return forgotten

    def prompt_context(
        self,
        query: str = "",
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

        # Relevance gating (§4): when a user turn is supplied, only facts that
        # share a content term with it enter the prompt — the whole store must
        # not ride every message. An empty query returns the full scoped view
        # for explicit memory inspection/management paths.
        q_terms = self._content_terms(query)
        with self._lock:
            fact_rows = [
                row for row in self._data.get("facts", [])
                if isinstance(row, dict) and applies(row)
            ]
            if q_terms:
                fact_rows = [
                    row for row in fact_rows
                    if self._content_terms(str(row.get("text", ""))) & q_terms
                ]
            facts = [str(row.get("text", "")) for row in fact_rows][-12:]
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
