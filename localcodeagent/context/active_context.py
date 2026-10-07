"""Active conversation context — the compact working state that survives
turns and restarts.

Lives on the ConversationManager row so persistence, per-conversation
isolation, and crash recovery come free. Routing reads the ACTIVE fields;
reference resolution also consults RECENT entities (a topic shift retires
an image task from driving routing but keeps it retrievable — "what was
wrong with the last image?" still resolves).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from typing import Any

# Task-level context expires — a stale image task from yesterday must not
# drive today's routing. Recent entities expire much later; "the last
# image" is a fair referent for days.
ACTIVE_TTL_S = 6 * 3600
ENTITY_TTL_S = 72 * 3600
MAX_RECENT_ENTITIES = 12


@dataclass
class ActiveContext:
    """Compact per-conversation working state (spec §9)."""

    active_intent: str = ""
    active_subject: str = ""
    active_artifact: str = ""
    active_workspace: str = ""
    active_project: str = ""
    active_branch: str = ""
    active_image_job: str = ""
    active_image_prompt: str = ""
    active_image_subject: str = ""
    image_attributes: list[str] = field(default_factory=list)
    pending_clarification: str = ""
    pending_intent: str = ""
    pending_subject: str = ""
    pending_prompt: str = ""
    last_successful_action: str = ""
    last_error: str = ""
    # The unresolved failure anaphoric repair binds to ("fix that error").
    active_error: str = ""
    # [{kind, label, job, ts}] — topic-shifted artifacts stay retrievable
    # without driving routing.
    recent_entities: list[dict[str, Any]] = field(default_factory=list)
    updated_at: float = 0.0
    version: int = 1

    # -- queries ----------------------------------------------------------

    @property
    def last_intent(self) -> str:
        """The task intent still driving this conversation's context."""
        return self.active_intent

    def image_active(self, *, now: float | None = None) -> bool:
        """True when an image task is still the live working context."""
        if not (self.active_image_prompt or self.active_image_job
                or self.active_image_subject):
            return False
        now = time.time() if now is None else now
        return (now - float(self.updated_at or 0)) < ACTIVE_TTL_S

    def pending(self, *, now: float | None = None) -> bool:
        if not self.pending_clarification:
            return False
        now = time.time() if now is None else now
        return (now - float(self.updated_at or 0)) < ACTIVE_TTL_S

    # -- mutation ---------------------------------------------------------

    def touch(self) -> None:
        self.updated_at = time.time()

    def record_turn(self, env: Any) -> None:
        """Fold an IntentEnvelope into working state — topic shifts and
        corrections handled by intent kind, newest instruction always
        wins over parked state (context weighting §14)."""
        self.touch()
        intent = getattr(env, "primary_intent", "")
        self.active_intent = intent
        if intent in {"image_generation", "image_edit"}:
            self.active_image_prompt = getattr(env, "subject", "") or \
                self.active_image_prompt
            self.active_image_subject = getattr(env, "subject", "") or \
                self.active_image_subject
            if getattr(env, "attributes", None):
                self.image_attributes = list(env.attributes)
            self.pending_clarification = getattr(
                env, "needs_clarification", "") or ""
            if self.pending_clarification:
                self.pending_intent = intent
                self.pending_subject = self.active_image_subject
                self.pending_prompt = self.active_image_prompt
        elif intent == "image_followup":
            # Preserve the established subject — the fragment only
            # modifies attributes (spec §7).
            if getattr(env, "followup_prompt", ""):
                mods = self.image_attributes + [env.followup_prompt]
                self.image_attributes = mods[-12:]
        elif intent == "clarification_response":
            self.pending_clarification = ""
            self.pending_intent = ""
            self.pending_subject = ""
            self.pending_prompt = ""
        elif intent == "feedback_signal":
            pass  # failure metadata handled by the recorder, not context
        elif intent in {"tool_action", "git_action", "github_status",
                        "file_edit", "coding", "research", "writing"}:
            # Topic shift — the image task retires from routing but its
            # subject stays resolvable as "the last image" (§10, §32.10).
            if self.active_image_subject:
                self._retire("image", self.active_image_subject,
                             self.active_image_job)
                self.active_image_job = ""
                self.active_image_prompt = ""
                self.active_image_subject = ""
                self.image_attributes = []
            # A parked image clarification dies with its task — a later
            # "yes" must resolve the CURRENT offer, not the retired one.
            self.pending_clarification = ""
            self.pending_intent = ""
            self.pending_subject = ""
            self.pending_prompt = ""
            self.active_subject = getattr(env, "subject", "")[:200]
        # conversation/question turns leave task context untouched — a
        # chat aside isn't a topic shift.

    def note_image_jobs(self, job_ids: list[str]) -> None:
        if job_ids:
            self.active_image_job = str(job_ids[0])
            self.touch()

    def note_error(self, error: str) -> None:
        """Bind the latest failure so "fix that error" resolves. Cleared
        explicitly when a follow-up task succeeds, not by time — a stale
        error is better than a lost referent."""
        self.active_error = str(error or "")[:300]
        self.last_error = self.active_error
        self.touch()

    def clear_error(self) -> None:
        self.active_error = ""

    def park_clarification(self, kind: str, *, intent: str = "",
                           subject: str = "", prompt: str = "") -> None:
        self.pending_clarification = kind
        self.pending_intent = intent
        self.pending_subject = subject
        self.pending_prompt = prompt
        self.touch()

    def _retire(self, kind: str, label: str, job: str = "") -> None:
        if not label:
            return
        self.recent_entities = [
            e for e in self.recent_entities
            if not (e.get("kind") == kind and e.get("label") == label)
        ]
        self.recent_entities.insert(0, {
            "kind": kind, "label": label, "job": job,
            "ts": time.time(),
        })
        self.recent_entities = self.recent_entities[:MAX_RECENT_ENTITIES]

    def entities(self, *, now: float | None = None) -> list[dict[str, Any]]:
        now = time.time() if now is None else now
        return [e for e in self.recent_entities
                if (now - float(e.get("ts") or 0)) < ENTITY_TTL_S]

    # -- serialization ----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "ActiveContext":
        if not isinstance(raw, dict):
            return cls()
        known = {f for f in cls.__dataclass_fields__}
        kwargs = {k: v for k, v in raw.items() if k in known}
        try:
            return cls(**kwargs)
        except TypeError:
            return cls()

    def describe(self) -> str:
        """One compact line for prompt context / trace — never prose."""
        parts = []
        if self.active_intent:
            parts.append(f"intent={self.active_intent}")
        if self.active_image_subject:
            parts.append(f"image_subject={self.active_image_subject[:120]}")
        if self.active_image_job:
            parts.append(f"image_job={self.active_image_job}")
        if self.pending_clarification:
            parts.append(f"awaiting={self.pending_clarification}")
        return "; ".join(parts)
