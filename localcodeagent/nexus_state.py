"""Nexus operational state — a structured, honest snapshot of what the
workstation is doing right now.

This is deliberately NOT a claim of consciousness. It is an operational
model — focus, load, pressure, next action — assembled from measured
sources (worker manager, queue, missions, resource monitor) so the UI,
avatar, and return briefings all read one coherent picture.
"""
from __future__ import annotations

import time
from typing import Any

# How long without user interaction counts as "away" for a briefing.
AWAY_THRESHOLD_S = 20 * 60


def build_state(*, workers: Any = None, queue: Any = None,
                missions: Any = None, resources: dict | None = None,
                last_interaction_at: float = 0.0,
                profile: dict | None = None) -> dict[str, Any]:
    """Assemble the operational-state snapshot from live sources.

    Every field is derived — nothing here is stored; callers recompute.
    """
    res = resources or {}
    w = {}
    try:
        if workers is not None:
            w = workers.status() or {}
    except Exception:
        w = {}
    active_workers = w.get("workers") or []
    queued = w.get("queue") or []
    running = [x for x in active_workers
               if str(x.get("status")) in {"running", "starting"}]
    capacity = w.get("capacity") or {}

    focus = ""
    active_mission = ""
    try:
        if missions is not None:
            act = missions.active() or []
            if act:
                focus = str(act[0].get("title") or act[0].get("objective")
                            or "")[:140]
                active_mission = str(act[0].get("id") or "")
    except Exception:
        pass
    if not focus and running:
        focus = str(running[0].get("title") or "")[:140]

    # Pressure: RAM/VRAM headroom + queue depth — the honest load signal.
    ram_pct = float(res.get("ram_used_pct") or 0.0)
    vram_free = float(res.get("vram_free_mb") or 0.0)
    vram_total = float(res.get("vram_total_mb") or 0.0)
    vram_pct = (1.0 - vram_free / vram_total) * 100 if vram_total else 0.0
    queue_depth = len(queued)
    pressure = "low"
    if ram_pct > 85 or vram_pct > 90 or queue_depth >= 6:
        pressure = "high"
    elif ram_pct > 65 or vram_pct > 70 or queue_depth >= 2:
        pressure = "moderate"

    # Operational "mood" — a presentation hint for avatar/voice, not a
    # claim about feelings. Idle when nothing runs; focused under load;
    # pressured when resources are tight; concerned on failures.
    mood = "idle"
    if running or queue_depth:
        mood = "focused"
    if pressure == "high":
        mood = "pressured"
    try:
        fails = [x for x in (w.get("recent") or [])[-8:]
                 if str(x.get("outcome")) == "failed"]
        if len(fails) >= 3:
            mood = "concerned"
    except Exception:
        pass

    idle_s = max(0.0, time.time() - last_interaction_at) \
        if last_interaction_at else 0.0
    return {
        "focus": focus,
        "mission": active_mission,
        "state": mood,
        "workers_active": len(running),
        "workers_total": len(active_workers),
        "queue_depth": queue_depth,
        "capacity": {"workers_safe": capacity.get("safe_workers"),
                     "ram_pct": round(ram_pct, 1),
                     "vram_pct": round(vram_pct, 1)},
        "resource_pressure": pressure,
        "idle_seconds": round(idle_s, 1),
        "away": idle_s >= AWAY_THRESHOLD_S and last_interaction_at > 0,
        "profile": str((profile or {}).get("profile_id") or ""),
        "next_action": (str(queued[0].get("title") or "")[:140]
                        if queued else ""),
        "ts": time.time(),
    }


