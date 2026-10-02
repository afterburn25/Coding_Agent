"""Specialist brains — standard interface, shared global infrastructure.

Specialists do NOT each duplicate the global architecture. A specialist
brain declares its domain (planning context, domain tools, preferred model
capabilities, policy, evaluator) and shares the global Corpus Callosum,
Hippocampus, Brain Stem, approval framework, and resource scheduler.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

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


class SpecialistBrain:
    """Runtime wrapper — a specialist owns domain state but shares the
    global bus/memory/brainstem; its model requests go through the
    Thalamus as capability requirements, never hard names."""

    def __init__(self, spec: SpecialistSpec, bus, hippocampus=None,
                 motor=None) -> None:
        self.spec = spec
        self.bus = bus
        self.hippocampus = hippocampus
        self.motor = motor
        self.local_context: dict[str, Any] = {}
        self.benchmarks: dict[str, float] = {}

    def status(self) -> dict[str, Any]:
        return {"name": self.spec.name, "domain": self.spec.domain,
                "capabilities": list(self.spec.capabilities),
                "preferred": {"coding": self.spec.preferred_requirement.coding,
                              "vision": self.spec.preferred_requirement.vision,
                              "quality": self.spec.preferred_requirement.quality}}
