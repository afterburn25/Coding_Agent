from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(slots=True)
class ProviderResponse:
    message: dict[str, Any]
    raw: dict[str, Any]


class ModelProvider(Protocol):
    def complete(self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> ProviderResponse:
        ...
