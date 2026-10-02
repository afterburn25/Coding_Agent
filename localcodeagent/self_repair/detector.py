"""Failure intake: normalize raw failure events into repair incidents.

Responsibilities:
- Normalize a volatile error message into a stable *signature* so the
  same fault recurring is recognized as one incident, not many.
- Score severity from class/subsystem/recurrence and decide whether the
  failure is worth an incident at all — warnings and one-shot noise do
  not generate repair work.
- Dedupe against open incidents: a repeat bumps `occurrences` and
  refreshes `last_seen` instead of forking a parallel repair.
"""
from __future__ import annotations

import re
import time
from typing import Any, Callable

from .models import new_incident, OPEN_REPAIR_STATES

# Volatile fragments removed before signing — otherwise every PIDs/ports/
# addresses/timestamps make each recurrence look like a brand-new fault.
_VOLATILE = [
    (re.compile(r"0x[0-9a-fA-F]+"), "0x?"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}:\d+\b"), "host:port"),
    (re.compile(r"\bport \d+\b", re.I), "port ?"),
    (re.compile(r"\b:\d{2,5}\b"), ":?"),
    (re.compile(r"\bpid[ =]?\d+\b", re.I), "pid ?"),
    (re.compile(r"\b\d+(?:\.\d+)? ?(?:ms|s|mb|gb|kb|bytes)\b", re.I), "N"),
    (re.compile(r"[A-Za-z]:\\[^\s'\"]+"), "<path>"),
    (re.compile(r"/[^\s'\"]+"), "<path>"),
    (re.compile(r"\b\d+\b"), "N"),
]

# error_class → (base severity score, default subsystem hint)
_CLASS_SEVERITY = {
    "SystemExit": 3, "OOM": 3, "MemoryError": 3, "ProcessCrash": 3,
    "BuildFailure": 3, "InstallerFailure": 3, "StartupFailure": 3,
    "DatabaseError": 2, "CorruptionError": 3, "MigrationError": 2,
    "PortCollision": 2, "TransportError": 2, "TimeoutError": 2,
    "Deadlock": 2, "ModelLoadError": 2, "RuntimeError": 2,
    "TestFailure": 2, "AssertionError": 1, "MissionNodeFailure": 2,
    "ToolError": 1, "ConfigError": 1, "DependencyError": 2,
    "PermissionError": 1, "NetworkError": 1, "ValidationError": 1,
    # A raised exception with a repo traceback is evidence, not noise —
    # score 2 keeps first-time occurrences above the suppression floor.
    "Exception": 2, "Error": 1,
}
_CRITICAL_SUBSYSTEMS = {"backend", "brain", "storage", "installer",
                        "watchdog", "self_update"}

# classes that are nearly always environmental noise unless recurring
_NOISY = {"NetworkError", "TimeoutError", "TransportError",
          "PermissionError"}


def normalize_signature(error_class: str, message: str,
                        subsystem: str) -> str:
    """Stable identity for 'the same fault'."""
    text = f"{error_class}: {message}".strip()
    for rx, repl in _VOLATILE:
        text = rx.sub(repl, text)
    text = re.sub(r"\s+", " ", text).strip().lower()
    return f"{subsystem}:{error_class}:{text[:140]}"


