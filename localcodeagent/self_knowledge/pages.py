"""Canonical UI page registry — one place where routes, their purpose,
and their deep-linkable sections are defined. Chat navigation and any
future UI help surface both read from here; nothing else may hard-code
routes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class PageSection:
    """A deep-linkable section inside a page (rendered as #anchor or a
    named tab)."""
    id: str
    name: str
    description: str = ""
    feature_ids: tuple[str, ...] = ()


@dataclass(slots=True)
class PageSpec:
    route: str
    title: str
    purpose: str
    aliases: tuple[str, ...] = ()
    sections: tuple[PageSection, ...] = ()
    feature_ids: tuple[str, ...] = ()

    def section(self, sid: str) -> PageSection | None:
        return next((s for s in self.sections
                     if s.id == sid or s.name.lower() == sid), None)

    def deep_link(self, section: str = "") -> str:
        if not section:
            return self.route
        s = self.section(section)
        return f"{self.route}#{s.id if s else section}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "route": self.route, "title": self.title,
            "purpose": self.purpose, "aliases": list(self.aliases),
            "sections": [
                {"id": s.id, "name": s.name, "description": s.description}
                for s in self.sections],
            "feature_ids": list(self.feature_ids),
        }


PAGES: list[PageSpec] = [
    PageSpec("/index.html", "Chat", "Talk with Nexus and issue work.",
             aliases=("home", "main", "chat page"),
             feature_ids=("chat",)),
    PageSpec("/settings.html", "Settings", "All configuration surfaces.",
             aliases=("settings", "preferences", "config"),
             sections=(
                 PageSection("profile", "Profile",
                             feature_ids=("profile",)),
                 PageSection("general", "General",
                             feature_ids=("startup_narration",)),
                 PageSection("permissions", "Permissions",
                             feature_ids=("permissions",)),
                 PageSection("models", "Models",
                             feature_ids=("model_management",)),
                 PageSection("appearance", "Appearance",
                             feature_ids=("appearance",)),
                 PageSection("privacy", "Privacy",
                             feature_ids=("conversation_memory",)),
                 PageSection("connections", "Connections",
                             feature_ids=("connectors", "github",
                                          "secrets")),
                 PageSection("notifications", "Notifications",
                             feature_ids=("notifications",)),
                 PageSection("voice", "Voice",
                             feature_ids=("voice", "stt")),
                 PageSection("setup", "Setup",
                             feature_ids=("provisioning",)),
                 PageSection("advanced", "Advanced",
                             feature_ids=("self_update", "backups",
                                          "skills")),
                 PageSection("creator", "Creator",
                             feature_ids=("profile",))),
             feature_ids=("profile", "permissions", "appearance",
                          "notifications", "secrets")),
    PageSpec("/models.html", "Models", "Model inventory, runtime state, "
             "installs, and routing.",
             aliases=("models", "model manager", "local models"),
             sections=(
                 PageSection("routing", "Routing",
                             feature_ids=("model_routing",)),
                 PageSection("runtime", "Runtime",
                             feature_ids=("model_management",)),),
             feature_ids=("model_management", "model_routing", "vision")),
    PageSpec("/image.html", "Image Studio", "Generate and manage images.",
             aliases=("images", "image studio", "pictures"),
             sections=(
                 PageSection("library", "Library",
                             feature_ids=("image_library",)),
                 PageSection("backends", "Backends",
                             feature_ids=("image_generation",)),
                 PageSection("models", "Image models",
                             feature_ids=("image_generation",)),),
             feature_ids=("image_generation", "image_library")),
    PageSpec("/voice.html", "Voice Studio", "Voice presets, devices, "
             "and speech settings.",
             aliases=("voice studio", "voice lab", "voice"),
             feature_ids=("voice",)),
    PageSpec("/personality.html", "Personality Studio",
             "Persona presets, custom personalities, moods, and the "
             "Speech Lab.",
             aliases=("personality", "persona", "speech lab"),
             sections=(
                 PageSection("speech-lab", "Speech Lab",
                             "How Nexus phrases and paces replies.",
                             ("speech_lab",)),),
             feature_ids=("persona", "speech_lab")),
    PageSpec("/command.html", "Command Center", "Workers, queue, "
             "autonomy, activity, and runtime controls.",
             aliases=("command center", "operations", "workers page"),
             sections=(
                 PageSection("workers", "Workers",
                             feature_ids=("workers",)),
                 PageSection("queue", "Queue", feature_ids=("queue",)),
                 PageSection("autonomy", "Autonomy",
                             feature_ids=("autonomy",)),
                 PageSection("activity", "Activity",
                             feature_ids=("activity",)),),
             feature_ids=("workers", "queue", "autonomy", "activity",
                          "dev_servers", "build_toolchain", "temp_specialists")),
    PageSpec("/missions.html", "Missions", "Autonomous missions, "
             "schedules, and triggers.",
             aliases=("missions", "autonomous"),
             feature_ids=("missions", "scheduling")),
    PageSpec("/projects.html", "Projects", "Project workspaces and "
             "their history.",
             aliases=("projects",),
             feature_ids=("projects", "project_scaffold")),
    PageSpec("/workspace.html", "Workspace", "The working directory "
             "Nexus operates in.",
             aliases=("workspace", "files", "file browser"),
             feature_ids=("workspaces", "code_editing", "code_intel")),
    PageSpec("/research.html", "Research", "Web research sessions and "
             "their reports.",
             aliases=("research",),
             feature_ids=("web_search",)),
    PageSpec("/knowledge.html", "Knowledge", "What Nexus has learned — "
             "knowledge records, conversation memory, Nexus Brain.",
             aliases=("knowledge", "memory", "brain"),
             sections=(
                 PageSection("brain", "Nexus Brain",
                             feature_ids=("nexus_brain",)),),
             feature_ids=("knowledge", "nexus_brain",
                          "conversation_memory", "causal_memory")),
    PageSpec("/answers.html", "Answer Memory", "Trusted learned answers "
             "that skip model inference.",
             aliases=("answers", "answer memory"),
             feature_ids=("answer_memory",)),
    PageSpec("/tools.html", "Tool Manager", "Installed tools, plugins, "
             "and MCP servers.",
             aliases=("tools", "plugins", "tool manager"),
             sections=(
                 PageSection("mcp", "MCP servers", feature_ids=("mcp",)),),
             feature_ids=("plugins", "mcp")),
    PageSpec("/trainer.html", "Model Growth Lab", "Experience review "
             "and training-data export.",
             aliases=("trainer", "growth lab", "training"),
             feature_ids=("model_growth",)),
    PageSpec("/system.html", "System", "Health, diagnostics, safe mode, "
             "and release controls.",
             aliases=("system", "diagnostics", "health"),
             feature_ids=("health", "safe_mode", "lkg", "self_repair",
                          "environment", "evaluation", "digital_twin")),
    PageSpec("/start.html", "Start Here", "Onboarding and first-run "
             "setup.",
             aliases=("start", "onboarding", "welcome"),
             feature_ids=("profile", "provisioning")),
]


class PageRegistry:
    """Route → PageSpec, plus name/alias → page and section search."""

    def __init__(self, pages: list[PageSpec] | None = None) -> None:
        self._pages = {p.route: p for p in (pages or PAGES)}
        self._by_name: dict[str, PageSpec] = {}
        self._by_section: dict[str, tuple[PageSpec, PageSection]] = {}
        for p in self._pages.values():
            names = {p.title.lower(), *(a.lower() for a in p.aliases)}
            for n in names:
                self._by_name.setdefault(n, p)
            for s in p.sections:
                self._by_section.setdefault(s.id, (p, s))
                self._by_section.setdefault(s.name.lower(), (p, s))

    def get(self, route: str) -> PageSpec | None:
        return self._pages.get(route)

    def all(self) -> list[PageSpec]:
        return list(self._pages.values())

    def find(self, text: str) -> PageSpec | None:
        t = str(text or "").lower()
        best: tuple[int, PageSpec] | None = None
        for name, page in self._by_name.items():
            if name in t:
                if best is None or len(name) > best[0]:
                    best = (len(name), page)
        return best[1] if best else None

    def find_section(self, text: str) -> tuple[PageSpec, PageSection] | None:
        """Match a named section — "speech lab" → (personality, Speech Lab)."""
        t = str(text or "").lower()
        best: tuple[int, PageSpec, PageSection] | None = None
        for key, (page, section) in self._by_section.items():
            if key and key in t:
                if best is None or len(key) > best[0]:
                    best = (len(key), page, section)
        return (best[1], best[2]) if best else None

    def route_for(self, text: str) -> str:
        """Best deep link for a phrase — section beats page."""
        hit = self.find_section(text)
        if hit:
            page, section = hit
            return page.deep_link(section.id)
        page = self.find(text)
        return page.route if page else ""
