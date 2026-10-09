"""Launch the section-42 dogfood mission against the live deployment."""
import json
import urllib.request

body = {
    "objective": (
        "Add a plain-English mission report endpoint and UI: "
        "GET /api/missions/<id>/report returns a compact human-readable "
        "summary of the mission (title, status, per-workstream progress, "
        "metrics); the missions detail page gains a Report panel that "
        "renders it; add a test for the payload shape; document the "
        "Engineering Missions layer in docs/ARCHITECTURE.md."
    ),
    "title": "Mission report endpoint + UI",
    "scope": "repository",
    "root": r"D:\Nexus_Core\Source",
    "decomposition": [
        {"title": "backend report endpoint",
         "instruction": ("In localcodeagent/server.py add GET "
                         "/api/missions/<id>/report returning "
                         "{title,status,workstreams:[{title,status,progress}],"
                         "metrics,summary} - a plain-English rollup built "
                         "from workstream_rollup + metrics + recent history. "
                         "Reuse existing helpers; keep it under 60 lines."),
         "scope": ["localcodeagent/server.py"], "role": "backend"},
        {"title": "web report panel",
         "instruction": ("In web/missions.js add a Report section to the "
                         "mission detail page that fetches "
                         "/api/missions/<id>/report and renders the summary "
                         "and workstream lines. Style additions go in "
                         "web/missions.css. Follow existing detail-section "
                         "patterns."),
         "scope": ["web/missions.js", "web/missions.css"],
         "role": "frontend"},
        {"title": "endpoint test",
         "instruction": ("In tests/test_autonomy.py add a test that builds "
                         "a mission with a workstream and asserts the "
                         "report payload shape produced by the report "
                         "builder (test the helper, not HTTP). Keep it "
                         "small."),
         "scope": ["tests/test_autonomy.py"], "role": "test"},
        {"title": "architecture docs",
         "instruction": ("In docs/ARCHITECTURE.md add an 'Engineering "
                         "Missions' section: mission to workstream to task "
                         "hierarchy, context capsule, ownership leases, "
                         "worktrees, steering, checkpoints. Match existing "
                         "doc style."),
         "scope": ["docs/"], "role": "docs"},
    ],
    "success_criteria": [{"kind": "verify_passed"}],
    "start": True,
}

req = urllib.request.Request(
    "http://127.0.0.1:54332/api/missions",
    data=json.dumps(body).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(req, timeout=20) as r:
    d = json.loads(r.read())
m = d.get("mission") or {}
print("id:", m.get("id"))
print("status:", m.get("status"))
print("workstreams:", len(m.get("workstreams") or []))
print("criteria:", m.get("acceptance_criteria"))
print("error:", d.get("error"))
