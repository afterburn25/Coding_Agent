"""Local-backend transport diagnostics and failure classification.

Nexus Core talks to local services (llama.cpp, ComfyUI, voice, tools) over
loopback HTTP. When a backend dies or a socket is reset, the raw exception
(`ConnectionResetError: [WinError 10054]`, `http.client.IncompleteRead`, …)
tells the user nothing. This module classifies transport failures, attaches
request/backend context, and produces both a friendly user message and a
structured diagnostic payload — never secrets or prompt content.
"""
from __future__ import annotations

import collections
import http.client
import socket
import threading
import time
import urllib.error
import urllib.parse
from pathlib import Path
from typing import Any

# Ordered categories — first match wins.
_KIND_LABELS = {
    "connection_reset": "connection reset by the backend",
    "connection_aborted": "connection aborted",
    "connection_refused": "connection refused — nothing is listening",
    "broken_pipe": "broken pipe",
    "incomplete_read": "backend closed the stream early",
    "timeout": "request timed out",
    "unreachable": "endpoint unreachable",
}

_SUBSYSTEM_LABELS = {
    "llm": "local language-model service",
    "comfyui": "ComfyUI image backend",
    "voice": "voice service",
    "tool": "tool server",
    "mcp": "MCP server",
    "local": "local service",
}


def _unwrap(exc: BaseException) -> BaseException:
    """URLError wraps the real OSError in .reason — unwrap to classify."""
    seen = 0
    while (isinstance(exc, urllib.error.URLError)
           and isinstance(exc.reason, BaseException) and seen < 4):
        exc = exc.reason  # type: ignore[assignment]
        seen += 1
    return exc


def classify_transport_error(exc: BaseException) -> str:
    """Map any transport exception to a stable kind string."""
    inner = _unwrap(exc)
    if isinstance(inner, socket.timeout) or isinstance(inner, TimeoutError):
        return "timeout"
    if isinstance(inner, http.client.IncompleteRead):
        return "incomplete_read"
    if isinstance(inner, ConnectionResetError):
        return "connection_reset"
    if isinstance(inner, ConnectionRefusedError):
        return "connection_refused"
    if isinstance(inner, ConnectionAbortedError):
        return "connection_aborted"
    if isinstance(inner, BrokenPipeError):
        return "broken_pipe"
    if isinstance(inner, (ConnectionError, http.client.HTTPException, OSError)):
        # Numeric Windows fallbacks: 10054 reset, 10061 refused, 10053 aborted.
        winerror = getattr(inner, "winerror", None) or getattr(inner, "errno", None)
        return {10054: "connection_reset", 10061: "connection_refused",
                10053: "connection_aborted", 32: "broken_pipe",
                10060: "timeout"}.get(winerror, "unreachable")
    return "unreachable"


def redact_url(url: str) -> str:
    """Strip credentials and query strings — host/port/path only."""
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname or ""
        if parts.port:
            host += f":{parts.port}"
        return urllib.parse.urlunsplit((parts.scheme, host, parts.path, "", ""))
    except Exception:
        return ""


def is_transport_failure(exc: BaseException) -> bool:
    """True for any socket/HTTP transport failure worth classifying."""
    inner = _unwrap(exc)
    return isinstance(inner, (
        ConnectionError, OSError, socket.timeout, TimeoutError,
        http.client.HTTPException, urllib.error.URLError,
    )) and not isinstance(inner, urllib.error.HTTPError)


