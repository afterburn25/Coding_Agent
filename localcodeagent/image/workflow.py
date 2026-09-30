from __future__ import annotations

import copy
import json
import re
import uuid
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
    def validate_api(workflow: dict[str, Any]) -> dict[str, Any]:
        """Validate the subset of ComfyUI API workflow structure we depend on.

        ComfyUI's normal UI/workflow export contains a top-level ``nodes`` list,
        while the prompt API expects a mapping of node ids to objects containing
        ``class_type`` and ``inputs``. This catches the common wrong-export case
        before a multi-gigabyte model is loaded.
        """
        errors: list[str] = []
        if not isinstance(workflow, dict):
            return {"valid": False, "format": "unknown", "node_count": 0, "errors": ["workflow must be a JSON object"], "unresolved_tokens": []}
        if isinstance(workflow.get("nodes"), list):
            errors.append("workflow is ComfyUI UI format; export/save it in API format for /prompt")
            fmt = "ui"
        else:
            fmt = "api"

        nodes: list[tuple[str, dict[str, Any]]] = []
        for node_id, node in workflow.items():
            if not isinstance(node, dict):
                continue
            if "class_type" in node or "inputs" in node:
                nodes.append((str(node_id), node))
                if not isinstance(node.get("class_type"), str) or not str(node.get("class_type", "")).strip():
                    errors.append(f"node {node_id} is missing class_type")
                if not isinstance(node.get("inputs"), dict):
                    errors.append(f"node {node_id} is missing inputs")
        if not nodes and fmt == "api":
            errors.append("workflow contains no ComfyUI API nodes")

        unresolved: set[str] = set()
        def scan(value: Any) -> None:
            if isinstance(value, dict):
                for item in value.values():
                    scan(item)
            elif isinstance(value, list):
                for item in value:
                    scan(item)
            elif isinstance(value, str):
                unresolved.update(TOKEN.findall(value))
        scan(workflow)
        return {
            "valid": not errors,
            "format": fmt,
            "node_count": len(nodes),
            "errors": errors,
            "unresolved_tokens": sorted(unresolved),
            "class_types": sorted({str(node.get("class_type")) for _, node in nodes if node.get("class_type")}),
        }

    def save_api(self, name: str, workflow: dict[str, Any]) -> dict[str, Any]:
        """Validate and atomically save a ComfyUI prompt-API workflow.

        ``name`` is always resolved below the configured workflow root. Normal
        ComfyUI UI exports are deliberately rejected because the backend /prompt
        endpoint needs the API graph produced by ComfyUI's Export (API) action.
        """
        if not isinstance(workflow, dict):
            raise ValueError("workflow must be a JSON object")
        validation = self.validate_api(workflow)
        if not validation.get("valid"):
            if validation.get("format") == "ui":
                raise ValueError(
                    "This is a normal ComfyUI workflow export, not an API workflow. "
                    "Open it in ComfyUI and use Workflow → Export (API), then import that JSON file."
                )
            details = "; ".join(str(x) for x in validation.get("errors", [])) or "invalid API workflow"
            raise ValueError(details)

        path = (self.root / str(name)).resolve()
        if not path.is_relative_to(self.root):
            raise PermissionError("workflow path must stay inside the image workflow directory")
        if path.suffix.lower() != ".json":
            raise ValueError("workflow path must end in .json")
        payload = json.dumps(workflow, indent=2)
        if len(payload.encode("utf-8")) > 10 * 1024 * 1024:
            raise ValueError("workflow JSON exceeds the 10 MB import limit")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(path)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        result = dict(validation)
        result.update({"name": str(path.relative_to(self.root)).replace("\\", "/"), "exists": True})
        return result

    def inspect(self, name: str) -> dict[str, Any]:
        try:
            workflow = self.load(name)
        except Exception as exc:
            return {"name": name, "exists": False, "valid": False, "format": "unknown", "node_count": 0, "errors": [f"{type(exc).__name__}: {exc}"], "unresolved_tokens": []}
        result = self.validate_api(workflow)
        result.update({"name": name, "exists": True})
        return result

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
