from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class ProviderResponse:
    message: dict[str, Any]
    raw: dict[str, Any]
