"""Specialist brains — standard interface, shared global infrastructure.

Specialists do NOT each duplicate the global architecture. A specialist
brain declares its domain (planning context, domain tools, preferred model
capabilities, policy, evaluator) and shares the global Corpus Callosum,
Hippocampus, Brain Stem, approval framework, and resource scheduler.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .events import CognitiveEvent, EventType
from .regions import BrainRegion
from .thalamus import ModelRequirement


@dataclass(slots=True)
class SpecialistSpec:
    """Declarative specialist profile — wiring to real subsystems happens
    in core.NexusBrain, which hands each specialist the global services."""
    name: str                        # coding_brain, research_brain, ...
    domain: str                      # coding|research|vision|review|systems|language
    preferred_requirement: ModelRequirement = field(default_factory=ModelRequirement)
    capabilities: tuple[str, ...] = ()   # tool capabilities it may request
    policy: dict[str, Any] = field(default_factory=dict)
    description: str = ""


SPECIALISTS: tuple[SpecialistSpec, ...] = (
    SpecialistSpec(
        name="coding_brain", domain="coding",
        preferred_requirement=ModelRequirement(coding=True, quality="auto"),
        capabilities=("repo_search", "read_file", "edit_file", "run_shell",
                      "run_tests", "git"),
        description="Code comprehension, editing, debugging, verification."),
    SpecialistSpec(
        name="research_brain", domain="research",
        preferred_requirement=ModelRequirement(reasoning=True, quality="auto"),
        capabilities=("web_search", "fetch_url", "browser_run", "research_topic"),
        description="External information gathering and synthesis."),
    SpecialistSpec(
        name="vision_brain", domain="vision",
        preferred_requirement=ModelRequirement(vision=True, quality="high"),
        capabilities=("generate_image", "screenshot", "computer_use"),
        description="Image generation planning and visual understanding."),
    SpecialistSpec(
        name="reviewer_brain", domain="review",
        preferred_requirement=ModelRequirement(coding=True, quality="high"),
        capabilities=("repo_search", "read_file", "run_tests"),
        description="Independent verification and edge-case review."),
    SpecialistSpec(
        name="systems_brain", domain="systems",
        preferred_requirement=ModelRequirement(quality="low"),
        capabilities=("system_status", "process_list", "health"),
        description="Runtime, installer, environment, and OS operations."),
    SpecialistSpec(
        name="language_brain", domain="language",
        preferred_requirement=ModelRequirement(quality="low",
                                               max_latency="realtime"),
        capabilities=(),
        description="Comprehension, intent, response generation, style."),
)


class SpecialistBrain(BrainRegion):
    """Runtime wrapper — a specialist owns domain state but shares the
    global bus/memory/brainstem; its model requests go through the
    Thalamus as capability requirements, never hard names."""

    def __init__(self, spec: SpecialistSpec, bus, hippocampus=None,
                 motor=None) -> None:
        self.spec = spec
        self.name = spec.name          # instance attr before BrainRegion init
        self.hippocampus = hippocampus
        self.motor = motor
        self.local_context: dict[str, Any] = {}
        self.benchmarks: dict[str, float] = {}
        super().__init__(bus)

    # -- addressed work ---------------------------------------------------------------
    def handle(self, event: CognitiveEvent) -> None:
        """Domain memory + context: specialists keep their own episodic
        slice of the shared Hippocampus (kind = '<domain>:outcome') and
        answer addressed memory queries with domain-first recall."""
        if event.type == EventType.MEMORY_QUERY and self.hippocampus is not None:
            res = self.hippocampus.recall(
                str(event.content.get("query") or ""),
                kinds={"episodic", "procedural"},
                project_id=str(event.content.get("project_id") or ""),
                limit=6)
            domain = [e for e in res.entries
                      if self.spec.domain in str(
                          getattr(e, "provenance", "") or "") or
                      str(getattr(e, "kind", "")).startswith(
                          self.spec.domain)]
            picked = (domain or res.entries)[:6]
            self.respond(event, EventType.MEMORY_RESULT, {
                "domain": self.spec.domain,
                "entries": [e.as_dict() for e in picked]})
        elif event.type in (EventType.EXECUTION_RESULT, EventType.MODEL_RESULT) \
                and self.hippocampus is not None:
            c = event.content
            subject = c.get("action") or c.get("model_id") or event.type
            self.hippocampus.record_episode(
                f"{self.spec.domain}:outcome",
                f"{subject} → {'ok' if c.get('ok', True) else 'failed'}",
                detail=json.dumps(c, default=str)[:1000],
                mission_id=event.mission_id, task_id=event.task_id)
        elif event.type == EventType.ACTION_SELECTION:
            self.local_context["last_selection"] = dict(event.content)
        elif event.type == EventType.LEARNING_EVENT:
            subject = str(event.content.get("subject") or "")
            value = event.content.get("value")
            if subject and isinstance(value, (int, float)):
                self.benchmarks[subject] = float(value)

    def request_capability(self, requirement: ModelRequirement, *,
                           timeout: float = 10.0) -> str:
        """Ask the Thalamus for a model by capability — never by name."""
        resp = self.bus.request(CognitiveEvent(
            type=EventType.MODEL_REQUEST, source=self.name,
            destination="thalamus",
            content={"coding": requirement.coding,
                     "vision": requirement.vision,
                     "reasoning": requirement.reasoning,
                     "min_context": requirement.min_context,
                     "quality": requirement.quality,
                     "max_latency": requirement.max_latency}),
            timeout=timeout)
        return str(resp.content.get("model_id") or "") if resp else ""

    def status(self) -> dict[str, Any]:
        base = super().status()
        base.update({
            "domain": self.spec.domain,
            "capabilities": list(self.spec.capabilities),
            "benchmarks": dict(self.benchmarks),
            "preferred": {"coding": self.spec.preferred_requirement.coding,
                          "vision": self.spec.preferred_requirement.vision,
                          "quality": self.spec.preferred_requirement.quality}})
        return base
