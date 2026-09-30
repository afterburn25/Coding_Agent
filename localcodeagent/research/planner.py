from __future__ import annotations

import re

from ..workflow.repository import RepositoryIndex
from .environment import EnvironmentInspector
from .types import ResearchPlan, ResearchQuestion


ERROR_MARKERS = re.compile(r"\b(error|exception|traceback|failed|failure|undefined reference|cannot find|not found|segfault|crash|incompatible)\b", re.I)
CHANGE_MARKERS = re.compile(r"\b(latest|current|newest|recent|release|version|deprecated|deprecation|compatib|api|sdk|library|framework|driver|cuda)\b", re.I)
DEEP_MARKERS = re.compile(r"\b(architecture|major refactor|migration|race condition|deadlock|security|unknown|unfamiliar|integration)\b", re.I)
SIMPLE_LOCAL_MARKERS = re.compile(r"\b(rename|format|typo|comment|text|label|css|spacing)\b", re.I)


class KnowledgeGapDetector:
    def __init__(self, index: RepositoryIndex, inspector: EnvironmentInspector) -> None:
        self.index = index
        self.inspector = inspector

    def analyze(self, task: str, requested_mode: str = "auto") -> ResearchPlan:
        environment = self.inspector.inspect()
        local_hits = self.index.search(task, limit=8) if self.index.ensure().get("file_count") else []
        reasons: list[str] = []
        questions: list[ResearchQuestion] = []
        error = bool(ERROR_MARKERS.search(task))
        changing = bool(CHANGE_MARKERS.search(task))
        deep = bool(DEEP_MARKERS.search(task))
        simple_local = bool(SIMPLE_LOCAL_MARKERS.search(task)) and bool(local_hits) and not changing and not error

        if error:
            reasons.append("task contains an error/compatibility signal that may be version-specific")
            questions.append(ResearchQuestion("What is the root cause of the exact error in this project/version?", "error", 100, True))
            questions.append(ResearchQuestion("Is the error documented in official docs or upstream issues for the installed version?", "error", 90, True))
        if changing:
            reasons.append("task references APIs/versions/current behavior that may have changed")
            questions.append(ResearchQuestion("What does the authoritative documentation say for the version used by this project?", "official_docs", 95, True))
        if deep:
            reasons.append("task involves an unfamiliar or high-impact subsystem")
            questions.append(ResearchQuestion("What are the official integration/architecture requirements and known constraints?", "architecture", 85, True))
        if not local_hits:
            reasons.append("repository index has little direct evidence for the requested topic")
            questions.append(ResearchQuestion("What authoritative API or implementation information is missing locally?", "general", 75, True))
        elif not simple_local:
            questions.append(ResearchQuestion("Does this repository already contain a compatible implementation pattern that should be extended?", "repository", 100, False))

        if simple_local and requested_mode in {"auto", "local_only", "offline"}:
            needed = False
            reasons = ["request appears locally answerable from existing repository evidence"]
        else:
            needed = bool(error or changing or deep or not local_hits)

        mode = requested_mode
        if mode == "auto":
            if not needed:
                mode = "local_only"
            elif deep or error:
                mode = "deep"
            elif changing:
                mode = "official"
            else:
                mode = "balanced"
        aliases = {"official_sources": "official", "deep_research": "deep", "no_research": "none", "local": "local_only"}
        mode = aliases.get(mode, mode)
        if mode == "none":
            needed = False
        if not questions and needed:
            questions.append(ResearchQuestion("What facts must be verified before implementation?", "general", 70, False))
        return ResearchPlan(task=task, mode=mode, needed=needed, reasons=reasons, questions=questions, environment=environment, local_hits=local_hits)
