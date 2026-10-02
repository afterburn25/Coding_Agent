"""Nexus Autonomy — persistent mission, trigger, schedule, and supervisor
subsystems.

Nexus may autonomously determine HOW to accomplish goals the user has
authorized. It must never invent unrelated top-level goals for the user:
autonomy is execution autonomy, not independent agenda creation.
"""

from .missions import MissionStore, MISSION_STATUSES, TERMINAL_MISSION_STATUSES
from .task_graph import TaskGraph, ResourceLocks
from .supervisor import AutonomousSupervisor
from .policy import AutonomyPolicy
from .notifications import NotificationCenter
from .triggers import TriggerEngine
from .scheduler import Scheduler
from .recovery import RecoveryManager, FailureClass, classify_failure
from .evaluator import MissionEvaluator, EvalVerdict
from .planner import MissionPlanner
from .budgets import BudgetManager

__all__ = [
    "MissionStore", "MISSION_STATUSES", "TERMINAL_MISSION_STATUSES",
    "TaskGraph", "ResourceLocks",
    "AutonomousSupervisor", "AutonomyPolicy", "NotificationCenter",
    "TriggerEngine", "Scheduler",
    "RecoveryManager", "FailureClass", "classify_failure",
    "MissionEvaluator", "EvalVerdict", "MissionPlanner", "BudgetManager",
]
