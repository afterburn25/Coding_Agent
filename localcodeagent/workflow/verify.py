from __future__ import annotations

import json
from pathlib import Path


def detect_verification_commands(workspace: Path) -> list[dict[str, str]]:
    """Return conservative verification commands that match files already present in the project."""
    root = workspace.resolve()
    commands: list[dict[str, str]] = []

    chat_nexus_tree = (
        (root / "localcodeagent" / "server.py").is_file()
        and (root / "web" / "index.html").is_file()
        and (root / "pyproject.toml").is_file()
    )

    if (root / "pyproject.toml").exists() or (root / "setup.py").exists() or (root / "tests").is_dir():
        # Chat Nexus self-development gets a stronger single approval-gated check:
        # the selftest command runs the unit suite, launches a second isolated
        # loopback instance from the edited tree, probes its APIs/UIs, then exits.
        if chat_nexus_tree:
            commands.append({
                "name": "Chat Nexus isolated self-update validation",
                "command": "python -m localcodeagent.selftest --workspace . --json",
            })
        else:
            # unittest requires no third-party dependency. pytest is left to project-specific agent decisions.
            commands.append({"name": "Python unit tests", "command": "python -m unittest discover -s tests -v"})

    package_json = root / "package.json"
    if package_json.exists():
        try:
            package = json.loads(package_json.read_text(encoding="utf-8"))
            scripts = package.get("scripts", {}) if isinstance(package, dict) else {}
            if "test" in scripts and "no test specified" not in str(scripts.get("test", "")).lower():
                commands.append({"name": "npm test", "command": "npm test -- --runInBand"})
            if "build" in scripts:
                commands.append({"name": "npm build", "command": "npm run build"})
        except (OSError, ValueError):
            pass

    if (root / "Cargo.toml").exists():
        commands.append({"name": "Cargo tests", "command": "cargo test"})
    if (root / "go.mod").exists():
        commands.append({"name": "Go tests", "command": "go test ./..."})
    if (root / "CMakeLists.txt").exists() and (root / "build").is_dir():
        commands.append({"name": "CMake build", "command": "cmake --build build"})
        commands.append({"name": "CTest", "command": "ctest --test-dir build --output-on-failure"})

    # Avoid turning a mixed monorepo into a long automatic verification storm.
    return commands[:4]