class BackendConnectionError(RuntimeError):
    """A local-backend request failed at the transport layer.

    Carries enough structured context for logs, the diagnostics surface and
    the recovery layer — and a `friendly` message safe to show the user.
    """

    def __init__(
        self,
        exc: BaseException,
        *,
        subsystem: str = "llm",
        url: str = "",
        method: str = "POST",
        model_id: str = "",
        request_id: str = "",
        streaming: bool = False,
        phase: str = "connect",          # connect | read | stream
        chunks_received: int = 0,
        partial_chars: int = 0,
        elapsed_s: float = 0.0,
        attempt: int = 0,
        process: dict[str, Any] | None = None,
    ) -> None:
        self.original = exc
        self.subsystem = subsystem
        self.url = url
        self.method = method
        self.model_id = model_id
        self.request_id = request_id
        self.streaming = streaming
        self.phase = phase
        self.chunks_received = chunks_received
        self.partial_chars = partial_chars
        self.elapsed_s = elapsed_s
        self.attempt = attempt
        self.process = dict(process or {})
        self.kind = classify_transport_error(exc)
        self.host, self.port = self._split_host(url)
        # Filled in by the orchestrator's recovery layer at catch time.
        self.backend: dict[str, Any] = {}
        super().__init__(self._technical())

    @staticmethod
    def _split_host(url: str) -> tuple[str, int]:
        try:
            parts = urllib.parse.urlsplit(url)
            return parts.hostname or "", int(parts.port or (443 if parts.scheme == "https" else 80))
        except Exception:
            return "", 0

    @property
    def delivered_output(self) -> bool:
        """True when the user already received streamed content — retrying
        would duplicate it, so recovery must not auto-retry."""
        return self.chunks_received > 0 or self.partial_chars > 0

    def _technical(self) -> str:
        where = f"{self.host}:{self.port}" if self.port else (self.url or self.subsystem)
        bits = [
            f"{_KIND_LABELS[self.kind]} at {where}",
            f"phase={self.phase}",
        ]
        if self.model_id:
            bits.append(f"model={self.model_id}")
        if self.streaming:
            bits.append(f"chunks={self.chunks_received}")
        bits.append(f"elapsed={self.elapsed_s:.2f}s")
        if self.attempt:
            bits.append(f"attempt={self.attempt}")
        return f"{self.original!r} ({', '.join(bits)})"

    @property
    def friendly(self) -> str:
        service = _SUBSYSTEM_LABELS.get(self.subsystem, "local service")
        where = f"{self.host}:{self.port}" if self.port else ""
        if self.delivered_output:
            return (
                f"The {service} disconnected mid-response"
                f"{f' ({where})' if where else ''} — the partial answer was kept. "
                "Send the prompt again to retry."
            )
        if self.kind == "connection_refused":
            return (
                f"The {service} is not running or not listening"
                f"{f' on {where}' if where else ''}. Nexus Core is checking "
                "the backend and will retry once."
            )
        if self.kind == "timeout":
            return (
                f"The {service} did not answer in time. "
                "It may still be loading the model — Nexus Core will retry once."
            )
        return (
            f"The {service} unexpectedly disconnected"
            f"{f' at {where}' if where else ''}. "
            "Nexus Core is checking the backend process and will retry once."
        )

    def diagnostic(self) -> dict[str, Any]:
        """Structured, secret-free diagnostic payload."""
        return {
            "subsystem": self.subsystem,
            "kind": self.kind,
            "url": redact_url(self.url),
            "host": self.host,
            "port": self.port,
            "method": self.method,
            "model_id": self.model_id,
            "request_id": self.request_id,
            "streaming": self.streaming,
            "phase": self.phase,
            "chunks_received": self.chunks_received,
            "partial_chars": self.partial_chars,
            "elapsed_s": round(self.elapsed_s, 3),
            "attempt": self.attempt,
            "exception": f"{type(self.original).__name__}: {self.original}",
            "winerror": getattr(_unwrap(self.original), "winerror", None),
            "backend": dict(self.backend),
        }

    def diagnostic_text(self) -> str:
        """One-line technical summary for logs/activity timelines."""
        pid = self.process.get("pid") or (self.backend or {}).get("pid")
        extra = f" pid={pid}" if pid else ""
        code = (self.backend or {}).get("exit_code")
        crash = f" exit_code={code}" if code is not None else ""
        req = f" request {self.request_id}" if self.request_id else ""
        return (
            f"[{self.subsystem}]{req}{extra} {self.kind} at "
            f"{self.host}:{self.port} phase={self.phase} "
            f"streaming={self.streaming} chunks={self.chunks_received}"
            f"{crash} — {type(self.original).__name__}: {self.original}"
        )


# Bounded ring of recent backend failures — surfaced by /api/diagnostics.
# Entries carry no prompts and no secrets.
_FAILURES: collections.deque = collections.deque(maxlen=25)

# Optional durable crash history — a bounded JSONL next to the other runtime
# state so crash patterns (e.g. "qwen3 30B resets at large context") survive
# restarts and can feed the Digital Twin / tuner. Configured by the server at
# boot; when unset the ring above is still recorded.
_HISTORY = None
_HISTORY_LOCK = threading.Lock()


def configure_history(path: Path | str | None) -> None:
    """Point crash-history persistence at a JSONL file (called once at boot)."""
    global _HISTORY
    if not path:
        return
    from .autonomy.state import JsonlLog  # late import — state.py is heavier
    _HISTORY = JsonlLog(Path(path), max_bytes=1024 * 1024, keep_tail=512 * 1024)


def _persist_failure(entry: dict) -> None:
    log = _HISTORY
    if log is None:
        return
    try:
        with _HISTORY_LOCK:
            log.append(dict(entry))
    except Exception:
        pass  # history is best-effort; never break the caller's error path


def record_failure(exc: BaseException) -> dict:
    """Snapshot a transport failure for the diagnostics report; returns the
    mutable entry so the recovery layer can annotate the outcome."""
    diag = getattr(exc, "diagnostic", None)
    entry = diag() if callable(diag) else {
        "subsystem": "unknown", "kind": "unclassified",
        "detail": f"{type(exc).__name__}: {exc}"[:400],
    }
    entry["time"] = time.time()
    entry["recovery"] = "pending"
    _FAILURES.append(entry)
    _persist_failure(entry)
    return entry


def annotate_recovery(entry: dict | None, outcome: str) -> None:
    """Record the recovery outcome on a failure entry — updates the in-memory
    row and appends the resolution to the durable history."""
    if not isinstance(entry, dict):
        return
    entry["recovery"] = outcome
    log = _HISTORY
    if log is None:
        return
    try:
        with _HISTORY_LOCK:
            log.append({
                "recovery_for": entry.get("request_id") or "",
                "subsystem": entry.get("subsystem", ""),
                "kind": entry.get("kind", ""),
                "recovery": outcome,
                "time": time.time(),
            })
    except Exception:
        pass


def recent_failures() -> list[dict]:
    return list(_FAILURES)


def crash_history(limit: int = 50) -> list[dict]:
    """Read back the persisted crash/recovery history (newest last)."""
    log = _HISTORY
    if log is None:
        return []
    try:
        return log.tail(limit)
    except Exception:
        return []