def build_briefing(*, since: float, jobs: Any = None,
                   workers: Any = None, queue: Any = None,
                   notifications: Any = None,
                   missions: Any = None) -> dict[str, Any]:
    """"While you were away" digest — bounded, evidence-only.

    Counts completions/failures/commits/repairs since `since`; unread
    notifications and still-queued items carry through. Empty when
    nothing happened — the caller decides whether to surface it.
    """
    out: dict[str, Any] = {"since": since, "meaningful": False,
                           "completed": [], "failed": [], "queued": [],
                           "commits": [], "repairs": [],
                           "approvals": [], "notes": []}
    try:
        if jobs is not None:
            for j in (jobs.list_jobs() or [])[:300]:
                ts = float(j.get("updated_at") or j.get("created_at") or 0)
                if ts < since:
                    continue
                st = str(j.get("state") or j.get("status") or "")
                row = {"id": j.get("id"), "title": str(j.get("title")
                                                         or "")[:120],
                       "status": st}
                if st == "completed":
                    out["completed"].append(row)
                elif st == "failed":
                    out["failed"].append(row)
    except Exception:
        pass
    try:
        if workers is not None:
            wstatus = workers.status() or {}
            for h in (wstatus.get("recent") or []):
                if float(h.get("ended_at") or 0) < since:
                    continue
                if str(h.get("outcome")) == "failed":
                    out["failed"].append({"id": h.get("id"),
                                          "title": str(h.get("title")
                                                         or "")[:120],
                                          "status": "failed"})
            q = wstatus.get("queue") or []
            out["queued"] = [{"id": x.get("id"),
                              "title": str(x.get("title") or "")[:120],
                              "reason": x.get("reason")} for x in q[:10]]
    except Exception:
        pass
    try:
        if notifications is not None:
            for n in (notifications.list(unread_only=True, limit=50) or []):
                if float(n.get("ts") or 0) < since:
                    continue
                lvl = str(n.get("level") or "")
                if lvl in {"approval", "approval_required"}:
                    out["approvals"].append(str(n.get("title") or "")[:140])
                else:
                    out["notes"].append(str(n.get("title") or "")[:140])
    except Exception:
        pass
    try:
        if missions is not None:
            for m in (missions.list() or [])[:40]:
                comp = m.get("completion") or {}
                fin = float(comp.get("finished_at") or 0)
                if fin >= since:
                    if comp.get("state") == "completed":
                        out["completed"].append({"id": m.get("id"),
                                                 "title": str(m.get(
                                                     "title") or "")[:120],
                                                 "status": "completed"})
                    elif "failed" in str(comp.get("state") or ""):
                        out["failed"].append({"id": m.get("id"),
                                              "title": str(m.get(
                                                  "title") or "")[:120],
                                              "status": "failed"})
    except Exception:
        pass
    out["meaningful"] = bool(out["completed"] or out["failed"]
                             or out["approvals"] or out["repairs"]
                             or out["commits"])
    # Dedupe completions/failures by id.
    for key in ("completed", "failed"):
        seen, rows = set(), []
        for r in out[key]:
            rid = r.get("id") or r.get("title")
            if rid in seen:
                continue
            seen.add(rid)
            rows.append(r)
        out[key] = rows[:12]
    out["counts"] = {k: len(out[k]) for k in
                     ("completed", "failed", "queued", "approvals",
                      "commits", "repairs", "notes")}
    return out


def build_situation(*, env: dict[str, Any] | None = None,
                    base: dict[str, Any] | None = None) -> dict[str, Any]:
    """The Situation Model — what is happening in Nexus's world *now*.

    Builds on the operational snapshot and adds the wider picture:
    active conversation + goal, missions and workstreams, running
    jobs/downloads/installs, resident models, pending approvals,
    connected services, waiting peer consults, recent failures, the
    current project, and artifacts awaiting delivery.

    Every source is injected through ``env`` callables and every read is
    defensive — a missing subsystem degrades its section, never the
    whole snapshot. Nothing is persisted here; this is a live view.
    """
    env = env or {}
    sit: dict[str, Any] = dict(base or {})

    def _call(key: str, default: Any = None, *a: Any) -> Any:
        fn = env.get(key)
        if fn is None:
            return default
        try:
            return fn(*a)
        except Exception:
            return default

    # -- conversation / user focus -----------------------------------------
    conv = _call("conversation", {}) or {}
    sit["conversation"] = {
        "active": bool(conv.get("active")),
        "topic": str(conv.get("topic") or "")[:140],
        "goal": str(conv.get("goal") or "")[:200],
        "user_present": not bool(sit.get("away")),
    }

    # -- missions + workstreams ---------------------------------------------
    missions = {"active": [], "paused": [], "workstreams_active": 0}
    try:
        store = env.get("missions")
        for m in (store.list() or [])[:60] if store is not None else []:
            st = str(m.get("status") or "")
            title = str(m.get("title") or m.get("objective") or "")[:140]
            if st == "active":
                ws_active = 0
                try:
                    ws = m.get("workstreams") or []
                    ws_active = sum(1 for x in ws
                                    if str(x.get("status")) == "active")
                except Exception:
                    pass
                missions["active"].append({"id": m.get("id"),
                                           "title": title,
                                           "workstreams": ws_active})
                missions["workstreams_active"] += ws_active
            elif st == "paused":
                missions["paused"].append({"id": m.get("id"),
                                           "title": title})
    except Exception:
        pass
    sit["missions"] = missions

    # -- activity: jobs / installs / downloads ------------------------------
    activity = {"running": [], "installs": [], "downloads": []}
    try:
        jobs = env.get("jobs")
        for j in (jobs.list_jobs() or [])[:80] if jobs is not None else []:
            st = str(j.get("state") or j.get("status") or "")
            if st not in ("running", "pending", "queued"):
                continue
            row = {"id": j.get("id"),
                   "title": str(j.get("title") or "")[:120],
                   "kind": str(j.get("kind") or j.get("type") or "")}
            kl = row["kind"].lower() + " " + row["title"].lower()
            if "install" in kl:
                activity["installs"].append(row)
            elif "download" in kl:
                activity["downloads"].append(row)
            else:
                activity["running"].append(row)
    except Exception:
        pass
    for k in activity:
        activity[k] = activity[k][:8]
    sit["activity"] = activity

    # -- models --------------------------------------------------------------
    models = _call("models_resident", {}) or {}
    sit["models"] = {
        "resident": str(models.get("resident") or models.get("model") or ""),
        "role": str(models.get("role") or ""),
        "vram_mb": int(models.get("vram_mb") or 0),
    }

    # -- approvals ------------------------------------------------------------
    pending = _call("pending_approvals", []) or []
    sit["approvals"] = {"pending": [str(p)[:140] for p in pending[:8]],
                        "count": len(pending)}

    # -- connected services -----------------------------------------------------
    services: dict[str, str] = {}
    try:
        connectors = env.get("connectors")
        rows = connectors.status() if connectors is not None else []
        for r in (rows or []):
            name = str(r.get("name") or "")
            if not name:
                continue
            if not r.get("enabled", True):
                services[name] = "disabled"
            elif r.get("authed"):
                services[name] = "connected"
            else:
                services[name] = "unauthenticated"
    except Exception:
        pass
    sit["services"] = services

    # -- social: waiting peer consults -----------------------------------------
    social = {"waiting_consults": 0, "pending_notifications": 0}
    try:
        consults = _call("waiting_consults", []) or []
        social["waiting_consults"] = len(consults)
    except Exception:
        pass
    sit["social"] = social

    # -- recent failures --------------------------------------------------------
    failures = _call("recent_failures", []) or []
    sit["failures"] = {"recent": [str(f)[:140] for f in failures[:6]],
                       "count": len(failures)}

    # -- project + artifacts ----------------------------------------------------
    sit["project"] = str(_call("current_project", "") or "")
    sit["artifacts_awaiting"] = int(_call("artifacts_awaiting", 0) or 0)

    sit["ts"] = time.time()
    return sit


