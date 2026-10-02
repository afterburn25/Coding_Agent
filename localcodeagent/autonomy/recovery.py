"""Recovery Manager: structured failure classification, bounded playbooks,
and repeated-failure (loop) detection.

Never infinite retries — every class maps to a finite step list and the
supervisor enforces per-mission retry budgets before blocking.
"""
from __future__ import annotations

import hashlib
import re
import time
from enum import Enum
from typing import Any


class FailureClass(str, Enum):
    MODEL_CRASH = "MODEL_CRASH"
    BACKEND_CRASH = "BACKEND_CRASH"
    TOOL_TIMEOUT = "TOOL_TIMEOUT"
    NETWORK_FAILURE = "NETWORK_FAILURE"
    CI_FAILURE = "CI_FAILURE"
    TEST_FAILURE = "TEST_FAILURE"
    BUILD_FAILURE = "BUILD_FAILURE"
    CUDA_OOM = "CUDA_OOM"
    DISK_LOW = "DISK_LOW"
    DATABASE_LOCKED = "DATABASE_LOCKED"
    DATABASE_CORRUPT = "DATABASE_CORRUPT"
    MODEL_CORRUPT = "MODEL_CORRUPT"
    DEPENDENCY_FAILURE = "DEPENDENCY_FAILURE"
    CHECKSUM_FAILURE = "CHECKSUM_FAILURE"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    PERMISSION_REQUIRED = "PERMISSION_REQUIRED"
    CONFLICT = "CONFLICT"
    STALE_WORKSPACE = "STALE_WORKSPACE"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


_PATTERNS: list[tuple[FailureClass, tuple[str, ...]]] = [
    (FailureClass.CUDA_OOM, ("out of memory", "cuda oom", "cublas", "vram", "insufficient memory")),
    (FailureClass.DISK_LOW, ("no space left", "disk full", "disk_low", "enosp")),
    (FailureClass.DATABASE_LOCKED, ("database is locked", "sqlite3.operationalerror: database is locked")),
    (FailureClass.DATABASE_CORRUPT, ("database disk image is malformed", "database corruption", "file is not a database")),
    (FailureClass.MODEL_CORRUPT, ("checksum", "hash mismatch", "corrupt model", "invalid gguf", "magic mismatch")),
    (FailureClass.CHECKSUM_FAILURE, ("checksum mismatch", "sha256 mismatch", "digest mismatch")),
    (FailureClass.AUTH_REQUIRED, ("401", "unauthorized", "authentication required", "bad credentials", "403")),
    (FailureClass.PERMISSION_REQUIRED, ("permission_denied", "approval_required", "requires approval", "access denied")),
    (FailureClass.MODEL_CRASH, ("llama-server exited", "model server crashed", "inference crashed", "server terminated")),
    (FailureClass.BACKEND_CRASH, ("backend crashed", "comfyui exited", "process exited", "connection refused")),
    (FailureClass.NETWORK_FAILURE, ("winerror 10054", "connectionreseterror", "connection reset", "incompleteread",
                                    "urlerror", "timed out", "timeout", "temporary failure in name resolution",
                                    "connection refused", "network is unreachable", "502", "503", "504")),
    (FailureClass.TOOL_TIMEOUT, ("tool timed out", "deadline exceeded", "tool_timeout")),
    (FailureClass.CI_FAILURE, ("ci failed", "workflow run failed", "check_run failure")),
    (FailureClass.TEST_FAILURE, ("test failed", "assertionerror", "failed (failures", "test failure", "unittest")),
    (FailureClass.BUILD_FAILURE, ("build failed", "compilation failed", "linker error", "error cs", "syntaxerror")),
    (FailureClass.DEPENDENCY_FAILURE, ("modulenotfounderror", "importerror", "package not found", "pip install failed",
                                       "dependency", "no matching distribution")),
    (FailureClass.CONFLICT, ("merge conflict", "git conflict", "409 conflict")),
    (FailureClass.STALE_WORKSPACE, ("stale workspace", "index.lock", "checkout failed")),
    (FailureClass.CANCELLED, ("cancelled", "canceled", "user stopped")),
]


def classify_failure(text: str) -> FailureClass:
    low = str(text or "").lower()
    for cls, needles in _PATTERNS:
        if any(n in low for n in needles):
            return cls
    return FailureClass.UNKNOWN


def failure_signature(task_title: str, error: str) -> str:
    """Stable signature for loop detection — normalize volatile tokens so the
    same logical failure hashes identically across retries."""
    norm = re.sub(r"[\dabcdef]{6,}", "#", str(error or "").lower())
    norm = re.sub(r"\s+", " ", norm)[:400]
    return hashlib.sha1(f"{task_title}|{norm}".encode()).hexdigest()[:16]


