"""Minimal RFC 6455 WebSocket client for ComfyUI live progress.

Dependency-free: the client only needs to complete the upgrade handshake and
decode inbound frames (server frames are never masked); outbound frames are
masked per spec. Used by ``ComfyUIProgressListener`` to turn ComfyUI's ``/ws``
event feed into per-prompt progress fractions so image jobs can report real
node/step progress instead of coarse poll estimates.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import threading
import urllib.parse
from typing import Any


class WebSocketError(Exception):
    pass


class WSClient:
    """Read-mostly WebSocket client: handshake + frame decode + masked send."""

    _GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self, url: str, *, timeout: float = 10.0) -> None:
        self.url = url
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._buf = b""

    def connect(self) -> "WSClient":
        parts = urllib.parse.urlparse(self.url)
        host = parts.hostname or "127.0.0.1"
        secure = parts.scheme == "wss"
        port = parts.port or (443 if secure else 80)
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        sock = socket.create_connection((host, port), timeout=self.timeout)
        if secure:
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.sendall(request.encode("utf-8"))
        response = self._read_http_head(sock)
        status_line = response.split(b"\r\n", 1)[0]
        if b" 101" not in status_line:
            sock.close()
            raise WebSocketError(f"websocket handshake failed: {status_line[:120]!r}")
        accept = base64.b64encode(
            hashlib.sha1((key + self._GUID).encode("ascii")).digest()
        ).decode("ascii")
        if accept.encode("ascii") not in response:
            sock.close()
            raise WebSocketError("websocket handshake failed: bad Sec-WebSocket-Accept")
        self._sock = sock
        return self

    def _read_http_head(self, sock: socket.socket) -> bytes:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                raise WebSocketError("connection closed during handshake")
            data += chunk
        head, _, self._buf = data.partition(b"\r\n\r\n")
        return head

    def _recv_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise WebSocketError("connection closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _send_frame(self, opcode: int, data: bytes = b"") -> None:
        """Send a masked frame — required of all client-to-server frames."""
        if self._sock is None:
            raise WebSocketError("not connected")
        mask = os.urandom(4)
        header = bytes([0x80 | opcode])
        n = len(data)
        if n < 126:
            header += bytes([0x80 | n])
        elif n < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self._sock.sendall(header + mask + masked)

    def recv_message(self) -> bytes:
        """Return the payload of the next complete data message.

        Handles continuation frames, ping→pong, and raises on close.
        """
        payload = b""
        while True:
            b1, b2 = self._recv_exact(2)
            fin = bool(b1 & 0x80)
            opcode = b1 & 0x0F
            masked = bool(b2 & 0x80)
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if masked else b""
            data = self._recv_exact(length)
            if masked:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if opcode == 0x8:
                raise WebSocketError("server closed the connection")
            if opcode == 0x9:
                self._send_frame(0xA, data)
                continue
            if opcode == 0xA:
                continue
            payload += data
            if fin:
                return payload

    def close(self) -> None:
        try:
            if self._sock is not None:
                self._send_frame(0x8)
                self._sock.close()
        except OSError:
            pass
        self._sock = None


class ComfyUIProgressListener:
    """Background listener for ComfyUI ``/ws`` events.

    Tracks per-prompt progress fractions and the currently executing node so
    callers can report real generation progress between status polls.
    """

    def __init__(self, endpoint: str, *, timeout: float = 10.0) -> None:
        parts = urllib.parse.urlparse(endpoint if "://" in endpoint else f"http://{endpoint}")
        scheme = "wss" if parts.scheme == "https" else "ws"
        self.url = f"{scheme}://{parts.netloc}/ws"
        self.timeout = timeout
        self._progress: dict[str, float] = {}
        self._nodes: dict[str, Any] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ws: WSClient | None = None
        self.connected = False

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        failures = 0
        try:
            while not self._stop.is_set():
                try:
                    self._ws = WSClient(self.url, timeout=self.timeout).connect()
                    self.connected = True
                    failures = 0
                    while not self._stop.is_set():
                        try:
                            payload = self._ws.recv_message()
                        except (WebSocketError, OSError):
                            break
                        try:
                            message = json.loads(payload)
                        except ValueError:
                            continue
                        if isinstance(message, dict):
                            self._handle(message)
                except Exception:
                    pass
                finally:
                    self.connected = False
                    if self._ws is not None:
                        self._ws.close()
                    self._ws = None
                if self._stop.is_set():
                    break
                # ComfyUI may restart between jobs — reconnect with capped backoff.
                failures += 1
                delay = min(30.0, 2.0 * failures)
                self._stop.wait(delay)
        finally:
            self.connected = False

    def _handle(self, message: dict[str, Any]) -> None:
        mtype = message.get("type")
        data = message.get("data") if isinstance(message.get("data"), dict) else {}
        prompt_id = str(data.get("prompt_id") or "")
        if not prompt_id:
            return
        with self._lock:
            if mtype == "progress":
                value = float(data.get("value") or 0)
                maximum = float(data.get("max") or 0)
                if maximum > 0:
                    self._progress[prompt_id] = min(1.0, max(0.0, value / maximum))
            elif mtype == "executing":
                self._nodes[prompt_id] = data.get("node")
            elif mtype == "executed":
                self._progress[prompt_id] = 1.0

    def progress_for(self, prompt_id: str) -> tuple[float | None, Any]:
        """Return (fraction, current_node) for a prompt id, or (None, None)."""
        with self._lock:
            return self._progress.get(prompt_id), self._nodes.get(prompt_id)

    def stop(self) -> None:
        self._stop.set()
        ws = self._ws
        if ws is not None:
            ws.close()
