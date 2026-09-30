from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

from .config import AgentConfig
from .server import VERSION, create_server, stop_state


def _probe(url: str, timeout: float = 2.0) -> tuple[int, str]:
    with urlopen(url, timeout=timeout) as response:
        return int(response.status), response.read().decode("utf-8", errors="replace")


def smoke_test_desktop_backend(
    config: AgentConfig,
    workspace: Path,
    web_root: Path,
    runtime_root: Path,
    config_path: Path,
) -> dict[str, Any]:
    """Start the bundled backend on an ephemeral loopback port and smoke-test it."""
    server, state = create_server(
        config,
        workspace,
        "127.0.0.1",
        0,
        web_root,
        runtime_root,
        config_path=config_path,
    )
    port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, name="chat-nexus-smoke-server", daemon=True)
    thread.start()
    result: dict[str, Any] = {"ok": False, "port": port, "checks": {}}
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                status, body = _probe(f"http://127.0.0.1:{port}/api/status")
                if status == 200:
                    payload = json.loads(body)
                    result["checks"]["status"] = bool(payload.get("version"))
                    break
            except (URLError, TimeoutError, OSError, json.JSONDecodeError):
                time.sleep(0.1)
        else:
            result["error"] = "Desktop backend did not become healthy."
            return result

        for name, path, marker in (
            ("main", "/", "Chat Nexus"),
            ("image", "/image.html", "Chat Nexus"),
            ("research", "/research.html", "Chat Nexus"),
        ):
            try:
                status, body = _probe(f"http://127.0.0.1:{port}{path}")
                result["checks"][name] = status == 200 and marker in body
            except Exception as exc:
                result["checks"][name] = False
                result.setdefault("details", {})[name] = f"{type(exc).__name__}: {exc}"
        result["ok"] = all(result["checks"].values())
        return result
    finally:
        server.shutdown()
        server.server_close()
        stop_state(state)
        thread.join(timeout=3)


def run_desktop(
    config: AgentConfig,
    workspace: Path,
    web_root: Path,
    runtime_root: Path,
    config_path: Path,
) -> None:
    """Run Chat Nexus as a real native desktop app window.

    The loopback HTTP backend is private implementation plumbing. Users interact
    only with the native WebView2 application window.
    """
    try:
        import webview
    except ImportError as exc:
        raise RuntimeError(
            "The desktop host is not installed. Install the desktop extra with "
            "'pip install pywebview' or use a packaged Chat Nexus desktop build."
        ) from exc

    server, state = create_server(
        config,
        workspace,
        "127.0.0.1",
        0,
        web_root,
        runtime_root,
        config_path=config_path,
    )
    port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, name="chat-nexus-desktop-backend", daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}/"

    try:
        webview.create_window(
            f"Chat Nexus {VERSION}",
            url=url,
            width=1440,
            height=900,
            min_size=(980, 640),
            background_color="#07101f",
            text_select=True,
        )
        # Edge/WebView2 is the preferred renderer on Windows. pywebview will use
        # the native platform engine on other supported systems.
        webview.start(gui="edgechromium" if __import__("os").name == "nt" else None)
    finally:
        server.shutdown()
        server.server_close()
        stop_state(state)
        thread.join(timeout=5)
