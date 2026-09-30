from __future__ import annotations

import argparse
from pathlib import Path

from .config import load_config
from .server import serve


def main() -> None:
    parser = argparse.ArgumentParser(description="Chat Nexus backend / development server")
    parser.add_argument("--workspace", default=".", help="Project directory the agent may access")
    parser.add_argument("--config", default="config.json", help="Path to Chat Nexus config JSON")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--server",
        action="store_true",
        help="Accepted for the native desktop launcher; the Python entry point is always backend/server mode.",
    )
    args = parser.parse_args()

    workspace = Path(args.workspace).expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    config_path = Path(args.config).expanduser().resolve()
    config = load_config(config_path if config_path.exists() else None)
    runtime_root = config_path.parent
    web_root = Path(__file__).resolve().parent.parent / "web"

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