# Bounded playbooks: ordered recovery steps the supervisor attempts.
# Each step is (action, arg) — supervisor interprets; 'escalate' ends.
PLAYBOOKS: dict[FailureClass, list[dict[str, Any]]] = {
    FailureClass.CUDA_OOM: [
        {"action": "evict_idle_models"},
        {"action": "retry", "delay": 20},
        {"action": "reduce_context"},
        {"action": "retry", "delay": 30},
        {"action": "fallback_model"},
        {"action": "retry", "delay": 30},
        {"action": "escalate"},
    ],
    FailureClass.BACKEND_CRASH: [
        {"action": "inspect_logs"},
        {"action": "restart_service"},
        {"action": "health_probe"},
        {"action": "retry", "delay": 15},
        {"action": "escalate"},
    ],
    FailureClass.MODEL_CRASH: [
        {"action": "restart_model"},
        {"action": "retry", "delay": 30},
        {"action": "fallback_model"},
        {"action": "retry", "delay": 30},
        {"action": "escalate"},
    ],
    FailureClass.NETWORK_FAILURE: [
        {"action": "retry", "delay": 10},
        {"action": "retry", "delay": 30},   # bounded exponential-ish backoff
        {"action": "retry", "delay": 120},
        {"action": "wait_connectivity"},
        {"action": "retry", "delay": 60},
        {"action": "escalate"},
    ],
    FailureClass.TEST_FAILURE: [
        {"action": "inspect_failure"},
        {"action": "subtask_fix", "title": "Diagnose and fix failing tests"},
        {"action": "escalate"},
    ],
    FailureClass.BUILD_FAILURE: [
        {"action": "inspect_failure"},
        {"action": "subtask_fix", "title": "Diagnose and fix build failure"},
        {"action": "escalate"},
    ],
    FailureClass.CI_FAILURE: [
        {"action": "fetch_logs"},
        {"action": "inspect_failure"},
        {"action": "subtask_fix", "title": "Reproduce and fix CI failure locally"},
        {"action": "escalate"},
    ],
    FailureClass.DEPENDENCY_FAILURE: [
        {"action": "inspect_failure"},
        {"action": "research"},
        {"action": "subtask_fix", "title": "Fix broken dependency/install"},
        {"action": "escalate"},
    ],
    FailureClass.PERMISSION_REQUIRED: [
        {"action": "request_approval"},
        {"action": "escalate"},
    ],
    FailureClass.AUTH_REQUIRED: [
        {"action": "escalate"},  # never guess credentials
    ],
    FailureClass.DISK_LOW: [
        {"action": "notify"},
        {"action": "pause"},
    ],
    FailureClass.DATABASE_LOCKED: [
        {"action": "retry", "delay": 5},
        {"action": "retry", "delay": 20},
        {"action": "escalate"},
    ],
    FailureClass.DATABASE_CORRUPT: [
        {"action": "restore_backup"},
        {"action": "escalate"},
    ],
    FailureClass.TOOL_TIMEOUT: [
        {"action": "retry", "delay": 10},
        {"action": "retry", "delay": 60},
        {"action": "escalate"},
    ],
    FailureClass.CONFLICT: [
        {"action": "escalate"},
    ],
    FailureClass.STALE_WORKSPACE: [
        {"action": "refresh_workspace"},
        {"action": "retry", "delay": 5},
        {"action": "escalate"},
    ],
    FailureClass.CANCELLED: [
        {"action": "stop"},
    ],
    FailureClass.CHECKSUM_FAILURE: [
        {"action": "redownload"},
        {"action": "escalate"},
    ],
    FailureClass.MODEL_CORRUPT: [
        {"action": "repair_model"},
        {"action": "escalate"},
    ],
    FailureClass.UNKNOWN: [
        {"action": "retry", "delay": 30},
        {"action": "inspect_failure"},
        {"action": "escalate"},
    ],
}


class RecoveryManager:
    """Drives failure → playbook-step progression inside a mission.

    The playbook cursor lives on the failure record in the mission so a
    restart continues mid-playbook instead of restarting recovery blindly.
    """

    def record_failure(self, mission: dict, task_title: str, error: str) -> dict:
        cls = classify_failure(error)
        sig = failure_signature(task_title, error)
        rec = {
            "ts": time.time(),
            "class": cls.value,
            "signature": sig,
            "task": task_title[:200],
            "error": str(error)[:800],
            "playbook_step": 0,
        }
        mission.setdefault("failure_history", []).append(rec)
        mission["failure_history"] = mission["failure_history"][-100:]
        return rec

    def same_failure_count(self, mission: dict, signature: str) -> int:
        return sum(1 for f in mission.get("failure_history", [])
                   if f.get("signature") == signature)

    def next_step(self, mission: dict, failure: dict) -> dict | None:
        """Advance the failed record's playbook cursor; None = exhausted."""
        cls = FailureClass(failure.get("class") or FailureClass.UNKNOWN.value)
        steps = PLAYBOOKS.get(cls, PLAYBOOKS[FailureClass.UNKNOWN])
        idx = int(failure.get("playbook_step") or 0)
        if idx >= len(steps):
            return None
        failure["playbook_step"] = idx + 1
        return steps[idx]

    def budgets_exceeded(self, mission: dict) -> str:
        """Returns a reason string if any retry budget is blown, else ''."""
        budgets = mission.get("budgets") or {}
        failures = mission.get("failure_history") or []
        max_repairs = int(budgets.get("max_repair_loops", 5))
        if int(mission.get("repair_loops", 0)) >= max_repairs:
            return f"repair-loop budget exhausted ({max_repairs})"
        max_same = int(budgets.get("max_same_failure_retries", 3))
        if failures:
            latest = failures[-1].get("signature")
            if latest and self.same_failure_count(mission, latest) >= max_same:
                return f"same failure repeated {max_same}x without progress"
        max_tools = int(budgets.get("max_tool_failures", 8))
        tool_failures = sum(1 for f in failures
                            if f.get("class") in {FailureClass.TOOL_TIMEOUT.value})
        if tool_failures >= max_tools:
            return f"tool-failure budget exhausted ({max_tools})"
        return ""
