"""Diagnostician — turn incident evidence into ranked root-cause
hypotheses and pick the repair kind (operational vs code).

Categories follow the spec's taxonomy. Each rule matches on error class,
message fragments, subsystem, and localized suspects; it emits a
hypothesis with evidence and a suggested repair kind. Rules are
deterministic — no LLM needed to decide *what kind* of fix to try —
which keeps diagnosis fast, testable, and honest about confidence.
"""
from __future__ import annotations

from typing import Any

CATEGORIES = {
    "source_bug", "config_bug", "runtime_bug", "dependency_incompatible",
    "environment", "hardware_pressure", "bad_model_config", "corrupt_model",
    "network_failure", "external_service", "permission", "stale_process",
    "race_condition", "recent_regression", "invalid_user_data",
    "corrupt_store", "port_collision", "unknown",
}

# kind → repair_kind: operational fixes run directly against the live
# system; code repairs require the worktree pipeline.
_OPERATIONAL = {"stale_process", "network_failure", "corrupt_store",
                "bad_model_config", "hardware_pressure", "environment",
                "port_collision", "external_service"}


def _has(msg: str, *needles: str) -> bool:
    ml = (msg or "").lower()
    return any(n in ml for n in needles)


def _hyp(kind: str, detail: str, confidence: float,
         evidence: list[str]) -> dict[str, Any]:
    if kind in _OPERATIONAL:
        repair_kind = "operational"
    elif kind in {"source_bug", "recent_regression", "config_bug",
                  "runtime_bug", "dependency_incompatible",
                  "race_condition", "invalid_user_data"}:
        repair_kind = "code"
    else:
        repair_kind = "code" if kind != "unknown" else ""
    return {"kind": kind, "detail": detail, "confidence": round(
        min(confidence, 0.99), 2), "evidence": evidence[:8],
        "repair_kind": repair_kind}


class Diagnostician:
    """Evidence → ranked hypotheses. Pure and deterministic."""

    def diagnose(self, incident: dict) -> dict[str, Any]:
        cls = str(incident.get("error_class") or "")
        msg = str(incident.get("error_message") or "")
        sub = str(incident.get("subsystem") or "")
        suspects = incident.get("suspects") or []
        ev: list[str] = []
        hyps: list[dict[str, Any]] = []

        add = hyps.append

        if cls == "PortCollision" or _has(msg, "10048", "address already in use"):
            add(_hyp("stale_process",
                     "Port held by a stale process — reclaim or rebind",
                     0.9, ["bind/address failure", "subsystem=network"]))
        if cls == "TransportError":
            conf = 0.75 if sub == "llama.cpp" else 0.6
            add(_hyp("stale_process" if sub == "llama.cpp" else "network_failure",
                     "Transport reset — peer process likely died or restarted",
                     conf, ["WinError 10054 / connection reset",
                            f"subsystem={sub}"]))
        if cls == "OOM" or _has(msg, "out of memory", "vram"):
            add(_hyp("hardware_pressure",
                     "Memory/VRAM exhaustion — reduce footprint or "
                     "downgrade model/context", 0.85,
                     ["OOM signature in message"]))
        if cls == "CorruptionError" or _has(msg, "corrupt", "json decode",
                                            "expecting value"):
            add(_hyp("corrupt_store",
                     "Corrupt or truncated state file — quarantine and "
                     "rebuild from source", 0.88,
                     ["JSON/integrity failure"]))
        if cls == "DatabaseError" or _has(msg, "sqlite", "database is locked"):
            add(_hyp("corrupt_store",
                     "SQLite integrity/lock failure — integrity check, "
                     "backup, rebuild", 0.8, ["database error signature"]))
        if cls in {"ProcessCrash"}:
            add(_hyp("runtime_bug" if sub in {"llama.cpp", "comfyui", "mcp"}
                     else "unknown",
                     f"{sub} process exited unexpectedly — inspect launch "
                     "args/config then restart", 0.7,
                     [f"unexpected exit in {sub}",
                      f"exit_code={incident.get('exit_code')}"]))
        if cls == "ModelLoadError":
            add(_hyp("bad_model_config",
                     "Model failed to load — invalid file/hash or "
                     "incompatible runtime args", 0.78,
                     ["model load failure"]))
            add(_hyp("corrupt_model",
                     "Model file may be corrupt — verify checksum, "
                     "redownload if mismatched", 0.5,
                     ["load failure could be data corruption"]))
        if cls in {"BuildFailure", "InstallerFailure", "StartupFailure",
                   "TestFailure"}:
            add(_hyp("source_bug" if suspects else "environment",
                     f"{sub or 'build'} failure — "
                     + ("traceback points into repo code" if suspects else
                        "no repo frame localized; likely environment/tooling"),
                     0.65 if suspects else 0.4,
                     [f"{len(suspects)} repo suspect(s) localized"]))
        if cls in {"TypeError", "AttributeError", "KeyError", "IndexError",
                   "ValueError", "NameError", "Exception", "Error"} \
                and suspects:
            top = suspects[0]
            add(_hyp("source_bug",
                     f"Unhandled {cls} in {top['path']}"
                     + (f"::{top['function']}()" if top.get("function") else ""),
                     min(0.9, 0.5 + top["confidence"] * 0.45),
                     [f"traceback frame {top['path']}:{top.get('line')}"]))
        # Regression correlation — recent commits touching the top suspect
        if suspects and suspects[0].get("recent_commits", 0) >= 2:
            add(_hyp("recent_regression",
                     "Top suspect file changed in recent commits — "
                     "likely a fresh regression", 0.6,
                     [f"{suspects[0]['recent_commits']} recent commits "
                      f"touch {suspects[0]['path']}"]))
        if _has(msg, "permission denied", "access is denied", "winerror 5"):
            add(_hyp("permission",
                     "OS denied access — check file locks/ACLs", 0.8,
                     ["permission-denied signature"]))
        if not hyps:
            add(_hyp("unknown",
                     "No deterministic rule matched — needs LLM-assisted "
                     "diagnosis or human triage", 0.2,
                     ["no signature match"]))

        hyps.sort(key=lambda h: -h["confidence"])
        best = hyps[0]
        return {"hypotheses": hyps[:6], "repair_kind": best["repair_kind"],
                "confidence": best["confidence"], "category": best["kind"]}
