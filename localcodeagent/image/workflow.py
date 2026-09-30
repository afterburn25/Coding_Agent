from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any


TOKEN = re.compile(r"\$\{([A-Za-z0-9_.-]+)\}")


class WorkflowManager:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def list(self) -> list[str]:
        return [str(p.relative_to(self.root)).replace("\\", "/") for p in sorted(self.root.rglob("*.json"))]

    def load(self, name: str) -> dict[str, Any]:
        path = (self.root / name).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            raise FileNotFoundError(name)
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("ComfyUI API workflow must be a JSON object")
        return data

    @staticmethod
    def render(workflow: dict[str, Any], variables: dict[str, Any]) -> dict[str, Any]:
        def convert(value: Any) -> Any:
            if isinstance(value, dict):
                return {k: convert(v) for k, v in value.items()}
            if isinstance(value, list):
                return [convert(v) for v in value]
            if isinstance(value, str):
                full = TOKEN.fullmatch(value)
                if full and full.group(1) in variables:
                    return variables[full.group(1)]
                return TOKEN.sub(lambda m: str(variables.get(m.group(1), m.group(0))), value)
            return value
        return convert(copy.deepcopy(workflow))
