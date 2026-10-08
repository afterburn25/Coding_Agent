"""Deterministic conversation-QA harness (backlog §5-§7).

Runs scripted multi-turn scenarios through AgentOrchestrator with a
stubbed model provider, evaluating per-turn behavior assertions against
what the orchestrator actually did — normalized text reaching the model,
memory injection, routing, tool use, task lifecycle. Discovered failures
are recorded in a permanent corpus (corpus.py) that becomes regression
coverage.
"""

from .conversation import (
    QaScenario,
    QaTurn,
    QaRunResult,
    TurnResult,
    ConversationQaRunner,
    ScriptedProvider,
    generate_scenarios,
    generate_hard_scenarios,
    generate_scope_scenarios,
)
from .corpus import FailureCorpus, CorpusEntry

__all__ = [
    "QaScenario",
    "QaTurn",
    "QaRunResult",
    "TurnResult",
    "ConversationQaRunner",
    "ScriptedProvider",
    "generate_scenarios",
    "generate_hard_scenarios",
    "generate_scope_scenarios",
    "FailureCorpus",
    "CorpusEntry",
]
