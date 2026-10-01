"""Container-isolated execution — runs commands inside Docker containers.

`docker_run` delegates to the `docker` manifest tool so it inherits the
`docker.access` permission gate and TOOL_NOT_INSTALLED reporting. The
workspace is bind-mounted read-write at /work by default so produced
artifacts land back in the project.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .base import ToolRegistry, ToolSpec

SAFE_IMAGES = {"python", "python:3", "python:3.11", "python:3.12", "python:3.13",
               "node", "node:20", "node:22", "alpine", "ubuntu", "debian",
               "gcc", "golang", "rust", "busybox"}


def register_docker_tools(registry: ToolRegistry, workspace: Path) -> None:

    def docker_run(args: dict[str, Any]) -> str:
        image = str(args.get("image", "")).strip()
        command = args.get("command")
        if not image:
            return "ERROR: 'image' is required (e.g. python:3.12, node:22)"
        if not command:
            return "ERROR: 'command' is required — argv run inside the container"
        argv = command if isinstance(command, list) else ["sh", "-c", str(command)]
        docker_args: list[str] = ["run", "--rm"]
        if not bool(args.get("network", False)):
            docker_args += ["--network", "none"]
        docker_args += ["--memory", str(args.get("memory", "512m")),
                        "--cpus", str(args.get("cpus", "1")),
                        "-v", f"{workspace}:/work", "-w", "/work"]
        for env in args.get("env", []) if isinstance(args.get("env"), list) else []:
            docker_args += ["-e", str(env)]
        docker_args.append(image)
        docker_args += [str(c) for c in argv]
        return registry.execute("docker", {"args": docker_args},
                                approved=bool(args.get("approved", False)))

    def docker_images(args: dict[str, Any]) -> str:
        return registry.execute("docker", {"args": ["images", "--format",
                                                    "{{.Repository}}:{{.Tag}} {{.Size}}"]},
                                approved=bool(args.get("approved", False)))

    registry.register(ToolSpec(
        "docker_run",
        "Run a command inside an isolated Docker container (workspace mounted at /work, no network by default, memory/CPU limited). Real sandboxing for untrusted builds/tests — requires Docker Desktop/daemon.",
        {
            "type": "object",
            "properties": {
                "image": {"type": "string", "description": "container image, e.g. python:3.12"},
                "command": {"description": "argv list or shell string (run via sh -c when a string)"},
                "network": {"type": "boolean", "default": False, "description": "allow container network"},
                "memory": {"type": "string", "default": "512m"},
                "cpus": {"type": "string", "default": "1"},
                "env": {"type": "array", "items": {"type": "string"}, "description": "KEY=VALUE env vars"},
            },
            "required": ["image", "command"],
        },
        "docker.access", docker_run,
        category="devops",
        capabilities=["run_container", "isolated_execute", "docker_run"],
    ))
    registry.register(ToolSpec(
        "docker_images",
        "List locally available Docker images (delegates to the docker manifest tool).",
        {"type": "object", "properties": {}},
        "docker.access", docker_images,
        category="devops",
        capabilities=["list_images", "docker_manage"],
    ))
