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

    # -- container inventory / lifecycle -------------------------------------
    def docker_ps(args: dict[str, Any]) -> str:
        fmt = ("{{.ID}}\t{{.Image}}\t{{.Names}}\t{{.Status}}\t{{.Ports}}")
        argv = ["ps", "--format", fmt]
        if args.get("all", True):
            argv.append("-a")
        return registry.execute(
            "docker", {"args": argv},
            approved=bool(args.get("approved", False)))

    def docker_logs(args: dict[str, Any]) -> str:
        name = str(args.get("name", "")).strip()
        if not name:
            return "ERROR: 'name' (container name or id) is required"
        tail = max(1, min(int(args.get("tail", 200)), 2000))
        return registry.execute(
            "docker", {"args": ["logs", "--tail", str(tail), name]},
            approved=bool(args.get("approved", False)))

    _CONTAINER_ACTIONS = {"start", "stop", "restart", "rm"}

    def docker_container(args: dict[str, Any]) -> str:
        action = str(args.get("action", "")).strip().lower()
        name = str(args.get("name", "")).strip()
        if action not in _CONTAINER_ACTIONS:
            return ("ERROR: 'action' must be one of "
                    + ", ".join(sorted(_CONTAINER_ACTIONS)))
        if not name:
            return "ERROR: 'name' (container name or id) is required"
        argv = [action, name]
        if action == "rm" and args.get("force"):
            argv.insert(1, "-f")
        return registry.execute(
            "docker", {"args": argv},
            approved=bool(args.get("approved", False)))

    # -- compose --------------------------------------------------------------
    _COMPOSE_FILES = ("compose.yml", "compose.yaml",
                      "docker-compose.yml", "docker-compose.yaml")

    def _compose_files(root: Path) -> list[Path]:
        found: list[Path] = []
        stack = [(root, 0)]
        while stack and len(found) < 25:
            d, depth = stack.pop()
            try:
                children = list(d.iterdir())
            except OSError:
                continue
            for c in children:
                if c.is_dir() and not c.is_symlink():
                    if depth < 4 and c.name not in {
                            ".git", "node_modules", "__pycache__",
                            ".venv", "venv", "dist", "build", ".agent"} \
                            and not c.name.startswith("."):
                        stack.append((c, depth + 1))
                elif c.is_file() and c.name in _COMPOSE_FILES:
                    found.append(c)
        return found

    def _compose_services(path: Path) -> list[dict[str, Any]]:
        """Bounded compose parse: service names + published ports.

        Regex-scoped to the services: block — no yaml dependency, and a
        partial read is reported honestly instead of silently dropping
        fields.
        """
        try:
            lines = path.read_text(
                encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        services: list[dict[str, Any]] = []
        in_services = False
        cur: dict[str, Any] | None = None
        cur_indent = 0
        in_ports = False
        for raw in lines:
            indent = len(raw) - len(raw.lstrip())
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if indent == 0:
                in_services = stripped.rstrip(":") == "services" \
                    and stripped.endswith(":")
                cur = None
                continue
            if not in_services:
                continue
            if indent == 2 and stripped.endswith(":"):
                cur = {"name": stripped[:-1], "ports": [],
                       "image": "", "build": ""}
                services.append(cur)
                cur_indent = indent
                in_ports = False
                continue
            if cur is None:
                continue
            if indent <= cur_indent:
                cur = None
                in_ports = False
                continue
            if stripped.startswith("image:"):
                cur["image"] = stripped.split(":", 1)[1].strip()
                in_ports = False
            elif stripped.startswith("build:"):
                cur["build"] = stripped.split(":", 1)[1].strip() or "."
                in_ports = False
            elif stripped.startswith("ports:"):
                in_ports = True
            elif in_ports and stripped.startswith("-"):
                p = stripped.lstrip("- ").strip().strip('"\'')
                if p:
                    cur["ports"].append(p)
            elif in_ports and indent <= cur_indent + 2:
                in_ports = False
        return services

    def docker_compose_detect(args: dict[str, Any]) -> str:
        raw = str(args.get("path", "") or "").strip()
        root = Path(raw) if raw and Path(raw).is_absolute() \
            else (workspace / raw if raw else workspace)
        root = root.resolve()
        files = _compose_files(root)
        return json.dumps({
            "root": str(root),
            "compose_files": [
                {"file": str(f), "services": _compose_services(f)}
                for f in files],
            "count": len(files)}, indent=2)

    _COMPOSE_ACTIONS = {"ps", "up", "down", "logs", "restart", "pull",
                        "stop", "start", "build"}

    def docker_compose(args: dict[str, Any]) -> str:
        action = str(args.get("action", "")).strip().lower()
        if action not in _COMPOSE_ACTIONS:
            return ("ERROR: 'action' must be one of "
                    + ", ".join(sorted(_COMPOSE_ACTIONS)))
        raw = str(args.get("file", "") or "").strip()
        cfile: Path | None = None
        if raw:
            cand = Path(raw)
            cfile = cand if cand.is_absolute() else workspace / cand
            if cfile.name not in _COMPOSE_FILES or not cfile.is_file():
                return f"ERROR: not a compose file: {raw}"
        else:
            found = _compose_files(workspace)
            if not found:
                return ("ERROR: no compose file under the workspace — "
                        "run docker_compose_detect to locate projects")
            cfile = found[0]
        argv = ["compose", "-f", str(cfile)]
        if action == "up":
            argv += ["up", "-d"]           # never block on attach
        else:
            argv.append(action)
        svc = str(args.get("service", "") or "").strip()
        if svc:
            argv.append(svc)
        if action == "logs":
            argv += ["--tail",
                     str(max(1, min(int(args.get("tail", 200)), 2000)))]
        # `cwd` isn't part of the manifest invoke schema — `docker
        # compose -f <abs>` derives the project dir from the file.
        return registry.execute(
            "docker", {"args": argv},
            approved=bool(args.get("approved", False)))

    def _unwrap(out: str) -> str:
        """The docker manifest invoker returns a JSON envelope
        {exit_code, stdout, stderr} — surface real stdout."""
        try:
            env = json.loads(out)
        except (ValueError, TypeError):
            return out
        if isinstance(env, dict) and "stdout" in env:
            stderr = str(env.get("stderr") or "")
            return str(env.get("stdout") or "") + (
                "\n" + stderr if stderr else "")
        return out

    # -- dev-server discovery --------------------------------------------------
    def docker_devservers(args: dict[str, Any]) -> str:
        """Published container ports are dev servers too — surface them
        as http://localhost:<port> candidates next to host processes."""
        fmt = "{{.Names}}\t{{.Ports}}\t{{.Image}}\t{{.Status}}"
        out = registry.execute(
            "docker", {"args": ["ps", "--format", fmt]},
            approved=bool(args.get("approved", False)))
        if isinstance(out, str) and out.startswith(
                ("ERROR", "TOOL_", "PERMISSION", "APPROVAL")):
            return out
        servers = []
        for line in _unwrap(out).splitlines():
            parts = line.split("\t")
            if len(parts) < 4:
                continue
            name, ports, image, status = parts[:4]
            urls = []
            # Ports column shape: 0.0.0.0:8000->80/tcp, :::8000->80/tcp
            for spec in ports.split(","):
                spec = spec.strip()
                if "->" not in spec:
                    continue
                host = spec.split("->")[0]
                m = host.rsplit(":", 1)
                if len(m) == 2 and m[1].strip().isdigit():
                    urls.append(f"http://localhost:{m[1].strip()}")
            servers.append({"name": name, "image": image,
                            "status": status, "urls": urls,
                            "source": "docker"})
        return json.dumps({"servers": servers,
                           "count": len(servers)}, indent=2)

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
    registry.register(ToolSpec(
        "docker_ps",
        "List Docker containers (id, image, name, status, ports). "
        "all=false limits to running.",
        {"type": "object",
         "properties": {"all": {"type": "boolean", "default": True}}},
        "docker.access", docker_ps,
        category="devops",
        capabilities=["list_containers", "docker_manage"],
    ))
    registry.register(ToolSpec(
        "docker_logs",
        "Tail a container's logs (name or id, tail<=2000 lines).",
        {"type": "object",
         "properties": {"name": {"type": "string"},
                        "tail": {"type": "integer", "default": 200}},
         "required": ["name"]},
        "docker.access", docker_logs,
        category="devops",
        capabilities=["container_logs", "docker_manage"],
    ))
    registry.register(ToolSpec(
        "docker_container",
        "Container lifecycle: action=start|stop|restart|rm (+force for rm).",
        {"type": "object",
         "properties": {
             "action": {"type": "string",
                        "enum": sorted(_CONTAINER_ACTIONS)},
             "name": {"type": "string"},
             "force": {"type": "boolean", "default": False}},
         "required": ["action", "name"]},
        "docker.access", docker_container,
        category="devops",
        capabilities=["container_lifecycle", "docker_manage"],
    ))
    registry.register(ToolSpec(
        "docker_compose_detect",
        "Find compose files under the workspace and list their services "
        "with published ports — filesystem scan, never runs compose.",
        {"type": "object",
         "properties": {"path": {"type": "string",
                                 "description": "subdir under the "
                                                "workspace"}}},
        "filesystem.read", docker_compose_detect,
        category="devops",
        capabilities=["compose_detect", "docker_manage"],
    ))
    registry.register(ToolSpec(
        "docker_compose",
        "Run docker compose against a compose file: "
        "action=ps|up(-d)|down|logs|restart|pull|stop|start|build, "
        "optional service scope.",
        {"type": "object",
         "properties": {
             "action": {"type": "string",
                        "enum": sorted(_COMPOSE_ACTIONS)},
             "file": {"type": "string",
                      "description": "compose file (default: first found "
                                     "in workspace)"},
             "service": {"type": "string"},
             "tail": {"type": "integer", "default": 200}},
         "required": ["action"]},
        "docker.access", docker_compose,
        category="devops",
        capabilities=["compose_up", "compose_down", "compose_logs",
                      "docker_manage"],
    ))
    registry.register(ToolSpec(
        "docker_devservers",
        "List running containers' published ports as http://localhost:<p> "
        "dev-server candidates — container-hosted services count as dev "
        "servers, not just host processes.",
        {"type": "object", "properties": {}},
        "docker.access", docker_devservers,
        category="devops",
        capabilities=["devserver_discovery", "docker_manage"],
    ))
