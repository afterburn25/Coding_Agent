"""Structured turn understanding — intent envelope, active context,
reference resolution.

The newest user request is the dominant instruction. Persona, identity,
memory, and style help answer it; they never replace it. This package is
the single place that decides WHAT a turn wants before any lane decides
HOW to answer it.
"""

from .intent import (
    IntentEnvelope,
    understand_turn,
    detect_image_intent,
    strip_image_scaffold,
    IMAGE_GENERATION,
    IMAGE_EDIT,
    IMAGE_FOLLOWUP,
    TOOL_ACTION,
    GIT_ACTION,
    FILE_EDIT,
    CODING,
    RESEARCH,
    WRITING,
    QUESTION,
    IDENTITY_QUERY,
    CLARIFICATION_RESPONSE,
    CORRECTION,
    FEEDBACK_SIGNAL,
    CONVERSATION,
    ACTION_INTENTS,
)
from .active_context import ActiveContext
from .references import resolve_references, REFERENCE_TERMS

__all__ = [
    "IntentEnvelope",
    "understand_turn",
    "detect_image_intent",
    "strip_image_scaffold",
    "ActiveContext",
    "resolve_references",
    "REFERENCE_TERMS",
    "IMAGE_GENERATION",
    "IMAGE_EDIT",
    "IMAGE_FOLLOWUP",
    "TOOL_ACTION",
    "GIT_ACTION",
    "FILE_EDIT",
    "CODING",
    "RESEARCH",
    "WRITING",
    "QUESTION",
    "IDENTITY_QUERY",
    "CLARIFICATION_RESPONSE",
    "CORRECTION",
    "FEEDBACK_SIGNAL",
    "CONVERSATION",
    "ACTION_INTENTS",
]