def classify_error(message: str, exc_type: str = "") -> tuple[str, str]:
    """Map raw failure text to (error_class, subsystem_hint)."""
    m = message or ""
    ml = m.lower()
    t = exc_type or ""
    if "10054" in m or "connection reset" in ml or "forcibly closed" in ml:
        return "TransportError", "network"
    if "10048" in m or "address already in use" in ml or "bind" in ml and "fail" in ml:
        return "PortCollision", "network"
    if "out of memory" in ml or "oom" in ml or t == "MemoryError" \
            or "vram" in ml and "alloc" in ml:
        return "OOM", "runtime"
    if "llama" in ml and ("crash" in ml or "exit" in ml or "died" in ml):
        return "ProcessCrash", "llama.cpp"
    if "comfy" in ml and ("crash" in ml or "exit" in ml or "died" in ml):
        return "ProcessCrash", "comfyui"
    if "mcp" in ml and ("crash" in ml or "exit" in ml or "died" in ml
                        or "disconnect" in ml):
        return "ProcessCrash", "mcp"
    if "inno" in ml or "installer" in ml and "fail" in ml:
        return "InstallerFailure", "installer"
    if "iscc" in ml or "packaging" in ml and "fail" in ml:
        return "BuildFailure", "packaging"
    if "json" in ml and ("corrupt" in ml or "decode" in ml or "expecting" in ml):
        return "CorruptionError", "storage"
    if "sqlite" in ml or "database is locked" in ml or "disk i/o" in ml:
        return "DatabaseError", "storage"
    if "startup" in ml and "fail" in ml or "health check" in ml and "fail" in ml:
        return "StartupFailure", "backend"
    if "modelload" in ml.replace(" ", "") or ("model" in ml and "load" in ml and "fail" in ml):
        return "ModelLoadError", "runtime"
    if t:
        # python exception type, e.g. TypeError / ValueError / AttributeError
        return (t if t in _CLASS_SEVERITY else "Exception"), "code"
    return "Error", "unknown"


class Detector:
    """Turns raw failure signals into deduplicated, severity-scored
    incidents. `incidents` is a callable returning the live incident
    rows (the coordinator's store) so dedupe sees existing state."""

    def __init__(self, existing: Callable[[], list[dict]], *,
                 min_score: int = 3, dedupe_window_s: float = 1800.0):
        self._existing = existing
        self.min_score = min_score
        self.dedupe_window_s = dedupe_window_s

    def severity(self, error_class: str, subsystem: str,
                 occurrences: int = 1) -> tuple[str, int]:
        score = _CLASS_SEVERITY.get(error_class, 1)
        if subsystem in _CRITICAL_SUBSYSTEMS:
            score += 1
        if occurrences >= 5:
            score += 2
        elif occurrences >= 2:
            score += 1
        name = "low"
        if score >= 5:
            name = "critical"
        elif score == 4:
            name = "high"
        elif score == 3:
            name = "normal"
        return name, score

    def ingest(self, *, source: str, error_message: str,
               exc_type: str = "", subsystem: str = "",
               stack_trace: str = "", **kw) -> tuple[dict | None, str]:
        """Returns (incident_or_None, disposition):
        'new' | 'recurred' | 'suppressed'.
        """
        error_class, hint = classify_error(error_message, exc_type)
        subsystem = subsystem or hint
        signature = normalize_signature(error_class, error_message,
                                        subsystem)
        now = time.time()

        for row in self._existing():
            if not isinstance(row, dict) \
                    or row.get("signature") != signature:
                continue
            if str(row.get("state")) in OPEN_REPAIR_STATES \
                    or now - float(row.get("last_seen") or 0) \
                    < self.dedupe_window_s:
                row["occurrences"] = int(row.get("occurrences") or 0) + 1
                row["last_seen"] = now
                row["updated_at"] = now
                sev, _ = self.severity(error_class, subsystem,
                                       row["occurrences"])
                row["severity"] = sev
                hist = row.setdefault("history", [])
                hist.append({"ts": now, "event": "recurred",
                             "detail": f"occurrence {row['occurrences']}"})
                del hist[:-80]
                return row, "recurred"

        occurrences = 1
        sev, score = self.severity(error_class, subsystem, occurrences)
        # Noise suppression: a first-time low-signal class never opens an
        # incident by itself — it must recur or come in on a critical path.
        if score < self.min_score and error_class in _NOISY:
            return None, "suppressed"
        if score < self.min_score - 1:
            return None, "suppressed"

        return new_incident(
            source=source, subsystem=subsystem, error_class=error_class,
            error_message=error_message, signature=signature,
            severity=sev, stack_trace=stack_trace, **kw), "new"
