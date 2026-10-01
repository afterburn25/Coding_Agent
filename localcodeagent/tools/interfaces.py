from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

# Standardized tool-family interfaces.
#
# The agent never talks to a concrete tool implementation directly; it talks to
# the Tool Registry, which holds ToolSpec manifests. These protocols describe
# the expected surface of each tool family so providers (native, manifest, or
# MCP) stay interchangeable. They are structural (duck-typed) contracts — a
# provider does not need to inherit from them.


@runtime_checkable
class ITool(Protocol):
    """Base contract: every registered tool exposes a manifest and executes a
    structured argument dict, returning a structured result or a bounded text
    result."""

    def manifest(self) -> dict[str, Any]: ...
    def execute(self, arguments: dict[str, Any]) -> Any: ...


@runtime_checkable
class IExecutableTool(ITool, Protocol):
    """A tool backed by a local executable or service process."""

    def command(self, arguments: dict[str, Any]) -> list[str]: ...
    def health(self) -> dict[str, Any]: ...


@runtime_checkable
class IModelBackend(Protocol):
    """A local or remote inference backend (llama.cpp, external endpoint, ...)."""

    def ensure_ready(self) -> str: ...
    def stop(self) -> Any: ...
    def status(self) -> dict[str, Any]: ...


@runtime_checkable
class IImageBackend(Protocol):
    """Image generation/editing backend (ComfyUI today, native/future later)."""

    def health(self) -> tuple[bool, str]: ...
    def submit(self, payload: dict[str, Any]) -> str: ...
    def status(self, backend_job_id: str) -> dict[str, Any]: ...
    def cancel(self, backend_job_id: str) -> bool: ...


@runtime_checkable
class IBrowserBackend(Protocol):
    """Browser automation backend (Playwright/Chromium)."""

    def run(self, url: str, actions: list[dict[str, Any]], screenshot: bool = False) -> dict[str, Any]: ...
    def status(self) -> dict[str, Any]: ...


@runtime_checkable
class IResearchProvider(Protocol):
    """Evidence provider: search/fetch with provenance."""

    def search(self, query: str, **kwargs: Any) -> list[dict[str, Any]]: ...
    def fetch(self, url: str, **kwargs: Any) -> dict[str, Any]: ...


@runtime_checkable
class IMediaTool(IExecutableTool, Protocol):
    """Media processing provider (FFmpeg family)."""


@runtime_checkable
class IDataTool(Protocol):
    """Local analytical/persistent data engine (DuckDB, SQLite)."""

    def query(self, sql: str, **kwargs: Any) -> dict[str, Any]: ...


@runtime_checkable
class IDocumentTool(Protocol):
    """Document extraction/conversion provider (OCR, Pandoc, PDF)."""

    def extract_text(self, path: str, **kwargs: Any) -> dict[str, Any]: ...


@runtime_checkable
class ISandboxProvider(Protocol):
    """Isolated execution environment (Docker, restricted process)."""

    def run(self, command: list[str], *, mounts: list[str] | None = None, timeout: int = 600) -> dict[str, Any]: ...
    def available(self) -> bool: ...


@runtime_checkable
class IVersionControlProvider(Protocol):
    """Local/remote version control operations (Git/GitHub)."""

    def status(self) -> dict[str, Any]: ...
