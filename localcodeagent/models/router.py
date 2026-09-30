from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Callable

from ..config import ModelProfile


ROLES = ("utility", "fast_coder", "primary_coder", "deep_reasoner", "reviewer", "vision")
ResourceAdvisor = Callable[[ModelProfile], tuple[bool, int, str]]


@dataclass(slots=True)
class RoutingDecision:
    role: str
    model_id: str
    reasons: list[str]
    complexity: int


class ModelRouter:
    """Capability + resource-aware router for local models."""

    def __init__(self, models: list[ModelProfile], resource_advisor: ResourceAdvisor | None = None) -> None:
        self.models = [m for m in models if m.enabled]
        if not self.models:
            raise ValueError("At least one enabled model profile is required")
        self.resource_advisor = resource_advisor

    def classify_role(self, text: str, *, phase: str = "work", changed_files: int = 0, failures: int = 0) -> tuple[str, int, list[str]]:
        t = text.lower()
        score = 1
        reasons: list[str] = []

        deep_terms = [
            "architecture", "refactor", "race condition", "deadlock", "memory leak",
            "security", "migration", "dependency conflict", "compiler errors",
            "distributed", "concurrency", "multi-thread", "crash", "root cause",
        ]
        simple_terms = ["rename", "typo", "format", "comment", "button text", "small change"]
        multi_terms = ["multiple files", "whole project", "repository", "full stack", "database", "backend", "frontend"]

        if any(x in t for x in simple_terms):
            score -= 1
            reasons.append("request appears narrowly scoped")
        if any(x in t for x in multi_terms):
            score += 2
            reasons.append("request spans multiple project areas")
        deep_matches = sum(1 for x in deep_terms if x in t)
        if deep_matches:
            score += min(5, 2 + deep_matches)
            reasons.append(f"request contains {deep_matches} deep reasoning/debugging signal(s)")
        if len(text) > 700:
            score += 1
            reasons.append("request is detailed")
        if len(re.findall(r"\b(and|also|then|after|before)\b", t)) >= 4:
            score += 1
            reasons.append("request contains several dependent steps")
        if changed_files >= 6:
            score += 2
            reasons.append("many files have changed")
        if failures >= 2:
            score += 3
            reasons.append("previous attempts failed; escalating")

        score = max(0, min(score, 10))
        if phase == "review":
            return "reviewer", score, reasons + ["review phase"]
        if any(term in t for term in ("screenshot", "analyze this image", "look at this image", "attached image", "vision task")):
            return "vision", score, reasons + ["visual-input understanding requested"]
        if score <= 1:
            return "fast_coder", score, reasons or ["low-complexity coding task"]
        if score >= 6:
            return "deep_reasoner", score, reasons or ["high-complexity task"]
        return "primary_coder", score, reasons or ["general coding task"]

    def choose(self, text: str, *, phase: str = "work", changed_files: int = 0, failures: int = 0, override: str | None = None) -> RoutingDecision:
        if override and override != "auto":
            role = override if override in ROLES else "primary_coder"
            complexity = 0
            reasons = ["manual role override"]
        else:
            role, complexity, reasons = self.classify_role(text, phase=phase, changed_files=changed_files, failures=failures)

        candidates = [m for m in self.models if role in m.roles]
        if not candidates:
            candidates = [m for m in self.models if "primary_coder" in m.roles] or self.models
            reasons.append(f"no dedicated {role} model configured; using fallback")

        ranked: list[tuple[bool, int, ModelProfile, str]] = []
        for model in candidates:
            if self.resource_advisor:
                fits, resource_score, resource_reason = self.resource_advisor(model)
            else:
                fits, resource_score, resource_reason = True, 0, ""
            ranked.append((fits, resource_score, model, resource_reason))

        fitting = [item for item in ranked if item[0]]
        pool = fitting or ranked
        pool.sort(key=lambda item: (-item[1], -item[2].priority, -item[2].context_window, item[2].id))
        fits, _, chosen, resource_reason = pool[0]
        if resource_reason:
            reasons.append(f"{chosen.id}: {resource_reason}")
        if not fits:
            reasons.append("no configured candidate is estimated to fit current resources; trying best fallback")
        return RoutingDecision(role=role, model_id=chosen.id, reasons=reasons, complexity=complexity)

    def get_profile(self, model_id: str) -> ModelProfile:
        for model in self.models:
            if model.id == model_id:
                return model
        raise KeyError(model_id)
