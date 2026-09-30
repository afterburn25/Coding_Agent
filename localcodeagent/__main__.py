from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import load_config
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
    parser.add_argument("--open-browser", action="store_true", help="Open the Chat Nexus UI in the default browser after startup.")
    parser.add_argument("--no-browser", action="store_true", help="Do not open a browser automatically.")
    args = parser.parse_args()

    app_dir = _application_dir()
    workspace = Path(args.workspace).expanduser().resolve() if args.workspace else app_dir
    workspace.mkdir(parents=True, exist_ok=True)
    config_path = Path(args.config).expanduser().resolve() if args.config else (app_dir / "config.json")
    config = load_config(config_path if config_path.exists() else None)
    runtime_root = config_path.parent
    web_root = _web_root()

    # Double-clicked Windows executable builds should visibly open the product.
    # Source/CLI runs remain non-intrusive unless --open-browser is requested.
    open_browser = bool(args.open_browser or (_frozen() and not args.no_browser))
    if args.no_browser:
        open_browser = False

    serve(
        config,
        workspace,
        args.host,
        args.port,
        web_root,
        runtime_root,
        config_path=config_path,
        open_browser=open_browser,
    )


if __name__ == "__main__":
    main()
