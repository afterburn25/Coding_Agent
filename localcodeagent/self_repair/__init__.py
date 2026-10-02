"""Nexus Core self-repair — detect, localize, repair, verify, promote,
roll back, and learn from the system's own failures.

The package is intentionally layered so each stage can be tested and
replaced independently:

    detector      raw failure event -> normalized RepairIncident
    localizer     traceback/git evidence -> ranked file/function suspects
    diagnosis     evidence -> ranked root-cause hypotheses + repair kind
    repair_memory durable procedural memory of past repair outcomes
    patcher       isolated git worktree for candidate code repairs
    verifier      targeted + regression test gates
    canary        bounded candidate-instance comparison
    rollback      last-known-good snapshot / restore
    coordinator   the state machine that ties it together

Design invariants:
- The stable tree is never patched first; code repairs happen in a
  worktree and promotion is gated on measured evidence.
- Operational repairs (restart, quarantine, port reclaim, config
  blacklist) are preferred over code changes when they fit.
- Every transition and artifact is persisted so a restart resumes the
  incident instead of repeating finished work.
"""
from .models import (REPAIR_STATES, SEVERITIES, new_incident,
                     IncidentBudgets)
from .detector import Detector
from .localizer import Localizer
from .diagnosis import Diagnostician
from .repair_memory import RepairMemory
from .coordinator import SelfRepairCoordinator

__all__ = [
    "REPAIR_STATES", "SEVERITIES", "new_incident", "IncidentBudgets",
    "Detector", "Localizer", "Diagnostician", "RepairMemory",
    "SelfRepairCoordinator",
]
