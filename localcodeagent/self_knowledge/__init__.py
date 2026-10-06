"""Nexus self-knowledge + universal chat control plane.

Nexus's answers about Nexus Core come from a canonical Feature Catalog
composed against the live Capability Registry, Tool Registry, Connector
Registry, Settings Registry, and Page Registry — never from static prose.

Layers:
    catalog.py   — FeatureSpec + FeatureCatalog (development status,
                   metadata, links to runtime capability probes)
    pages.py     — PageSpec + PageRegistry (every UI route + deep links)
    settings.py  — SettingSpec + SettingsRegistry (chat-mutable settings)
    actions.py   — ActionSpec + ActionRegistry (typed, risk-classed,
                   verified, undoable chat actions)
    service.py   — SelfKnowledgeService: NL interpretation → resolution
                   → answer payload / action plan / execution result.

Nothing here duplicates a runtime probe — the service composes the
existing registries so chat and GUI share the same truth and the same
mutation path.
"""
from __future__ import annotations

from .catalog import FeatureCatalog, FeatureSpec
from .pages import PageRegistry, PageSpec
from .settings import SettingSpec, SettingsRegistry
from .actions import ActionRegistry, ActionSpec, ActionResult, Risk
from .service import SelfKnowledgeService, Resolution

__all__ = [
    "FeatureCatalog", "FeatureSpec",
    "PageRegistry", "PageSpec",
    "SettingSpec", "SettingsRegistry",
    "ActionRegistry", "ActionSpec", "ActionResult", "Risk",
    "SelfKnowledgeService", "Resolution",
]
