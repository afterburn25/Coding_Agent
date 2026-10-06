"""Continual learning subsystem.

Memory taxonomy + promotion policy + lesson extraction + consolidation +
competency maps + active-learning curriculum + mastery + procedural and
strategy memory — coordinated by LearningGovernor over the existing
stores (KnowledgeMemory, EvidenceBoard, DecisionJournal, ModelGrowthLab,
regressions, benchmarks).
"""

from . import taxonomy
from .competencies import CompetencyMap
from .consolidation import ConsolidationBudget, ConsolidationEngine
from .curriculum import (ActiveLearningPlanner, CurriculumManager,
                         StudySessionStore)
from .freshness import FreshnessPolicy
from .governor import LearningGovernor
from .lessons import LessonExtractor, LessonStore, classify_problem
from .mastery import MasteryEvaluator
from .procedures import ProceduralMemory
from .promotion import KnowledgePromotionPolicy, PromotionDecision
from .strategies import StrategyEvaluator

__all__ = [
    "taxonomy", "CompetencyMap", "ConsolidationBudget",
    "ConsolidationEngine", "ActiveLearningPlanner", "CurriculumManager",
    "StudySessionStore", "FreshnessPolicy", "LearningGovernor",
    "LessonExtractor", "LessonStore", "classify_problem",
    "MasteryEvaluator", "ProceduralMemory", "KnowledgePromotionPolicy",
    "PromotionDecision", "StrategyEvaluator",
]