def situation_text(sit: dict[str, Any]) -> str:
    """Compact, honest natural-language answer to 'what's going on?' —
    a sentence or two assembled from the situation snapshot. Never a raw
    JSON dump."""
    if not sit:
        return "Nothing is running right now."
    parts: list[str] = []

    conv = sit.get("conversation") or {}
    if conv.get("active"):
        if conv.get("goal"):
            parts.append(f"You're my focus — working on "
                         f"\"{conv['goal']}\"")
        else:
            parts.append("Your conversation has priority")

    ms = sit.get("missions") or {}
    n_active = len(ms.get("active") or [])
    n_ws = int(ms.get("workstreams_active") or 0)
    if n_active:
        t = ms["active"][0].get("title") or "unnamed mission"
        bit = (f"{n_active} mission{'s' if n_active != 1 else ''} running"
               f" ({t}" + (f" +{n_active - 1} more" if n_active > 1 else "")
               + ")")
        if n_ws:
            bit += f" with {n_ws} active workstream{'s' if n_ws != 1 else ''}"
        parts.append(bit)
    n_paused = len(ms.get("paused") or [])
    if n_paused:
        parts.append(f"{n_paused} mission{'s' if n_paused != 1 else ''} paused")

    act = sit.get("activity") or {}
    n_run = len(act.get("running") or [])
    n_dl = len(act.get("downloads") or [])
    n_in = len(act.get("installs") or [])
    if n_run:
        parts.append(f"{n_run} job{'s' if n_run != 1 else ''} in flight")
    if n_dl:
        parts.append(f"{n_dl} download{'s' if n_dl != 1 else ''} active")
    if n_in:
        parts.append(f"{n_in} install{'s' if n_in != 1 else ''} running")

    appr = sit.get("approvals") or {}
    if appr.get("count"):
        parts.append(f"{appr['count']} approval"
                     f"{'s' if appr['count'] != 1 else ''} waiting on you")

    soc = sit.get("social") or {}
    if soc.get("waiting_consults"):
        n = soc["waiting_consults"]
        parts.append(f"{n} peer consult{'s' if n != 1 else ''} awaiting a reply")

    models = sit.get("models") or {}
    if models.get("resident"):
        parts.append(f"{models['resident']} is resident")

    fails = sit.get("failures") or {}
    if fails.get("count"):
        parts.append(f"{fails['count']} recent failure"
                     f"{'s' if fails['count'] != 1 else ''} to note")

    if not parts:
        idle = sit.get("idle_seconds") or 0
        if sit.get("away"):
            return "Idle — nothing running, and you've been away a while."
        return "Idle — nothing running right now."
    return ". ".join(parts) + "."


def briefing_text(brief: dict[str, Any]) -> str:
    """One-paragraph natural-language digest for UI + optional voice."""
    if not brief.get("meaningful"):
        return ""
    c = brief.get("counts") or {}
    parts = []
    if c.get("completed"):
        parts.append(f"{c['completed']} task"
                     f"{'s' if c['completed'] != 1 else ''} completed")
    if c.get("failed"):
        parts.append(f"{c['failed']} failed")
    if c.get("queued"):
        parts.append(f"{c['queued']} still queued")
    if c.get("approvals"):
        parts.append(f"{c['approvals']} approval"
                     f"{'s' if c['approvals'] != 1 else ''} waiting")
    text = "While you were away: " + ", ".join(parts) + "."
    if c.get("approvals"):
        text += " The approvals need your attention."
    return text
