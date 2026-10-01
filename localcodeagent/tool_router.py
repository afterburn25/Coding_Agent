from __future__ import annotations

import json
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Iterable

from .tools.base import ToolRegistry, ToolSpec

HEALTH_TTL_SECONDS = 60.0
ERROR_PREFIXES = ("ERROR:", "PERMISSION_DENIED:", "APPROVAL_REQUIRED:", "TOOL_DISABLED:")


class ToolRouter:
    """Capability-driven tool selection over the ToolRegistry.

    The orchestrator asks "who can do X" instead of naming a concrete tool.
    Candidates are filtered by enabled state, install status, permission mode,
    OS support, and hardware fit, then ranked by preference, permission
    strength, and cached health. Execution walks the ranked list with fallback
    and records routing telemetry analogous to model telemetry.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        resources: Callable[[], dict[str, Any]] | dict[str, Any] | None = None,
        prefer: Iterable[str] | None = None,
        telemetry_path: Path | None = None,
    ) -> None:
        self.registry = registry
        self._resources = resources
        self.prefer = {str(p).lower() for p in (prefer or [])}
        self.telemetry: deque[dict[str, Any]] = deque(maxlen=500)
        self.telemetry_path = Path(telemetry_path) if telemetry_path else None
        self._health_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._lock = threading.RLock()
        if self.telemetry_path and self.telemetry_path.is_file():
            try:
                for line in self.telemetry_path.read_text(encoding="utf-8").splitlines()[-500:]:
                    entry = json.loads(line)
                    if isinstance(entry, dict):
                        self.telemetry.append(entry)
            except (OSError, ValueError):
                pass

    # -- candidate selection --------------------------------------------------

    def _snapshot(self) -> dict[str, Any]:
        if callable(self._resources):
            try:
                return self._resources() or {}
            except Exception:
                return {}
        return dict(self._resources or {})

    def _health(self, spec: ToolSpec) -> dict[str, Any] | None:
        if spec.health_check is None:
            return None
        with self._lock:
            hit = self._health_cache.get(spec.name)
        if hit and (time.time() - hit[0]) < HEALTH_TTL_SECONDS:
            return hit[1]
        result = self.registry.health(spec.name)
        with self._lock:
            self._health_cache[spec.name] = (time.time(), result)
        return result

    def _os_ok(self, spec: ToolSpec) -> bool:
        if not spec.supported_os:
            return True
        platform = "windows" if sys.platform.startswith("win") else "darwin" if sys.platform == "darwin" else "linux"
        return platform in {str(o).lower() for o in spec.supported_os}

    def explain(self, capability: str, *, check_health: bool = False) -> dict[str, Any]:
        """Return ranked candidates plus exclusion reasons — powers routing UI/debug."""
        hw = self._snapshot()
        network_denied = self.registry.permission_manager.effective("network.read") == "deny"
        free_vram_mb = float(hw.get("free_vram_gb") or 0) * 1024
        free_ram_mb = float(hw.get("available_ram_gb") or 0) * 1024
        has_gpu = bool(hw.get("gpus")) or float(hw.get("total_vram_gb") or 0) > 0

        candidates: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = []
        for name in self.registry.find_by_capability(capability):
            spec = self.registry.get(name)
            if spec is None:
                continue
            reason = None
            if not self.registry.is_enabled(name):
                reason = "disabled"
            elif not self.registry.manifest(name).get("callable", True):
                reason = "not_callable"
            elif spec.install_status == "missing":
                reason = "not_installed"
            elif self.registry.permission_manager.effective(spec.permission) == "deny":
                reason = "permission_denied"
            elif spec.requires_network and network_denied:
                reason = "offline"
            elif spec.requires_gpu and not has_gpu:
                reason = "no_gpu"
            elif int(spec.requirements.get("vram_mb") or 0) > free_vram_mb > 0:
                reason = "insufficient_vram"
            elif int(spec.requirements.get("ram_mb") or 0) > free_ram_mb > 0:
                reason = "insufficient_ram"
            elif not self._os_ok(spec):
                reason = "unsupported_os"
            if reason:
                excluded.append({"tool": name, "reason": reason})
                continue
            mode = self.registry.permission_manager.effective(spec.permission)
            score = 0
            if name.lower() in self.prefer or spec.provider.lower() in self.prefer:
                score += 3
            score += {"allow": 2, "session": 1}.get(mode, 0)
            learned = self._learned_score(capability, name)
            score += learned
            health = self._health(spec) if check_health else None
            if health and health.get("ok"):
                score += 1
            candidates.append({
                "tool": name,
                "provider": spec.provider,
                "score": score,
                "permission_mode": mode,
                "needs_approval": mode == "ask",
                "install_status": spec.install_status,
            })
        candidates.sort(key=lambda c: (-c["score"], c["tool"]))
        return {"capability": capability, "candidates": candidates, "excluded": excluded}

    def route(self, capability: str) -> str | None:
        ranked = self.explain(capability)["candidates"]
        return ranked[0]["tool"] if ranked else None

    def _learned_score(self, capability: str, tool: str) -> float:
        """Success-rate bonus from routing telemetry (0–4 points).

        A tool that has repeatedly succeeded for this capability outranks an
        untested or failing one; fewer than 3 recorded calls earns a neutral 0.
        """
        calls = wins = 0
        with self._lock:
            entries = list(self.telemetry)
        for e in entries:
            if e.get("capability") != capability or e.get("chosen") != tool:
                continue
            calls += 1
            if e.get("ok"):
                wins += 1
        if calls < 3:
            return 0.0
        return round(4.0 * wins / calls, 2)

    # -- execution ------------------------------------------------------------

    def execute(self, capability: str, arguments: dict[str, Any], *, approved: bool = False) -> dict[str, Any]:
        started = time.time()
        info = self.explain(capability)
        attempts: list[dict[str, Any]] = []
        approval_pending: dict[str, Any] | None = None
        for cand in info["candidates"]:
            name = cand["tool"]
            if cand["needs_approval"] and not approved:
                attempts.append({"tool": name, "outcome": "approval_required"})
                approval_pending = approval_pending or cand
                continue
            result = self.registry.execute(name, arguments, approved=approved)
            if result.startswith(ERROR_PREFIXES):
                attempts.append({"tool": name, "outcome": result.split(":", 1)[0].lower(), "detail": result[:300]})
                continue
            elapsed = round((time.time() - started) * 1000, 1)
            self._record(capability, name, attempts, ok=True, elapsed_ms=elapsed)
            return {"ok": True, "tool": name, "result": result, "attempts": attempts, "elapsed_ms": elapsed}
        if approval_pending is not None and not any(a["outcome"] != "approval_required" for a in attempts):
            elapsed = round((time.time() - started) * 1000, 1)
            self._record(capability, approval_pending["tool"], attempts, ok=False, elapsed_ms=elapsed)
            return {"ok": False, "error": "approval_required", "tool": approval_pending["tool"],
                    "attempts": attempts, "elapsed_ms": elapsed}
        elapsed = round((time.time() - started) * 1000, 1)
        self._record(capability, None, attempts, ok=False, elapsed_ms=elapsed)
        return {"ok": False, "error": "no_capable_tool", "capability": capability,
                "excluded": info["excluded"], "attempts": attempts, "elapsed_ms": elapsed}

    def _record(self, capability: str, chosen: str | None, attempts: list[dict[str, Any]], *, ok: bool, elapsed_ms: float) -> None:
        entry = {
            "ts": time.time(),
            "capability": capability,
            "chosen": chosen,
            "attempts": attempts,
            "ok": ok,
            "elapsed_ms": elapsed_ms,
        }
        with self._lock:
            self.telemetry.append(entry)
        if self.telemetry_path:
            try:
                self.telemetry_path.parent.mkdir(parents=True, exist_ok=True)
                with self.telemetry_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
                # The file only ever feeds the last-500 tail on load — keep it
                # bounded so months of runs do not grow it without limit.
                if self.telemetry_path.stat().st_size > 512 * 1024:
                    lines = self.telemetry_path.read_text(encoding="utf-8").splitlines()[-2000:]
                    tmp = self.telemetry_path.with_suffix(".tmp")
                    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
                    tmp.replace(self.telemetry_path)
            except OSError:
                pass

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            return list(self.telemetry)[-limit:]

    def stats(self) -> dict[str, Any]:
        """Aggregate per (capability → tool) outcomes for routing insight."""
        table: dict[str, dict[str, Any]] = {}
        with self._lock:
            entries = list(self.telemetry)
        for e in entries:
            key = f"{e.get('capability')}|{e.get('chosen') or '-'}"
            row = table.setdefault(key, {"capability": e.get("capability"), "tool": e.get("chosen"),
                                         "calls": 0, "ok": 0, "failed": 0, "total_ms": 0.0})
            row["calls"] += 1
            if e.get("ok"):
                row["ok"] += 1
            else:
                row["failed"] += 1
            row["total_ms"] += float(e.get("elapsed_ms") or 0)
        rows = []
        for row in table.values():
            row["success_rate"] = round(row["ok"] / row["calls"], 3) if row["calls"] else 0.0
            row["avg_ms"] = round(row["total_ms"] / row["calls"], 1) if row["calls"] else 0.0
            row.pop("total_ms", None)
            rows.append(row)
        rows.sort(key=lambda r: (r["capability"] or "", -(r["calls"])))
        return {"total_events": len(entries), "routes": rows}
