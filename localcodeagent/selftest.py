from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen


def is_chat_nexus_tree(workspace: Path) -> bool:
    root = workspace.resolve()
    return (
        (root / "localcodeagent" / "server.py").is_file()
        and (root / "web" / "index.html").is_file()
        and (root / "pyproject.toml").is_file()
    )


def _no_window() -> int:
    """CREATE_NO_WINDOW on Windows — runs standalone, so no package import."""
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        return int(subprocess.CREATE_NO_WINDOW)
    return 0


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _get(url: str, *, timeout: float = 2.0) -> tuple[int, str, str]:
    with urlopen(url, timeout=timeout) as response:
        raw = response.read()
        return int(response.status), str(response.headers.get("Content-Type") or ""), raw.decode("utf-8", errors="replace")


def _smoke_config() -> dict[str, Any]:
    """Return a resource-safe configuration for the second validation instance."""
    return {
        "runtime_auto_start": False,
        "image_enabled": False,
        "research_enabled": False,
        "profiles_onboarding_gate": False,
        "sync_source_on_start": False,
        "max_agent_steps": 2,
        "models": [{
            "id": "selftest-model",
            "runtime": "external",
            "endpoint": "http://127.0.0.1:9/v1",
            "model": "selftest-model",
            "roles": ["utility", "fast_coder", "primary_coder", "deep_reasoner", "reviewer"],
            "enabled": True,
            "tool_calling": True,
            "priority": 1,
        }],
        "permissions": {
            "filesystem.read": "allow",
            "filesystem.write": "deny",
            "shell.execute": "deny",
            "git.execute": "deny",
            "github.read": "deny",
            "github.write": "deny",
            "network.read": "deny",
            "browser.control": "deny",
            "image.read": "deny",
            "image.generate": "deny",
            "image.manage": "deny",
        },
    }


