"""Long-running mission soak — exercises the full autonomy loop for real.

Boots a source backend against a real config, then repeatedly:

  1. submits a bounded one-shot mission (inspect -> work -> verify graph),
  2. auto-resolves any approval gates (approvals are what make unattended
     runs stall — the soak grants them so every resume path runs),
  3. waits for a terminal mission state,
  4. every ``--kill-every`` cycles hard-kills the backend mid-mission,
     restarts it, and verifies the mission persists and still completes.

Every event is appended to a JSONL log so partial runs are still
verifiable evidence; a final JSON summary is written on exit.

Usage:
    python scripts/mission_soak.py --config D:\\Nexus_Core\\config.json \
        --cycles 12 --kill-every 3 --report data/soak/mission_soak.json
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

TERMINAL = {"completed", "failed", "blocked", "cancelled", "archived"}
MODEL_ID = os.environ.get("NEXUS_SOAK_MODEL", "qwen3-4b-instruct")


def _api(port: int, method: str, path: str,
         body: dict | None = None, timeout: int = 15) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except Exception:
            return exc.code, {}
    except Exception:
        return 0, {}


class Soak:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.port: int = args.port
        self.proc: subprocess.Popen | None = None
        self.log_path = Path(args.report).with_suffix(".jsonl")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.stats = {
            "cycles": 0, "completed": 0, "failed": 0, "blocked": 0,
            "timeouts": 0, "approvals": 0, "kills": 0, "recoveries": 0,
            "unexpected_exits": 0, "started_at": time.time(),
        }

    # -- process lifecycle -------------------------------------------------

    def _launch(self) -> None:
        log = open(Path(self.args.report).with_suffix(".backend.log"),
                   "ab", buffering=0)
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "localcodeagent", "--server",
             "--workspace", str(self.args.workspace),
             "--config", str(self.args.config),
             "--port", str(self.port)],
            cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT)
        self._event("backend_launch", pid=self.proc.pid, port=self.port)

    def _kill(self) -> None:
        if self.proc and self.proc.poll() is None:
            subprocess.run(["taskkill", "/PID", str(self.proc.pid),
                            "/T", "/F"], capture_output=True)
            self.stats["kills"] += 1
            self._event("backend_killed", pid=self.proc.pid)
        self.proc = None

    def _wait_alive(self, timeout: float = 120) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc is None or self.proc.poll() is not None:
                return False
            code, _ = _api(self.port, "GET", "/api/nexus/state", timeout=4)
            if code:
                return True
            time.sleep(2)
        return False

    def _restart(self) -> bool:
        self._launch()
        ok = self._wait_alive()
        if ok:
            self.stats["recoveries"] += 1
            self._ensure_autonomy_on()
        self._event("backend_restart", ok=ok)
        return ok

    def _ensure_autonomy_on(self) -> None:
        """The soak workspace persists across runs — a control flag left
        by a previous session (stop, safe mode) would silently park every
        mission 'ready' while the soak reports timeouts. Restore the
        operating posture the soak assumes."""
        code, sm = _api(self.port, "GET", "/api/safemode")
        if code and sm.get("active"):
            _api(self.port, "POST", "/api/safemode/exit", {})
            self._event("safemode_exited")
        code, st = _api(self.port, "GET", "/api/autonomy/status")
        if code and (st.get("stopped") or st.get("paused")):
            _api(self.port, "POST", "/api/autonomy/start", {})
            self._event("autonomy_started")
        # A restart drops every managed runtime — re-start the utility
        # model or missions sit 'executing' with no model behind them.
        _api(self.port, "POST", "/api/runtime/start",
             {"model_id": MODEL_ID, "approve": True}, timeout=30)

    # -- soak loop ----------------------------------------------------------

    def _event(self, kind: str, **fields) -> None:
        rec = {"ts": time.time(), "kind": kind, **fields}
        with open(self.log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
        print(f"[soak] {kind} " + " ".join(
            f"{k}={v}" for k, v in fields.items()), flush=True)

    def _approve_all(self) -> None:
        code, body = _api(self.port, "GET", "/api/autonomy/approvals")
        for ap in (body.get("approvals") or []):
            aid = ap.get("id")
            if not aid or str(ap.get("state") or "pending") != "pending":
                continue
            _api(self.port, "POST",
                 f"/api/autonomy/approvals/{aid}/approve", {})
            self.stats["approvals"] += 1
            self._event("approval_granted", id=aid,
                        action=ap.get("action") or ap.get("name"))

    def _mission_terminal(self, mid: str) -> dict | None:
        code, body = _api(self.port, "GET", f"/api/missions")
        for m in body.get("missions") or []:
            if m.get("id") == mid:
                return m if m.get("status") in TERMINAL else m
        return None

    def _run_cycle(self, idx: int) -> str:
        objective = (f"[SOAK cycle {idx}] Create a file named "
                     f"soak_{idx}.txt in this workspace containing one "
                     f"line: 'cycle {idx} ok'. Then finish.")
        code, body = _api(self.port, "POST", "/api/missions", {
            "objective": objective,
            "title": f"soak-{idx}",
            "scope": "one_shot",
            "autonomy_profile": "local_autonomous",
            "success_criteria": [
                {"kind": "artifact_exists",
                 "target": f"soak_{idx}.txt"},
                {"kind": "verify_passed",
                 "description": "workspace tests pass"}],
        })
        mission = body.get("mission") or {}
        mid = mission.get("id")
        if not mid:
            self._event("submit_failed", code=code, body=body)
            return "submit_failed"
        self._event("mission_submitted", id=mid, cycle=idx)

        kill_this_cycle = (idx % max(1, self.args.kill_every) == 0
                           and idx > 0)
        killed = False
        deadline = time.time() + self.args.mission_timeout
        status = ""
        while time.time() < deadline:
            if self.proc is None or self.proc.poll() is not None:
                self.stats["unexpected_exits"] += 1
                self._event("backend_died", cycle=idx,
                            code=self.proc.returncode if self.proc else -1)
                if not self._restart():
                    return "backend_dead"
            self._approve_all()
            m = self._mission_terminal(mid)
            status = (m or {}).get("status", status)
            if status == "executing" and kill_this_cycle and not killed:
                killed = True
                self._kill()
                if not self._restart():
                    return "restart_failed"
                continue
            if status in TERMINAL:
                self._event("mission_terminal", id=mid, status=status)
                return status
            time.sleep(10)
        self._event("mission_timeout", id=mid, last=status)
        _api(self.port, "POST", f"/api/missions/{mid}/cancel",
             {"reason": "soak timeout"})
        return "timeout"

    def run(self) -> int:
        self._launch()
        if not self._wait_alive(timeout=180):
            self._event("boot_failed")
            return 1
        self._ensure_autonomy_on()
        # Managed-start the utility model so the first mission doesn't
        # eat the cold-boot cost inside its timeout.
        _api(self.port, "POST", "/api/runtime/start",
             {"model_id": MODEL_ID, "approve": True}, timeout=30)
        end = time.time() + self.args.duration * 60 if self.args.duration \
            else float("inf")
        idx = 0
        while self.stats["cycles"] < self.args.cycles \
                and time.time() < end:
            idx += 1
            outcome = self._run_cycle(idx)
            self.stats["cycles"] += 1
            key = {"completed": "completed", "failed": "failed",
                   "blocked": "blocked"}.get(outcome)
            if key:
                self.stats[key] += 1
            elif outcome == "timeout":
                self.stats["timeouts"] += 1
            if outcome in {"backend_dead", "restart_failed"}:
                break
        self._event("soak_done", **self.stats)
        report = {"stats": self.stats,
                  "log": str(self.log_path)}
        Path(self.args.report).write_text(json.dumps(report, indent=1))
        return 0 if self.stats["unexpected_exits"] == 0 else 2


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--config", required=True)
    p.add_argument("--port", type=int, default=8791)
    p.add_argument("--cycles", type=int, default=12)
    p.add_argument("--duration", type=float, default=0,
                   help="minutes; overrides --cycles if reached first")
    p.add_argument("--kill-every", type=int, default=3)
    p.add_argument("--mission-timeout", type=float, default=900)
    p.add_argument("--workspace", default="")
    p.add_argument("--report", default="data/soak/mission_soak.json")
    args = p.parse_args()
    if not args.workspace:
        args.workspace = str(Path(tempfile.gettempdir())
                             / "nexus-mission-soak-ws")
    ws = Path(args.workspace)
    ws.mkdir(parents=True, exist_ok=True)
    # Seed a trivially-passing test so the verify gate has a real signal.
    (ws / "test_sanity.py").write_text(
        "def test_sanity():\n    assert True\n", encoding="utf-8")
    soak = Soak(args)
    try:
        return soak.run()
    except KeyboardInterrupt:
        soak._event("interrupted", **soak.stats)
        return 130
    finally:
        if soak.proc and soak.proc.poll() is None:
            soak.proc.terminate()
            try:
                soak.proc.wait(timeout=15)
            except Exception:
                soak._kill()


if __name__ == "__main__":
    raise SystemExit(main())
