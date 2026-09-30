from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


class ImageBackend(ABC):
    @abstractmethod
    def health(self) -> tuple[bool, str]: ...

    @abstractmethod
    def inspect(self) -> dict[str, Any]: ...

    @abstractmethod
    def submit(self, workflow: dict[str, Any]) -> str: ...

    @abstractmethod
    def status(self, backend_job_id: str) -> dict[str, Any]: ...

    @abstractmethod
    def fetch_outputs(self, backend_job_id: str, destination: Path) -> list[Path]: ...

    @abstractmethod
    def cancel(self, backend_job_id: str) -> None: ...
