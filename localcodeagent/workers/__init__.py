"""Adaptive worker scheduling for Nexus Core.

See docs/WORKERS.md — workers are logical execution units admitted by
measured hardware capacity, never a fixed count.
"""
from .capacity import CapacitySnapshot, Reserves, ResourceMonitor
from .leaks import WorkerLeakTracker, WorkerRun
from .manager import (AdaptiveWorkerManager, QueueEntry, WorkerRecord,
                      QUEUE_REASONS, REASON_TEXT)
from .roles import (PRIORITY, ROLES, ROLE_PROFILES, ResourceEstimate,
                    classify_role, estimate_for)

__all__ = [
    "AdaptiveWorkerManager", "CapacitySnapshot", "QueueEntry",
    "QUEUE_REASONS", "REASON_TEXT", "PRIORITY", "ROLES", "ROLE_PROFILES",
    "Reserves", "ResourceEstimate", "ResourceMonitor", "WorkerRecord",
    "WorkerLeakTracker", "WorkerRun",
    "classify_role", "estimate_for",
]
