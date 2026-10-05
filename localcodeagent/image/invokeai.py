"""InvokeAI backend adapter — REST client + graph builder (InvokeAI 4/5/6).

InvokeAI exposes a queue API: we translate Nexus's normalized generation
spec into a ``enqueue_graph`` payload (model loader → compel conditioning →
noise → denoise_latents → l2i), submit it to ``/api/v1/queue/default``,
poll the queue item for status, and download produced images through
``/api/v1/images/i/{name}/full``.

Operations implemented: text_to_image, image_edit/img2img, inpaint
(denoise mask), variation (seed fan-out or img2img), upscale (esrgan
node when a spandrel/upscale model is installed). Outpaint/canvas and
custom node graphs are honestly unsupported — the router falls back to
ComfyUI for those.
"""
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

QUEUE = "default"

# InvokeAI merges ComfyUI's sampler+scheduler into a single scheduler enum.
_SCHEDULER_MAP = {
    ("euler", "simple"): "euler",
    ("euler", "normal"): "euler",
    ("euler", "karras"): "euler_k",
    ("euler_ancestral", "simple"): "euler_a",
    ("euler_ancestral", "normal"): "euler_a",
    ("heun", "normal"): "heun",
    ("heun", "karras"): "heun_k",
    ("dpmpp_2m", "karras"): "dpmpp_2m_k",
    ("dpmpp_2m", "normal"): "dpmpp_2m",
    ("dpmpp_2m", "simple"): "dpmpp_2m",
    ("dpmpp_2m_sde", "karras"): "dpmpp_2m_sde_k",
    ("dpmpp_2m_sde", "normal"): "dpmpp_2m_sde",
    ("dpmpp_2s_ancestral", "normal"): "dpmpp_2s",
    ("ddim", "normal"): "ddim",
    ("ddim", "ddim_uniform"): "ddim",
    ("lcm", "simple"): "lcm",
    ("lcm", "sgm_uniform"): "lcm",
    ("uni_pc", "normal"): "unipc",
    ("uni_pc", "sgm_uniform"): "unipc",
    ("dpmpp_3m_sde", "karras"): "dpmpp_3m_k",
    ("dpmpp_3m_sde", "normal"): "dpmpp_3m",
    ("lms", "normal"): "lms",
    ("lms", "karras"): "lms_k",
}
_FALLBACK_SCHEDULER = "dpmpp_2m_k" if True else "euler"

# Ops this adapter can execute natively.
SUPPORTED_OPS = {
    "text_to_image", "edit_image", "inpaint", "variation", "upscale",
}

# Bases we can build a faithful InvokeAI graph for. flux/sd3 need different
# node plumbing — reported as unsupported so routing falls back to ComfyUI.
_GRAPHABLE_BASES = {"sd-1", "sd-2", "sdxl", "sdxl-refiner"}


def _conn_error(exc: BaseException, url: str, *, method: str = "GET") -> BackendConnectionError:
    return BackendConnectionError(
        exc, subsystem="invokeai", url=url, method=method,
        request_id=secrets.token_hex(6),
    )


def _model_ref(m: dict[str, Any]) -> dict[str, Any]:
    """ModelIdentifierField for graph nodes."""
    return {k: m.get(k) for k in ("key", "hash", "name", "base", "type")
            if m.get(k) is not None}


