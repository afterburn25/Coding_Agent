from __future__ import annotations

import http.client
import json
import mimetypes
import secrets
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from .backend import ImageBackend
from ..netdiag import BackendConnectionError


def _conn_error(exc: BaseException, url: str, *, method: str = "GET") -> BackendConnectionError:
    return BackendConnectionError(
        exc, subsystem="comfyui", url=url, method=method,
        request_id=secrets.token_hex(6),
    )


class ComfyUIBackend(ImageBackend):
    """Small dependency-free adapter for a local ComfyUI server API.

    ComfyUI can execute any operation a configured API workflow exists for
    — its capability set is the full operation vocabulary; per-model
    workflow availability is enforced upstream by the router/manager."""

    name = "comfyui"
    display_name = "ComfyUI"

    def __init__(self, endpoint: str = "http://127.0.0.1:8188", *, timeout: float = 10.0) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout
        self.client_id = str(uuid.uuid4())

    def capabilities(self) -> set[str]:
        return {"text_to_image", "edit_image", "inpaint", "outpaint",
                "remove_background", "upscale", "variation"}

    def _json(self, path: str, *, method: str = "GET", payload: dict | None = None) -> Any:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"} if data is not None else {},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                return json.loads(raw.decode("utf-8")) if raw else {}
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, OSError, http.client.HTTPException, TimeoutError) as exc:
            raise _conn_error(exc, self.endpoint + path, method=method) from exc

    def health(self) -> tuple[bool, str]:
        try:
            req = urllib.request.Request(self.endpoint + "/system_stats", method="GET")
            with urllib.request.urlopen(req, timeout=min(self.timeout, 0.75)) as resp:
                raw = resp.read(64_000)
            info = json.loads(raw.decode("utf-8")) if raw else {}
            return True, json.dumps(info)[:500]
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            return False, str(exc)

    def inspect(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, path in (("system_stats", "/system_stats"), ("object_info", "/object_info")):
            try:
                result[name] = self._json(path)
            except Exception as exc:
                result[name] = {"error": str(exc)}
        return result


    def upload_image(self, path: Path, *, subfolder: str = "local-code-agent", overwrite: bool = True) -> dict[str, Any]:
        """Upload a local input image to ComfyUI using its multipart image endpoint."""
        path=Path(path).resolve()
        if not path.is_file():
            raise FileNotFoundError(path)
        boundary="----LocalCodeAgent"+uuid.uuid4().hex
        mime=mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        parts=[]
        def field(name: str, value: str) -> None:
            parts.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n").encode("utf-8"))
        field("type","input")
        field("overwrite","true" if overwrite else "false")
        field("subfolder",subfolder)
        head=(f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{path.name}\"\r\nContent-Type: {mime}\r\n\r\n").encode("utf-8")
        tail=(f"\r\n--{boundary}--\r\n").encode("utf-8")
        body=b"".join(parts)+head+path.read_bytes()+tail
        last=None
        for endpoint_path in ("/upload/image","/api/upload/image"):
            req=urllib.request.Request(self.endpoint+endpoint_path,data=body,method="POST",headers={"Content-Type":f"multipart/form-data; boundary={boundary}"})
            try:
                with urllib.request.urlopen(req,timeout=max(self.timeout,30.0)) as resp:
                    raw=resp.read()
                result=json.loads(raw.decode("utf-8")) if raw else {}
                if result.get("name"):
                    return result
                last=RuntimeError(f"Unexpected ComfyUI upload response: {result}")
            except urllib.error.HTTPError as exc:
                last=exc
                if exc.code not in {404,405}:
                    raise
            except (urllib.error.URLError, OSError, http.client.HTTPException, TimeoutError) as exc:
                raise _conn_error(exc, self.endpoint + endpoint_path, method="POST") from exc
        raise RuntimeError(f"ComfyUI image upload failed: {last}")

    def submit(self, workflow: dict[str, Any]) -> str:
        response = self._json("/prompt", method="POST", payload={"prompt": workflow, "client_id": self.client_id})
        prompt_id = str(response.get("prompt_id", ""))
        if not prompt_id:
            raise RuntimeError(f"ComfyUI did not return prompt_id: {response}")
        return prompt_id

    def status(self, backend_job_id: str) -> dict[str, Any]:
        history = self._json(f"/history/{urllib.parse.quote(backend_job_id)}")
        entry = history.get(backend_job_id) if isinstance(history, dict) else None
        if not entry:
            return {"state": "running", "progress": 0.0}
        status = entry.get("status", {}) if isinstance(entry, dict) else {}
        # ComfyUI records failures as an execution_error message with
        # completed=False — without this check a crashed prompt polls as
        # "running" forever and the UI hangs on a stuck progress bar.
        messages = status.get("messages") or []
        error = next(
            (m[1] for m in messages
             if isinstance(m, (list, tuple)) and len(m) > 1
             and m[0] == "execution_error" and isinstance(m[1], dict)),
            None)
        if error is not None:
            detail = error.get("exception_message") or error.get("exception_type") or "execution error"
            node = error.get("node_type") or error.get("node_id") or ""
            return {
                "state": "failed",
                "progress": 0.0,
                "error": f"{detail} (node: {node})" if node else str(detail),
                "history": entry,
            }
        complete = bool(status.get("completed", False)) or bool(entry.get("outputs"))
        return {
            "state": "finished" if complete else "running",
            "progress": 1.0 if complete else 0.5,
            "history": entry,
        }

    def _download_view(self, item: dict[str, Any], destination: Path) -> Path:
        params = urllib.parse.urlencode({
            "filename": item.get("filename", ""),
            "subfolder": item.get("subfolder", ""),
            "type": item.get("type", "output"),
        })
        req = urllib.request.Request(self.endpoint + "/view?" + params, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=max(self.timeout, 30.0)) as resp:
                data = resp.read()
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, OSError, http.client.HTTPException, TimeoutError) as exc:
            raise _conn_error(exc, self.endpoint + "/view") from exc
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        return destination

    def fetch_outputs(self, backend_job_id: str, destination: Path) -> list[Path]:
        state = self.status(backend_job_id)
        history = state.get("history", {})
        outputs = history.get("outputs", {}) if isinstance(history, dict) else {}
        saved: list[Path] = []
        index = 0
        for node in outputs.values() if isinstance(outputs, dict) else []:
            for image in node.get("images", []) if isinstance(node, dict) else []:
                name = Path(str(image.get("filename", f"image-{index}.png"))).name
                target = destination / f"{index:02d}-{name}"
                saved.append(self._download_view(image, target))
                index += 1
        return saved

    def cancel(self, backend_job_id: str) -> None:
        # ComfyUI interrupt currently interrupts the active execution. Keep the id in the
        # signature so a future queue-specific cancellation implementation is compatible.
        self._json("/interrupt", method="POST", payload={})