def validate_self_update(
    workspace: Path,
    *,
    run_tests: bool = True,
    test_timeout: int = 300,
    startup_timeout: int = 30,
) -> dict[str, Any]:
    root = workspace.expanduser().resolve()
    started = time.monotonic()
    result: dict[str, Any] = {
        "ok": False,
        "workspace": str(root),
        "tests": None,
        "smoke": {},
        "duration_seconds": 0.0,
        "log_tail": "",
    }
    if not is_chat_nexus_tree(root):
        result["error"] = "Workspace is not a Nexus Core source tree."
        return result

    env = dict(os.environ)
    previous_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(root) + (os.pathsep + previous_pythonpath if previous_pythonpath else "")

    if run_tests:
        try:
            tests = subprocess.run(
                [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
                cwd=root,
                env=env,
                text=True,
                errors="replace",
                capture_output=True,
                timeout=max(30, int(test_timeout)),
                creationflags=_no_window(),
            )
        except subprocess.TimeoutExpired as exc:
            result["tests"] = {"ok": False, "timeout": True}
            result["error"] = f"Unit tests timed out after {test_timeout}s."
            result["log_tail"] = str((exc.stdout or "") + (exc.stderr or ""))[-12000:]
            result["duration_seconds"] = round(time.monotonic() - started, 3)
            return result
        test_output = (tests.stdout or "") + (tests.stderr or "")
        result["tests"] = {
            "ok": tests.returncode == 0,
            "returncode": tests.returncode,
            "output_tail": test_output[-12000:],
        }
        if tests.returncode != 0:
            result["error"] = "Unit tests failed; isolated application launch was skipped."
            result["log_tail"] = test_output[-12000:]
            result["duration_seconds"] = round(time.monotonic() - started, 3)
            return result

    port = _free_loopback_port()
    with tempfile.TemporaryDirectory(prefix="chat-nexus-selftest-") as td:
        temp = Path(td)
        config_path = temp / "selftest-config.json"
        config_path.write_text(json.dumps(_smoke_config(), indent=2), encoding="utf-8")
        command = [
            sys.executable,
            "-m",
            "localcodeagent",
            "--workspace",
            str(root),
            "--config",
            str(config_path),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ]
        process = subprocess.Popen(
            command,
            cwd=root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=_no_window(),
        )
        error = ""
        status_payload: dict[str, Any] | None = None
        deadline = time.monotonic() + max(5, int(startup_timeout))
        try:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    error = f"Isolated Nexus Core exited early with code {process.returncode}."
                    break
                try:
                    code, content_type, body = _get(f"http://127.0.0.1:{port}/api/status")
                    if code == 200 and "application/json" in content_type:
                        status_payload = json.loads(body)
                        break
                except (URLError, TimeoutError, OSError, json.JSONDecodeError):
                    time.sleep(0.2)
            if status_payload is None and not error:
                error = f"Isolated Nexus Core did not become healthy within {startup_timeout}s."

            checks: dict[str, Any] = {}
            if status_payload is not None:
                checks["status"] = {
                    "ok": bool(status_payload.get("version")),
                    "version": status_payload.get("version"),
                    "workspace": status_payload.get("workspace"),
                }
                for name, path, marker in (
                    ("main_ui", "/", "Nexus Core"),
                    ("image_ui", "/image.html", "Nexus Core"),
                    ("research_ui", "/research.html", "Nexus Core"),
                    ("trainer_ui", "/trainer.html", "Nexus Core"),
                    ("missions_ui", "/missions.html", "Nexus Core"),
                    ("projects_ui", "/projects.html", "Nexus Core"),
                    ("tools_ui", "/tools.html", "Nexus Core"),
                    ("models_ui", "/models.html", "Nexus Core"),
                    ("system_ui", "/system.html", "Nexus Core"),
                    ("answers_ui", "/answers.html", "Nexus Core"),
                    ("settings_ui", "/settings.html", "Nexus Core"),
                    ("voice_ui", "/voice.html", "Nexus Core"),
                    ("personality_ui", "/personality.html", "Nexus Core"),
                    ("health_api", "/api/health", '"overall"'),
                    ("skills_api", "/api/skills", '"skills"'),
                    ("knowledge_api", "/api/knowledge", '"stats"'),
                    ("rag_api", "/api/rag", '"stats"'),
                    ("lsp_api", "/api/lsp", '"candidates"'),
                    ("missions_api", "/api/missions", '"missions"'),
                    ("autonomy_api", "/api/autonomy/status", '"resource_mode"'),
                    ("tools_api", "/api/tools", '"tools"'),
                    ("workflows_api", "/api/workflows", '"workflows"'),
                    ("mcp_api", "/api/mcp", '"servers"'),
                    ("jobs_api", "/api/jobs", '"jobs"'),
                    ("projects_api", "/api/projects", '"projects"'),
                    ("preferences_api", "/api/preferences", '"rules"'),
                    ("library_api", "/api/library", '"documents"'),
                    ("processes_api", "/api/processes", '"processes"'),
                    ("resources_api", "/api/resources", '"hardware"'),
                    ("readiness_api", "/api/readiness", '"ready_to_code"'),
                    ("tasks_api", "/api/tasks", '"recent"'),
                    ("activity_api", "/api/activity", '"activities"'),
                    ("queue_api", "/api/queue", '"items"'),
                    ("stt_api", "/api/stt", '"available"'),
                    ("nexus_state_api", "/api/nexus/state", '"focus"'),
                    ("briefing_api", "/api/briefing", '"meaningful"'),
                    ("browser_api", "/api/browser/status", '"playwright"'),
                ):
                    # First-hit endpoints pay one-time lazy init; under parallel
                    # test load a single 2s probe can flake. Retry once, then
                    # record the precise failure for diagnosis.
                    last_exc: Exception | None = None
                    for _attempt in range(2):
                        try:
                            code, _content_type, body = _get(f"http://127.0.0.1:{port}{path}")
                            checks[name] = {"ok": code == 200 and marker in body, "status": code}
                            last_exc = None
                            break
                        except Exception as exc:
                            last_exc = exc
                            time.sleep(0.3)
                    if last_exc is not None:
                        checks[name] = {"ok": False, "error": f"{type(last_exc).__name__}: {last_exc}"}
                # The shared event bus is a long-lived SSE stream — verify the
                # handshake headers and first bytes rather than reading to EOF.
                try:
                    with urlopen(f"http://127.0.0.1:{port}/api/events", timeout=3) as sse:
                        sse_ok = sse.status == 200 and "text/event-stream" in str(sse.headers.get("Content-Type") or "")
                    checks["events_bus"] = {"ok": sse_ok}
                except Exception as exc:
                    checks["events_bus"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                failed = [k for k, item in checks.items() if not item.get("ok")]
                if failed:
                    detail = ", ".join(
                        f"{k}={checks[k].get('error') or checks[k].get('status')}"
                        for k in failed[:6])
                    error = error or (
                        f"Isolated smoke checks failed ({len(failed)}): {detail}")
            result["smoke"] = {
                "port": port,
                "checks": checks,
            }
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    output, _ = process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    output, _ = process.communicate(timeout=5)
            else:
                output, _ = process.communicate(timeout=5)
            result["log_tail"] = str(output or "")[-12000:]

        if error:
            result["error"] = error
        else:
            result["ok"] = True

    result["duration_seconds"] = round(time.monotonic() - started, 3)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate the current Nexus Core working tree in an isolated second instance.")
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--skip-tests", action="store_true", help="Skip the unit-test phase and run only the isolated server smoke test.")
    parser.add_argument("--test-timeout", type=int, default=900,
                        help="Unit-test phase budget; the full suite is ~1900 tests and exceeds the old 300s default on slower hosts.")
    parser.add_argument("--startup-timeout", type=int, default=30)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = validate_self_update(
        Path(args.workspace),
        run_tests=not args.skip_tests,
        test_timeout=args.test_timeout,
        startup_timeout=args.startup_timeout,
    )
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print("Nexus Core self-update validation:", "PASS" if result["ok"] else "FAIL")
        if result.get("tests") is not None:
            print("Tests:", "PASS" if result["tests"].get("ok") else "FAIL")
        for name, check in (result.get("smoke", {}).get("checks", {}) or {}).items():
            print(f"{name}: {'PASS' if check.get('ok') else 'FAIL'}")
        if result.get("error"):
            print("Error:", result["error"])
    raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