class InvokeAIBackend(ImageBackend):
    """Dependency-free adapter for a local InvokeAI server (port 9090)."""

    name = "invokeai"
    display_name = "InvokeAI"

    def __init__(self, endpoint: str = "http://127.0.0.1:9090",
                 *, timeout: float = 4.0) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout
        self.client_id = str(uuid.uuid4())
        self._health_cache: tuple[float, tuple[bool, str]] | None = None

    # -- HTTP plumbing -------------------------------------------------

    def _json(self, path: str, *, method: str = "GET",
              payload: dict | None = None) -> Any:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint + path, data=data, method=method,
            headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                return json.loads(raw.decode("utf-8")) if raw else {}
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, OSError, http.client.HTTPException,
                TimeoutError) as exc:
            raise _conn_error(exc, self.endpoint + path, method=method) from exc

    # -- contract ------------------------------------------------------

    def health(self) -> tuple[bool, str]:
        """Fast health probe — one endpoint, 3s cache. A dead/dropping
        endpoint must not stall UI summaries with serialized retries."""
        import time as _time
        if self._health_cache is not None and _time.time() - self._health_cache[0] < 3.0:
            return self._health_cache[1]
        result = self._health_uncached()
        self._health_cache = (_time.time(), result)
        return result

    def _health_uncached(self) -> tuple[bool, str]:
        for probe in ("/api/v1/app/version", "/api/v1/app/runtime_config"):
            try:
                info = self._json(probe)
                version = info.get("version") or info.get("app_version") or ""
                return True, f"InvokeAI {version}".strip()
            except urllib.error.HTTPError:
                continue  # old version without this route — try the next
            except BackendConnectionError as exc:
                return False, str(exc)
            except Exception as exc:
                return False, str(exc)
        return False, "InvokeAI API not reachable"

    def inspect(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, path in (
            ("version", "/api/v1/app/version"),
            ("config", "/api/v1/app/runtime_config"),
            ("queue", f"/api/v1/queue/{QUEUE}/status"),
        ):
            try:
                out[name] = self._json(path)
            except Exception as exc:
                out[name] = {"error": str(exc)}
        return out

    def capabilities(self) -> set[str]:
        return set(SUPPORTED_OPS)

    def models(self, *, model_type: str = "") -> list[dict[str, Any]]:
        """Installed models from InvokeAI's model manager. v5/v6 mount the
        manager at /api/v2/models; very old installs exposed /api/v1/models."""
        params = {}
        if model_type:
            params["model_type"] = model_type
        query = ("?" + urllib.parse.urlencode(params)) if params else ""
        data: Any = None
        for base in ("/api/v2/models/", "/api/v1/models/"):
            try:
                data = self._json(base + query)
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    continue
                return []
            except Exception:
                return []
        if data is None:
            return []
        rows = data.get("models") if isinstance(data, dict) else None
        if isinstance(rows, list):
            return rows
        return data if isinstance(data, list) else []

    # -- uploads / downloads -------------------------------------------

    def upload_image(self, path: Path, **kwargs: Any) -> dict[str, Any]:
        path = Path(path).resolve()
        if not path.is_file():
            raise FileNotFoundError(path)
        boundary = "----NexusInvoke" + uuid.uuid4().hex
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        # v5/v6: image_category/is_intermediate/crop_visible are query params;
        # only `file` travels in the multipart body.
        query = urllib.parse.urlencode({
            "image_category": "general",
            "is_intermediate": "false",
            "crop_visible": "false",
        })
        head = (f"--{boundary}\r\nContent-Disposition: form-data; "
                f"name=\"file\"; filename=\"{path.name}\"\r\n"
                f"Content-Type: {mime}\r\n\r\n").encode("utf-8")
        tail = f"\r\n--{boundary}--\r\n".encode("utf-8")
        body = head + path.read_bytes() + tail
        req = urllib.request.Request(
            self.endpoint + "/api/v1/images/upload?" + query,
            data=body, method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with urllib.request.urlopen(req, timeout=max(self.timeout, 60.0)) as resp:
                result = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, OSError, http.client.HTTPException,
                TimeoutError) as exc:
            raise _conn_error(exc, self.endpoint + "/api/v1/images/upload",
                              method="POST") from exc
        if not result.get("image_name"):
            raise RuntimeError(f"InvokeAI upload returned no image_name: {result}")
        return result

    def _download(self, image_name: str, destination: Path) -> Path:
        url = f"{self.endpoint}/api/v1/images/i/{urllib.parse.quote(image_name)}/full"
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=max(self.timeout, 60.0)) as resp:
                data = resp.read()
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, OSError, http.client.HTTPException,
                TimeoutError) as exc:
            raise _conn_error(exc, url) from exc
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        return destination

    # -- graph construction ---------------------------------------------

    @staticmethod
    def _scheduler(spec: dict[str, Any]) -> str:
        sampler = str(spec.get("sampler_name") or "").strip()
        sched = str(spec.get("scheduler") or "").strip()
        if not sampler and not sched:
            return _FALLBACK_SCHEDULER
        mapped = _SCHEDULER_MAP.get((sampler, sched)) or \
            _SCHEDULER_MAP.get((sampler, "normal")) or \
            _SCHEDULER_MAP.get((sampler, "simple"))
        if mapped:
            return mapped
        # The user may have named an InvokeAI scheduler directly.
        known = {s for pair in _SCHEDULER_MAP.values() for s in [pair]}
        known |= {"ddim", "ddpm", "deis", "deis_k", "lms", "lms_k", "pndm",
                  "heun", "heun_k", "euler", "euler_k", "euler_a", "kdpm_2",
                  "kdpm_2_a", "dpmpp_2s", "dpmpp_2s_k", "dpmpp_2m",
                  "dpmpp_2m_k", "dpmpp_2m_sde", "dpmpp_2m_sde_k",
                  "dpmpp_3m", "dpmpp_3m_k", "unipc", "unipc_k", "lcm",
                  "euler_cfg_pp", "sgm_uniform"}
        if sched in known:
            return sched
        if sampler in known:
            return sampler
        return _FALLBACK_SCHEDULER

    def build_spec_graph(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Translate a normalized spec into an InvokeAI graph payload."""
        model = spec.get("model") or {}
        base = str(model.get("base") or "sdxl").lower()
        if base not in _GRAPHABLE_BASES:
            raise ValueError(f"operation_unsupported: InvokeAI graph builder "
                             f"does not support base '{base}' yet")
        is_sdxl = base.startswith("sdxl")
        loader_type = "sdxl_model_loader" if is_sdxl else "main_model_loader"
        compel_type = "sdxl_compel_prompt" if is_sdxl else "compel"

        prompt = str(spec.get("prompt") or "")
        negative = str(spec.get("negative_prompt") or "")
        seed = int(spec.get("seed") if spec.get("seed") is not None
                   else secrets.randbelow(2**31))
        width = int(spec.get("width") or 1024)
        height = int(spec.get("height") or 1024)
        steps = int(spec.get("steps") or 25)
        cfg = float(spec.get("guidance") if spec.get("guidance") is not None
                    else (7.5 if not is_sdxl else 5.5))
        strength = float(spec.get("denoise_strength")
                         if spec.get("denoise_strength") is not None else 1.0)
        scheduler = self._scheduler(spec)

        nodes: dict[str, Any] = {
            "model_loader": {
                "id": "model_loader", "type": loader_type,
                "model": _model_ref(model), "is_intermediate": True,
            },
            "positive": {
                "id": "positive", "type": compel_type,
                "prompt": prompt, "is_intermediate": True,
            },
            "negative": {
                "id": "negative", "type": compel_type,
                "prompt": negative, "is_intermediate": True,
            },
            "denoise": {
                "id": "denoise", "type": "denoise_latents",
                "steps": steps, "cfg_scale": cfg, "scheduler": scheduler,
                "denoising_start": 0.0, "denoising_end": 1.0,
                "is_intermediate": True,
            },
            "l2i": {"id": "l2i", "type": "l2i", "fp32": False},
        }
        edges: list[dict[str, Any]] = []

        def edge(src: str, sfield: str, dst: str, dfield: str) -> None:
            edges.append({"source": {"node_id": src, "field": sfield},
                          "destination": {"node_id": dst, "field": dfield}})

        edge("model_loader", "vae", "l2i", "vae")
        edge("denoise", "latents", "l2i", "latents")
        edge("positive", "conditioning", "denoise", "positive_conditioning")
        edge("negative", "conditioning", "denoise", "negative_conditioning")
        # Model output name differs across loader types.
        model_out = "unet" if is_sdxl else "unet"
        edge("model_loader", model_out, "denoise", "unet")
        if is_sdxl:
            edge("model_loader", "clip", "positive", "clip")
            edge("model_loader", "clip2", "positive", "clip2")
            edge("model_loader", "clip", "negative", "clip")
            edge("model_loader", "clip2", "negative", "clip2")
        else:
            edge("model_loader", "clip", "positive", "clip")
            edge("model_loader", "clip", "negative", "clip")

        init_image = str(spec.get("source_image_name") or "")
        mask_image = str(spec.get("mask_image_name") or "")
        loras = spec.get("loras") or []

        if init_image:
            nodes["i2l"] = {"id": "i2l", "type": "i2l",
                            "image": {"image_name": init_image},
                            "is_intermediate": True}
            edge("model_loader", "vae", "i2l", "vae")
            edge("i2l", "latents", "denoise", "latents")
            # img2img convention: denoising_start = 1 - strength.
            strength = max(0.0, min(1.0, strength))
            nodes["denoise"]["denoising_start"] = round(1.0 - strength, 4)
        else:
            nodes["noise"] = {"id": "noise", "type": "noise",
                              "width": width, "height": height,
                              "seed": seed, "use_cpu": False,
                              "is_intermediate": True}
            edge("noise", "noise", "denoise", "noise")

        if mask_image:
            nodes["mask"] = {"id": "mask", "type": "create_denoise_mask",
                             "image": {"image_name": mask_image},
                             "tiled": False, "fp32": True,
                             "is_intermediate": True}
            edge("model_loader", "vae", "mask", "vae")
            edge("mask", "denoise_mask", "denoise", "denoise_mask")

        for idx, lora in enumerate(loras[:4]):
            ref = lora.get("model") or {}
            if not ref:
                continue
            nid = f"lora_{idx}"
            nodes[nid] = {"id": nid, "type": "lora_loader"
                          if not is_sdxl else "sdxl_lora_loader",
                          "lora": _model_ref(ref),
                          "weight": float(lora.get("strength", 1.0)),
                          "is_intermediate": True}
            prev = "model_loader" if idx == 0 else f"lora_{idx - 1}"
            edge(nid, model_out, "denoise", "unet")
            edge(prev, model_out, nid, "model" if is_sdxl else "unet")
            if is_sdxl:
                edge(prev, "clip", nid, "clip")
                edge(nid, "clip", "positive", "clip")
                edge(nid, "clip", "negative", "clip")

        return {"id": "nexus_gen", "nodes": nodes, "edges": edges}

    # -- job lifecycle ---------------------------------------------------

    def submit(self, spec: dict[str, Any]) -> str:
        graph = self.build_spec_graph(spec)
        runs = max(1, int(spec.get("count") or 1))
        # v5/v6: POST /queue/{queue_id}/enqueue_batch with a Batch body.
        # `runs` repeats the graph; the result lists per-run item ids.
        payload = {"graph": graph, "runs": runs,
                   "origin": "nexus", "destination": None}
        response = self._json(f"/api/v1/queue/{QUEUE}/enqueue_batch",
                              method="POST", payload=payload)
        item_ids = response.get("item_ids") or []
        if not item_ids and isinstance(response.get("batch"), dict):
            item_ids = response["batch"].get("item_ids") or []
        if not item_ids:
            raise RuntimeError(f"InvokeAI did not return queue item ids: {response}")
        # Multiple runs produce multiple items — track them all.
        return ",".join(str(i) for i in item_ids)

    @staticmethod
    def _item_ids(backend_job_id: str) -> list[str]:
        return [p for p in str(backend_job_id).split(",") if p]

    def _item(self, item_id: str) -> dict[str, Any]:
        return self._json(
            f"/api/v1/queue/{QUEUE}/i/{urllib.parse.quote(item_id)}")

    def status(self, backend_job_id: str) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for iid in self._item_ids(backend_job_id):
            try:
                items.append(self._item(iid))
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    return {"state": "failed",
                            "error": "InvokeAI queue item not found"}
                raise
        statuses = [str(it.get("status") or "").lower() for it in items]
        if any(s == "canceled" for s in statuses):
            return {"state": "cancelled", "progress": 0.0,
                    "items": items}
        if any(s in {"error", "failed"} for s in statuses):
            item = next((it for it in items
                         if str(it.get("status") or "").lower()
                         in {"error", "failed"}), items[0])
            err = (item.get("error_message") or item.get("error_type")
                   or "InvokeAI job failed")
            return {"state": "failed", "progress": 0.0,
                    "error": str(err), "items": items}
        done = sum(1 for s in statuses if s in {"completed", "complete"})
        if done == len(items):
            return {"state": "finished", "progress": 1.0, "items": items}
        return {"state": "running",
                "progress": min(0.95, done / max(1, len(items))),
                "items": items}

    def fetch_outputs(self, backend_job_id: str, destination: Path) -> list[Path]:
        st = self.status(backend_job_id)
        items = st.get("items") or ([st["item"]] if st.get("item") else [])
        saved: list[Path] = []
        index = 0
        for item in items:
            session = item.get("session") or {}
            results = session.get("results") or {}
            if not isinstance(results, dict):
                continue
            for result in results.values():
                image = result.get("image") if isinstance(result, dict) else None
                if not isinstance(image, dict):
                    continue
                name = image.get("image_name")
                if not name:
                    continue
                saved.append(self._download(
                    str(name), destination / f"{index:02d}-{name}"))
                index += 1
        return saved

    def cancel(self, backend_job_id: str) -> None:
        """Cancel each queued item; fall back to clearing the queue's
        pending items when an item is already past the cancelable point."""
        pending = False
        for iid in self._item_ids(backend_job_id):
            try:
                self._json(
                    f"/api/v1/queue/{QUEUE}/i/{urllib.parse.quote(iid)}/cancel",
                    method="PUT", payload={})
            except urllib.error.HTTPError as exc:
                if exc.code in {404, 405, 409}:
                    pending = True
                    continue
                raise
        if pending:
            # Items already in-flight can't be item-cancelled on some
            # versions — clear pending + cancel the batch as a fallback.
            try:
                self._json(f"/api/v1/queue/{QUEUE}/clear", method="PUT",
                           payload={})
            except Exception:
                pass
