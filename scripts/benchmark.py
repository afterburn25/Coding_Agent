"""Nexus Core performance benchmark — measures the installed build.

Usage:
    python scripts/benchmark.py baseline            # full suite vs running app
    python scripts/benchmark.py idle                # idle telemetry sample
    python scripts/benchmark.py chat                # TTFT + tok/s probes
    python scripts/benchmark.py voice               # Chatterbox lifecycle probe
    python scripts/benchmark.py memory [turns]      # RAM over N chat turns
    python scripts/benchmark.py processes           # process inventory

Writes JSON results to docs/benchmark-<label>.json. Read-only against the
app except `startup`, which kills and relaunches the installed build.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTALL = Path(r"D:\Nexus_Core")
OUT_DIR = ROOT / "docs"


def _ps(script: str) -> str:
    return subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True, text=True, timeout=60).stdout.strip()


def nvidia() -> dict:
    out = subprocess.run(
        ["nvidia-smi",
         "--query-gpu=memory.used,memory.total,utilization.gpu",
         "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=15).stdout.strip()
    try:
        used, total, util = [float(x) for x in out.split(",")]
        return {"vram_used_mb": used, "vram_total_mb": total,
                "gpu_util_pct": util}
    except ValueError:
        return {"vram_used_mb": None, "vram_total_mb": None,
                "gpu_util_pct": None, "raw": out}


def _proc_tree() -> list[dict]:
    """All processes belonging to the Nexus install (backend tree + host +
    runtime children)."""
    rows = _ps(
        "Get-CimInstance Win32_Process | "
        "Where-Object {$_.ExecutablePath -like 'D:\\Nexus_Core*' -or "
        "$_.Name -in 'NexusCore.exe','ChatNexus.Backend.exe'} | "
        "Select-Object ProcessId,ParentProcessId,Name,WorkingSetSize,"
        "KernelModeTime,UserModeTime | ConvertTo-Json")
    if not rows:
        return []
    data = json.loads(rows)
    if isinstance(data, dict):
        data = [data]
    out = []
    for p in data:
        cpu_ticks = int(p.get("KernelModeTime") or 0) + int(
            p.get("UserModeTime") or 0)
        out.append({
            "pid": p["ProcessId"], "ppid": p.get("ParentProcessId"),
            "name": p["Name"],
            "ram_mb": round((p.get("WorkingSetSize") or 0) / 1e6, 1),
            "cpu_s": round(cpu_ticks / 1e7, 2)})
    return out


def _threads() -> int:
    out = _ps(
        "(Get-Process | Where-Object {$_.Path -like 'D:\\Nexus_Core*' -or "
        "$_.Name -in 'NexusCore','ChatNexus.Backend'} | "
        "Measure-Object -Property Threads -Sum).Sum")
    try:
        return int(out)
    except (ValueError, TypeError):
        return -1


def find_backend() -> tuple[int, str]:
    """Return (pid, base_url) of the running installed backend."""
    out = _ps(
        "Get-CimInstance Win32_Process -Filter \"Name='ChatNexus.Backend.exe'\" "
        "| Select-Object ProcessId,CommandLine,ExecutablePath | ConvertTo-Json")
    data = json.loads(out) if out else []
    if isinstance(data, dict):
        data = [data]
    for p in data:
        if "Nexus_Core" in (p.get("ExecutablePath") or ""):
            m = re.search(r"--port (\d+)", p.get("CommandLine") or "")
            if m:
                return p["ProcessId"], f"http://127.0.0.1:{m.group(1)}"
    raise RuntimeError("installed backend not running")


def _post(base: str, path: str, body: dict, timeout: float = 300) -> dict:
    req = urllib.request.Request(
        base + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _get(base: str, path: str, timeout: float = 30) -> dict:
    with urllib.request.urlopen(base + path, timeout=timeout) as r:
        return json.loads(r.read())


def chat_latency(base: str, message: str, timeout: float = 300) -> dict:
    t0 = time.perf_counter()
    try:
        res = _post(base, "/api/chat", {"message": message}, timeout)
        elapsed = time.perf_counter() - t0
        content = res.get("content") or ""
        return {"ok": True, "total_s": round(elapsed, 2),
                "chars": len(content),
                "model": (res.get("model_events") or [{}])[0].get("model")}
    except Exception as exc:
        return {"ok": False, "error": str(exc),
                "total_s": round(time.perf_counter() - t0, 2)}


def chat_stream(base: str, message: str, timeout: float = 300) -> dict:
    """SSE stream — measures time-to-first-token and token rate."""
    req = urllib.request.Request(
        base + "/api/chat/stream", data=json.dumps(
            {"message": message}).encode(),
        headers={"Content-Type": "application/json",
                 "Accept": "text/event-stream"})
    t0 = time.perf_counter()
    ttft = None
    tokens = 0
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if ttft is None and line == "event: token":
                    ttft = time.perf_counter() - t0
                if line == "event: token":
                    tokens += 1
                if '"done"' in line or '"result"' in line and '"final"' in line:
                    break
        total = time.perf_counter() - t0
        return {"ok": True, "ttft_s": round(ttft or -1, 2),
                "total_s": round(total, 2), "token_events": tokens,
                "tok_per_s": round(tokens / max(total - (ttft or 0), .01), 1)}
    except Exception as exc:
        return {"ok": False, "error": str(exc),
                "total_s": round(time.perf_counter() - t0, 2)}


def snapshot() -> dict:
    procs = _proc_tree()
    gpu = nvidia()
    return {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "processes": len(procs),
        "ram_mb_total": round(sum(p["ram_mb"] for p in procs), 1),
        "threads": _threads(),
        **gpu,
        "by_process": procs,
    }


def _wait_backend(timeout_s: float = 180) -> tuple[float, str]:
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < timeout_s:
        try:
            pid, base = find_backend()
            _get(base, "/api/status", timeout=2)
            return time.perf_counter() - t0, base
        except Exception:
            time.sleep(0.5)
    raise RuntimeError("backend never came up")


def cmd_idle(label: str = "idle") -> dict:
    print("sampling idle for 30s ...")
    base_pid, base = find_backend()
    samples = []
    for _ in range(6):
        samples.append(snapshot())
        time.sleep(5)
    # CPU% per process between first and last sample
    d_cpu = {p["name"]: p["cpu_s"] for p in samples[-1]["by_process"]}
    t_cpu = {p["name"]: p["cpu_s"] for p in samples[0]["by_process"]}
    span = 25.0
    cpu_pct = {k: round((d_cpu[k] - t_cpu.get(k, 0)) / span * 100, 2)
               for k in d_cpu}
    res = {"mode": "idle", "samples": samples,
           "ram_mb_total": samples[-1]["ram_mb_total"],
           "vram_mb": samples[-1]["vram_used_mb"],
           "processes": samples[-1]["processes"],
           "threads": samples[-1]["threads"],
           "cpu_pct_by_process": cpu_pct}
    _save(res, label)
    return res


def cmd_chat(base: str | None = None, label: str = "chat") -> dict:
    base = base or find_backend()[1]
    probes = {
        "simple": "What is 2 + 2? Answer in one word.",
        "normal": "Explain what a Python list comprehension is, briefly.",
    }
    res = {"mode": "chat"}
    for name, msg in probes.items():
        print(f"  {name} probe ...")
        res[name] = chat_stream(base, msg)
        res[name + "_blocking"] = chat_latency(base, msg)
    _save(res, label)
    return res


def cmd_voice(base: str | None = None, label: str = "voice") -> dict:
    base = base or find_backend()[1]
    res = {"mode": "voice"}
    st = _get(base, "/api/voice/status")
    res["initial_engine"] = st.get("engine")
    res["vram_before_mb"] = nvidia()["vram_used_mb"]
    # Force-unload then measure cold synthesis → TTFA
    try:
        _post(base, "/api/voice/unload", {}, timeout=30)
    except Exception:
        pass
    res["vram_unloaded_mb"] = nvidia()["vram_used_mb"]
    t0 = time.perf_counter()
    r = _post(base, "/api/voice/preview",
              {"text": "Benchmark voice check."},
              timeout=180)
    res["cold_ttfa_s"] = round(time.perf_counter() - t0, 2)
    res["cold_audio_s"] = r.get("duration_s") or r.get("seconds")
    res["vram_loaded_mb"] = nvidia()["vram_used_mb"]
    t0 = time.perf_counter()
    r2 = _post(base, "/api/voice/preview",
               {"text": "Second line, warm engine."},
               timeout=120)
    warm_s = time.perf_counter() - t0
    res["warm_ttfa_s"] = round(warm_s, 2)
    dur2 = r2.get("duration_s") or r2.get("seconds") or 0
    res["warm_rtf"] = round(warm_s / dur2, 2) if dur2 else None
    _save(res, label)
    return res


def cmd_memory(base: str | None = None, turns: int = 10,
               label: str = "memory") -> dict:
    base = base or find_backend()[1]
    res = {"mode": "memory", "turns": turns, "series": []}
    res["series"].append({"turn": 0, **snapshot()})
    for i in range(1, turns + 1):
        r = chat_latency(base, f"Memory probe {i}: reply with only the "
                               f"number {i}.", timeout=120)
        snap = snapshot()
        snap["turn"] = i
        snap["chat_s"] = r.get("total_s")
        res["series"].append(snap)
        print(f"  turn {i}/{turns}  ram={snap['ram_mb_total']}MB  "
              f"chat={r.get('total_s')}s")
    res["ram_growth_mb"] = round(
        res["series"][-1]["ram_mb_total"] - res["series"][0]["ram_mb_total"], 1)
    _save(res, label)
    return res


def cmd_processes(label: str = "processes") -> dict:
    res = {"mode": "processes", **snapshot()}
    _save(res, label)
    for p in res["by_process"]:
        print(f"  {p['pid']:>6}  {p['name']:<28} {p['ram_mb']:>9.1f} MB")
    return res


def _save(res: dict, label: str) -> None:
    OUT_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = OUT_DIR / f"benchmark-{label}-{ts}.json"
    path.write_text(json.dumps(res, indent=2))
    print(f"  -> {path}")


def cmd_baseline() -> dict:
    """Full baseline against the *currently running* installed app."""
    _, base = find_backend()
    res = {"mode": "baseline",
           "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "version": _get(base, "/api/status").get("version")}
    print("[1/4] idle sample")
    res["idle"] = cmd_idle("baseline-idle")
    print("[2/4] chat probes")
    res["chat"] = cmd_chat(base, "baseline-chat")
    print("[3/4] voice probes")
    res["voice"] = cmd_voice(base, "baseline-voice")
    print("[4/4] memory (10 turns)")
    res["memory"] = cmd_memory(base, turns=10, label="baseline-memory")
    _save(res, "baseline")
    return res


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "baseline"
    arg = sys.argv[2] if len(sys.argv) > 2 else None
    if cmd == "idle":
        cmd_idle()
    elif cmd == "chat":
        cmd_chat()
    elif cmd == "voice":
        cmd_voice()
    elif cmd == "memory":
        cmd_memory(turns=int(arg or 10))
    elif cmd == "processes":
        cmd_processes()
    elif cmd == "baseline":
        r = cmd_baseline()
        print(json.dumps({k: v for k, v in r.items()
                          if k != "ts"}, indent=1)[:4000])
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
