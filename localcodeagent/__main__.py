from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import load_config
from .desktop import run_desktop, smoke_test_desktop_backend
from .server import serve


def _frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _application_dir() -> Path:
    if _frozen():
        return Path(sys.executable).resolve().parent
    return Path.cwd().resolve()


def _web_root() -> Path:
    bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return bundle_root / "web"


def main() -> None:
    parser = argparse.ArgumentParser(description="Chat Nexus local-first AI coding agent")
    parser.add_argument("--workspace", default="", help="Project directory the agent may access. Bundled builds default to the executable folder.")
    parser.add_argument("--config", default="", help="Path to agent config JSON. Bundled builds default to config.json beside ChatNexus.exe.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--desktop", action="store_true", help="Run in the native desktop application window.")
    parser.add_argument("--server", action="store_true", help="Run only the loopback web server for development/debugging.")
    parser.add_argument("--smoke-test", action="store_true", help="Smoke-test the packaged backend/UI and exit.")
    args = parser.parse_args()

    app_dir = _application_dir()
    workspace = Path(args.workspace).expanduser().resolve() if args.workspace else app_dir
    workspace.mkdir(parents=True, exist_ok=True)
    config_path = Path(args.config).expanduser().resolve() if args.config else (app_dir / "config.json")
    config = load_config(config_path if config_path.exists() else None)
    runtime_root = config_path.parent
    web_root = _web_root()

    if args.smoke_test:
        result = smoke_test_desktop_backend(config, workspace, web_root, runtime_root, config_path)
        print(json.dumps(result, indent=2))
        raise SystemExit(0 if result.get("ok") else 1)

    if (_frozen() and not args.server) or args.desktop:
        run_desktop(config, workspace, web_root, runtime_root, config_path)
        return

    serve(
        config,
        workspace,
        args.host,
        args.port,
        web_root,
        runtime_root,
        config_path=config_path,
    )


if __name__ == "__main__":
    main()
