"""Nexus Brain — modular cognitive architecture.

The LLM is not the brain. Models are interchangeable inference resources
selected by the Thalamus; persistent cognition lives in the regions below:

    NexusBrain (core)
    ├── CorpusCallosum      typed event/message bus
    ├── PrefrontalCortex    goals, plans, replanning, completion assessment
    ├── Hippocampus         episodic/semantic/procedural/project memory
    ├── Thalamus            input routing, attention, model selection
    ├── BasalGanglia        action selection and scoring
    ├── MotorCortex         controlled execution + structured telemetry
    └── BrainStem           health, watchdogs, recovery — no LLM required

Specialist brains (coding, research, vision, reviewer, systems, language)
share the global bus, memory, and Brain Stem while keeping local state.
"""
from .core import NexusBrain
from .events import CognitiveEvent, EventType
from .bus import CorpusCallosum

__all__ = ["NexusBrain", "CognitiveEvent", "EventType", "CorpusCallosum"]
