"""Cognitive event types — the Corpus Callosum envelope.

Every message between regions is a strongly typed CognitiveEvent carrying
correlation, provenance, confidence, and priority so the whole path of an
input through Nexus can be traced structurally (never raw model reasoning).
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any


class EventType:
    """Canonical cognitive message types."""
    OBSERVATION = "observation"
    MEMORY_QUERY = "memory_query"
    MEMORY_RESULT = "memory_result"
    PLAN = "plan"
    ACTION_CANDIDATE = "action_candidate"
    ACTION_SELECTION = "action_selection"
    EXECUTION_REQUEST = "execution_request"
    EXECUTION_RESULT = "execution_result"
    CONFLICT_DETECTED = "conflict_detected"
    HEALTH_EVENT = "health_event"
    RISK_EVENT = "risk_event"
    MODEL_REQUEST = "model_request"
    MODEL_RESULT = "model_result"
    LEARNING_EVENT = "learning_event"
    MISSION_EVENT = "mission_event"
    ATTENTION = "attention_event"


class Priority:
    LOW = 10
    NORMAL = 50
    HIGH = 80
    CRITICAL = 100


# Well-known region names (stable wire identifiers — not class names).
REGION_THALAMUS = "thalamus"
REGION_PFC = "prefrontal_cortex"
REGION_HIPPOCAMPUS = "hippocampus"
REGION_BASAL_GANGLIA = "basal_ganglia"
REGION_MOTOR = "motor_cortex"
REGION_CEREBELLUM = "cerebellum"
REGION_BRAINSTEM = "brain_stem"
REGION_LIMBIC = "limbic"
REGION_CORE = "nexus_brain"

ALL_REGIONS = (
    REGION_THALAMUS, REGION_PFC, REGION_HIPPOCAMPUS, REGION_BASAL_GANGLIA,
    REGION_MOTOR, REGION_CEREBELLUM, REGION_BRAINSTEM,
)


@dataclass(slots=True)
class CognitiveEvent:
    """Immutable-ish envelope exchanged on the Corpus Callosum."""
    type: str
    source: str
    destination: str = ""          # "" = broadcast to subscribers of type
    content: dict[str, Any] = field(default_factory=dict)
    correlation_id: str = ""
    mission_id: str = ""
    conversation_id: str = ""
    task_id: str = ""
    confidence: float = 1.0
    priority: int = Priority.NORMAL
    provenance: str = ""           # e.g. "tool:run_shell", "memory:episodic"
    trace_id: str = ""
    retry_of: str = ""             # event id this retries
    ts: float = field(default_factory=time.time)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])

    def reply(self, type_: str, content: dict[str, Any], **kw) -> "CognitiveEvent":
        """Build a response addressed back to this event's source, keeping
        correlation so requesters can match replies."""
        return CognitiveEvent(
            type=type_, source=self.destination or REGION_CORE,
            destination=self.source, correlation_id=self.correlation_id,
            mission_id=self.mission_id, conversation_id=self.conversation_id,
            task_id=self.task_id, content=content, **kw)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "type": self.type, "source": self.source,
            "destination": self.destination, "correlation_id": self.correlation_id,
            "mission_id": self.mission_id, "conversation_id": self.conversation_id,
            "task_id": self.task_id, "confidence": self.confidence,
            "priority": self.priority, "provenance": self.provenance,
            "trace_id": self.trace_id, "retry_of": self.retry_of,
            "ts": self.ts, "content": self.content,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CognitiveEvent":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})
