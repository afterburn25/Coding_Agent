from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Callable

from ..config import ModelProfile
from .tiers import canonical_role, tier_for_role


ROLES = ("utility", "lightweight_reasoner", "fast_coder", "primary_coder",
         "deep_reasoner", "reviewer", "vision")


def _role_matches(model: ModelProfile, role: str) -> bool:
    """Canonical-role matching: a 'lightweight_reasoner' request is served by
    models configured with any tier-2 role (light_coder, general_assistant…),
    and 'fast_coder' resolves to the primary-coder ladder rung."""
    if role in model.roles:
        return True
    target = canonical_role(role)
    return any(canonical_role(r) == target for r in model.roles)


def _tier_distance(model: ModelProfile, want: int) -> int:
    """Closest ladder-tier distance between the model's roles and `want`."""
    if not model.roles:
        return 99
    return min(abs(tier_for_role(r) - want) for r in model.roles)
ResourceAdvisor = Callable[[ModelProfile], tuple[bool, int, str]]
PerformanceAdvisor = Callable[[ModelProfile, str, int], tuple[int, str]]


@dataclass(slots=True)
class RoutingDecision:
    role: str
    model_id: str
    reasons: list[str]
    complexity: int


class ModelRouter:
    """Capability + resource-aware router for local models."""

    def __init__(
        self,
        models: list[ModelProfile],
        resource_advisor: ResourceAdvisor | None = None,
        performance_advisor: PerformanceAdvisor | None = None,
    ) -> None:
        self.models = list(models)
        # Routing pool excludes disabled profiles, but an all-disabled config
        # must not brick the app — selection errors per request instead.
        self.enabled_models = [m for m in models if m.enabled]
        self.resource_advisor = resource_advisor
        self.performance_advisor = performance_advisor

    def classify_role(self, text: str, *, phase: str = "work", changed_files: int = 0, failures: int = 0) -> tuple[str, int, list[str]]:
        t = text.lower().strip()
        score = 1
        reasons: list[str] = []

        casual_exact = {
            "hi", "hello", "hey", "hey there", "good morning", "good afternoon",
            "good evening", "thanks", "thank you", "who are you", "what are you",
            "what can you do", "what all can you do", "help", "help me",
        }
        normalized = re.sub(r"[!?.,]+$", "", t).strip()
        coding_signals = (
            "code", "debug", "fix", "build", "implement", "refactor", "error",
            "file", "repo", "repository", "project", "test", "compile", "function",
            "class", "api", "database", "backend", "frontend", "git", "github",
        )
        capability_question = (
            len(t) <= 180
            and any(phrase in t for phrase in (
                "what can you do", "what all can you do", "what are your capabilities",
                "what do you do", "how can you help",
            ))
            and not any(signal in t for signal in coding_signals)
        )
        if normalized in casual_exact or capability_question:
            return "utility", 0, ["lightweight conversational request"]

        current_info_signals = (
            "latest", "latest version", "current version", "current release",
            "today's news", "today’s news", "weather today", "news",
            "look up", "lookup", "search the web", "online", "release version",
        )
        work_signals = (
            "build", "create", "implement", "write code", "edit", "change",
            "rename", "improve", "debug", "fix", "refactor", "test", "compile",
            "repository", "repo", "project", "file", "backend", "frontend",
            "database", "api", "github", "git", "function", "class", "script",
            "website", "webpage", "button", "chat nexus",
        )
        # Tool-requiring intents must never land on the utility lane — utility
        # sessions carry no tool schemas. Route them to the lightest
        # tool-capable tier instead.
        tool_signals = (
            "generate an image", "generate a picture", "generate a photo",
            "make an image", "make a picture", "make a photo", "create an image",
            "create a picture", "draw ", "paint ", "sketch ", "render ",
            "image of", "picture of", "photo of", "pic of", "wallpaper",
            "generate a pic", "make a pic", "image generation", "image of a",
        )
        if any(signal in t for signal in tool_signals):
            return "light_coder", 1, ["tool-using request needs a tool-capable lane"]

        if (
            len(t) <= 320
            and not any(signal in t for signal in current_info_signals)
            and not any(signal in t for signal in work_signals)
        ):
            return "utility", 0, ["short non-coding conversation"]

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
            # Lightest tool-capable model first — the utility lane carries no
            # tools, so simple-but-real work lands on light_coder (8B); the
            # heavier tiers only engage when complexity or failures demand it.
            return "light_coder", score, reasons or ["low-complexity request"]
        if score >= 6:
            return "deep_reasoner", score, reasons or ["high-complexity task"]
        return "primary_coder", score, reasons or ["general coding task"]

    def choose(
        self,
        text: str,
        *,
        phase: str = "work",
        changed_files: int = 0,
        failures: int = 0,
        override: str | None = None,
        exclude_model_ids: set[str] | None = None,
    ) -> RoutingDecision:
        excluded = set(exclude_model_ids or ())
        available_models = [model for model in self.enabled_models if model.id not in excluded]
        if not available_models:
            raise ValueError("No enabled model remains after excluding failed candidates")

        if override and override != "auto":
            role = override if override in ROLES else "primary_coder"
            complexity = 0
            reasons = ["manual role override"]
        else:
            role, complexity, reasons = self.classify_role(text, phase=phase, changed_files=changed_files, failures=failures)

        candidates = [m for m in available_models if _role_matches(m, role)]
        if not candidates:
            primary = [m for m in available_models
                       if _role_matches(m, "primary_coder")]
            if primary:
                candidates = primary
            else:
                # Degrade toward the nearest ladder tier rather than an
                # arbitrary model — a light-coder task on an 8B+4B
                # machine should land on the 8B, not the 4B utility.
                want = tier_for_role(role)
                best = min((_tier_distance(m, want) for m in available_models),
                           default=99)
                candidates = [m for m in available_models
                              if _tier_distance(m, want) == best] \
                    or available_models
            reasons.append(f"no dedicated {role} model configured; using fallback")

        ranked: list[tuple[bool, int, int, ModelProfile, str, str]] = []
        for model in candidates:
            if self.resource_advisor:
                fits, resource_score, resource_reason = self.resource_advisor(model)
            else:
                fits, resource_score, resource_reason = True, 0, ""
            if self.performance_advisor:
                performance_score, performance_reason = self.performance_advisor(model, role, complexity)
            else:
                performance_score, performance_reason = 0, ""
            ranked.append((fits, resource_score, performance_score, model, resource_reason, performance_reason))

        fitting = [item for item in ranked if item[0]]
        if fitting:
            pool = fitting
        else:
            fallback_candidates = [m for m in available_models if _role_matches(m, "primary_coder") and m not in candidates]
            fallback_ranked: list[tuple[bool, int, int, ModelProfile, str, str]] = []
            for model in fallback_candidates:
                if self.resource_advisor:
                    fits, resource_score, resource_reason = self.resource_advisor(model)
                else:
                    fits, resource_score, resource_reason = True, 0, ""
                if self.performance_advisor:
                    performance_score, performance_reason = self.performance_advisor(model, role, complexity)
                else:
                    performance_score, performance_reason = 0, ""
                fallback_ranked.append((fits, resource_score, performance_score, model, resource_reason, performance_reason))
            fallback_fitting = [item for item in fallback_ranked if item[0]]
            if fallback_fitting:
                pool = fallback_fitting
                reasons.append(f"no runnable dedicated {role} model; using runnable primary-coder fallback")
            else:
                pool = ranked
        pool.sort(key=lambda item: (-item[1], -item[2], -item[3].priority, -item[3].context_window, item[3].id))
        fits, _, _, chosen, resource_reason, performance_reason = pool[0]
        if resource_reason:
            reasons.append(f"{chosen.id}: {resource_reason}")
        if performance_reason:
            reasons.append(performance_reason)
        if not fits:
            reasons.append("no configured candidate is estimated to fit current resources; trying best fallback")
        return RoutingDecision(role=role, model_id=chosen.id, reasons=reasons, complexity=complexity)

    def get_profile(self, model_id: str) -> ModelProfile:
        for model in self.models:
            if model.id == model_id:
                return model
        raise KeyError(model_id)
