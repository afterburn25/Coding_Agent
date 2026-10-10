from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable

from .types import ImageModelProfile
from .workflow import WorkflowManager
from ..fsutil import replace_with_retry


LORA_EXTENSIONS = {".safetensors", ".gguf", ".ckpt", ".pt", ".pth"}


def _sha256(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


class ImageAssetLibrary:
    """Local image model/LoRA library with verification and explicit installs.

    The library never silently replaces an existing large file. Existing files are
    reused when verification passes (or when no checksum/size is provided). Repair
    is an explicit operation.
    """

    def __init__(self, *, base_dir: Path, models_dir: Path, workflows_dir: Path) -> None:
        self.base_dir = base_dir.resolve()
        self.models_dir = models_dir.resolve()
        self.workflows_dir = workflows_dir.resolve()
        self.models_dir.mkdir(parents=True, exist_ok=True)
        self.workflows_dir.mkdir(parents=True, exist_ok=True)
        self.lora_dir = self.models_dir / "loras"
        self.lora_dir.mkdir(parents=True, exist_ok=True)
        self._install_jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    def resolve(self, value: str) -> Path:
        p = Path(value).expanduser()
        return p.resolve() if p.is_absolute() else (self.base_dir / p).resolve()

    def _allowed_model_path(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.models_dir)
            return True
        except ValueError:
            return False

    def _component_rows(self, profile: ImageModelProfile) -> list[dict[str, Any]]:
        if profile.components:
            return [dict(x) for x in profile.components]
        if profile.model_path:
            return [{"key": "model", "path": profile.model_path, "required": True}]
        return []

    def verify_model(self, profile: ImageModelProfile, *, deep_hash: bool = False) -> dict[str, Any]:
        components=[]
        required_ok=True
        for spec in self._component_rows(profile):
            key=str(spec.get("key") or Path(str(spec.get("path") or "component")).name)
            raw_path=str(spec.get("path") or "")
            path=self.resolve(raw_path) if raw_path else Path()
            required=bool(spec.get("required", True))
            exists=bool(raw_path and path.is_file())
            size=path.stat().st_size if exists else 0
            expected_size=int(spec.get("size_bytes") or 0)
            expected_hash=str(spec.get("sha256") or "").lower().strip()
            size_ok=(not expected_size) or size == expected_size
            hash_value=""
            hash_ok=True
            if exists and expected_hash and deep_hash:
                hash_value=_sha256(path)
                hash_ok=hash_value.lower() == expected_hash
            ok=exists and size_ok and hash_ok
            if required and not ok:
                required_ok=False
            components.append({
                "key":key,"path":str(path) if raw_path else "","required":required,"exists":exists,
                "size_bytes":size,"size_gb":round(size/(1024**3),2),"expected_size_bytes":expected_size,
                "size_ok":size_ok,"sha256":hash_value,"expected_sha256":expected_hash,"hash_ok":hash_ok,
                "url":str(spec.get("url") or ""),"ok":ok,
            })

        workflow_rows=[]
        workflow_map=profile.workflows or ({"default":profile.workflow} if profile.workflow else {})
        for operation,name in workflow_map.items():
            path=(self.workflows_dir / name).resolve()
            exists=path.is_file() and path.is_relative_to(self.workflows_dir)
            validation: dict[str, Any] = {"valid": False, "format": "unknown", "node_count": 0, "errors": []}
            if exists:
                try:
                    raw=json.loads(path.read_text(encoding="utf-8"))
                    validation=WorkflowManager.validate_api(raw)
                except Exception as exc:
                    validation={"valid":False,"format":"unknown","node_count":0,"errors":[f"{type(exc).__name__}: {exc}"],"unresolved_tokens":[]}
            workflow_rows.append({"operation":operation,"name":name,"path":str(path),"exists":exists,**validation})

        installed=required_ok and bool(components)
        status="installed" if installed else ("partial" if any(c["exists"] for c in components) else "missing")
        return {
            "id":profile.id,"family":profile.family,"status":status,"installed":installed,
            "components":components,"workflows":workflow_rows,"required_nodes":list(profile.required_nodes),
        }

    def verify_all(self, profiles: list[ImageModelProfile], *, deep_hash: bool = False) -> list[dict[str, Any]]:
        return [self.verify_model(p, deep_hash=deep_hash) for p in profiles]

    def list_loras(self) -> list[dict[str, Any]]:
        rows=[]
        for path in sorted(p for p in self.lora_dir.rglob("*") if p.is_file() and p.suffix.lower() in LORA_EXTENSIONS):
            meta={}
            for candidate in (path.with_suffix(path.suffix+".json"), path.with_suffix(".json")):
                if candidate.is_file():
                    try:
                        meta=json.loads(candidate.read_text(encoding="utf-8"))
                        break
                    except Exception:
                        pass
            rows.append({
                "id":str(meta.get("id") or path.stem),"name":str(meta.get("name") or path.stem),
                "path":str(path.resolve()),"backend_name":path.relative_to(self.lora_dir).as_posix(),"size_gb":round(path.stat().st_size/(1024**3),3),
                "enabled":bool(meta.get("enabled", True)),"strength":float(meta.get("strength",1.0)),
                "version":str(meta.get("version") or ""),"compatible_families":list(meta.get("compatible_families") or []),
                "preview_image":str(meta.get("preview_image") or ""),"tags":list(meta.get("tags") or []),
                "notes":str(meta.get("notes") or ""),
            })
        return rows

    def resolve_loras(self, selections: list[dict[str, Any]], profile: ImageModelProfile) -> list[dict[str, Any]]:
        if not selections:
            return []
        if len(selections) > max(0, int(profile.max_loras)):
            raise ValueError(f"{profile.id} supports at most {profile.max_loras} selected LoRA(s)")
        installed = self.list_loras()
        resolved=[]
        used=set()
        for selection in selections:
            if not isinstance(selection, dict):
                raise ValueError("LoRA selections must be objects")
            wanted=str(selection.get("id") or selection.get("name") or selection.get("path") or "").strip()
            if not wanted:
                raise ValueError("Each LoRA selection requires a name or id")
            wanted_lower=wanted.lower().replace("\\", "/")
            matches=[]
            for row in installed:
                keys={str(row.get("id") or "").lower(),str(row.get("name") or "").lower(),str(row.get("backend_name") or "").lower(),Path(str(row.get("path") or "")).name.lower()}
                if wanted_lower in keys:
                    matches.append(row)
            if not matches:
                raise ValueError(f"LoRA is not installed: {wanted}")
            if len(matches) > 1:
                raise ValueError(f"LoRA selection is ambiguous: {wanted}; use its id or relative filename")
            row=dict(matches[0])
            if not row.get("enabled", True):
                raise ValueError(f"LoRA is disabled: {row.get('name')}")
            compat=[str(x).lower() for x in row.get("compatible_families",[]) if str(x).strip()]
            if compat and "*" not in compat and profile.family.lower() not in compat and profile.id.lower() not in compat:
                raise ValueError(f"LoRA {row.get('name')} is not marked compatible with {profile.family}")
            requested_version=str(selection.get("version") or "").strip()
            if requested_version and requested_version != str(row.get("version") or ""):
                raise ValueError(f"LoRA {row.get('name')} version {requested_version} was requested but installed version is {row.get('version') or 'unversioned'}")
            key=str(row.get("path") or "")
            if key in used:
                raise ValueError(f"LoRA selected more than once: {row.get('name')}")
            used.add(key)
            strength=float(selection.get("strength", row.get("strength",1.0)))
            if not -4.0 <= strength <= 4.0:
                raise ValueError(f"LoRA strength for {row.get('name')} must be between -4 and 4")
            resolved.append({
                "id":row.get("id"),"display_name":row.get("name"),"name":row.get("backend_name"),
                "path":row.get("path"),"version":row.get("version"),"strength":strength,
                "compatible_families":row.get("compatible_families",[]),
            })
        return resolved

    def save_lora_metadata(self, lora_path: str, metadata: dict[str, Any]) -> dict[str, Any]:
        path=self.resolve(lora_path)
        if not self._allowed_model_path(path) or not path.is_file() or self.lora_dir not in path.parents:
            raise ValueError("LoRA path must point to an existing file inside models/image/loras")
        sidecar=path.with_suffix(path.suffix+".json")
        existing={}
        if sidecar.is_file():
            try: existing=json.loads(sidecar.read_text(encoding="utf-8"))
            except Exception: existing={}
        merged={**existing, **metadata, "path":str(path), "updated_at":time.time()}
        sidecar.write_text(json.dumps(merged, indent=2), encoding="utf-8")
        return merged

    def write_comfy_extra_paths(self, destination: Path) -> Path:
        destination=destination.resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        base=str(self.models_dir).replace("\\", "/")
        # ComfyUI supports multiple directories per category in extra_model_paths.yaml.
        text=(
            "local_code_agent:\n"
            f"  base_path: \"{base}\"\n"
            "  diffusion_models: |\n"
            "    qwen/diffusion_models\n"
            "    flux/diffusion_models\n"
            "    stable-diffusion/diffusion_models\n"
            "  text_encoders: |\n"
            "    qwen/text_encoders\n"
            "    flux/text_encoders\n"
            "    stable-diffusion/text_encoders\n"
            "  vae: |\n"
            "    qwen/vae\n"
            "    flux/vae\n"
            "    vae\n"
            "  checkpoints: |\n"
            "    stable-diffusion/checkpoints\n"
            "  loras: loras\n"
            "  controlnet: controlnet\n"
            "  upscale_models: upscalers\n"
        )
        destination.write_text(text, encoding="utf-8")
        return destination

    def _safe_download_url(self, url: str) -> str:
        parsed=urllib.parse.urlsplit(url)
        if parsed.scheme == "https":
            return url
        if parsed.scheme == "http" and (parsed.hostname or "").lower() in {"127.0.0.1","localhost","::1"}:
            return url
        raise ValueError("Model downloads require HTTPS (HTTP is allowed only for localhost testing).")

    def _download_component(self, spec: dict[str, Any], *, repair: bool, progress: Callable[[int,int],None]) -> dict[str, Any]:
        raw_path=str(spec.get("path") or "")
        url=self._safe_download_url(str(spec.get("url") or ""))
        if not raw_path:
            raise ValueError("Component path is missing")
        target=self.resolve(raw_path)
        if not self._allowed_model_path(target):
            raise ValueError("Image model download destination must stay inside models/image")
        target.parent.mkdir(parents=True, exist_ok=True)
        expected_size=int(spec.get("size_bytes") or 0)
        expected_hash=str(spec.get("sha256") or "").lower().strip()
        if target.is_file() and not repair:
            size=target.stat().st_size
            size_ok=(not expected_size) or size==expected_size
            hash_ok=(not expected_hash) or _sha256(target).lower()==expected_hash
            if size_ok and hash_ok:
                return {"path":str(target),"status":"existing","size_bytes":size}
            raise RuntimeError(f"Existing model file failed verification: {target}. Use repair to replace it.")

        tmp=target.with_suffix(target.suffix+".part")
        done=0
        headers={"User-Agent":"LocalCodeAgent/0.4"}
        if tmp.is_file():
            partial=tmp.stat().st_size
            if expected_size and partial==expected_size:
                # Previous attempt died between download and verification —
                # verify the part in place instead of re-fetching.
                if (not expected_hash) or _sha256(tmp).lower()==expected_hash:
                    replace_with_retry(tmp,target)
                    return {"path":str(target),"status":"downloaded","size_bytes":target.stat().st_size}
                tmp.unlink(missing_ok=True)
            elif expected_size and partial>expected_size:
                tmp.unlink(missing_ok=True)
            elif partial>0:
                headers["Range"]=f"bytes={partial}-"
                done=partial
        req=urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                remaining=int(resp.headers.get("Content-Length") or 0)
                mode="ab" if done else "wb"
                if done and getattr(resp,"status",None)!=206:
                    # Server ignored the Range request — restart cleanly.
                    done=0; mode="wb"
                    remaining=int(resp.headers.get("Content-Length") or 0)
                total=done+remaining if remaining else expected_size
                need=remaining or expected_size
                free=shutil.disk_usage(target.parent).free
                if need and free < need + 512*1024*1024:
                    raise OSError("Insufficient disk space for model download plus safety reserve")
                with tmp.open(mode) as out:
                    while True:
                        chunk=resp.read(4*1024*1024)
                        if not chunk: break
                        out.write(chunk); done+=len(chunk); progress(done,total)
            if expected_size and tmp.stat().st_size != expected_size:
                tmp.unlink(missing_ok=True)
                raise RuntimeError(f"Downloaded size mismatch for {target.name}")
            if expected_hash and _sha256(tmp).lower() != expected_hash:
                tmp.unlink(missing_ok=True)
                raise RuntimeError(f"SHA256 mismatch for {target.name}")
            replace_with_retry(tmp,target)
            return {"path":str(target),"status":"downloaded","size_bytes":target.stat().st_size}
        except Exception:
            # Mid-stream failures keep the .part so the next attempt resumes;
            # verification failures above already removed the corrupt file.
            raise

    def start_install(self, profile: ImageModelProfile, *, repair: bool = False) -> dict[str, Any]:
        specs=[s for s in self._component_rows(profile) if s.get("url") and (bool(s.get("required", True)) or bool(s.get("install_default", False)))]
        if not specs:
            raise RuntimeError(f"Model '{profile.id}' has no downloadable components configured.")
        with self._lock:
            for existing in self._install_jobs.values():
                if existing.get("model_id") == profile.id and existing.get("state") in {"queued", "downloading"}:
                    # A live job for this model already owns the .part files —
                    # a second downloader would corrupt the same targets.
                    return dict(existing)
        job={
            "id":uuid.uuid4().hex,"model_id":profile.id,"state":"queued","progress":0.0,
            "current_file":"","created_at":time.time(),"finished_at":None,"error":"","results":[],
        }
        with self._lock: self._install_jobs[job["id"]]=job

        def run():
            try:
                job["state"]="downloading"
                count=len(specs)
                for idx,spec in enumerate(specs):
                    job["current_file"]=str(spec.get("path") or "")
                    def cb(done:int,total:int, idx=idx):
                        frac=(done/total) if total else 0.0
                        job["progress"]=min(0.99,(idx+frac)/count)
                    result=self._download_component(spec,repair=repair,progress=cb)
                    job["results"].append(result)
                    job["progress"]=(idx+1)/count
                job["state"]="finished"; job["progress"]=1.0
            except Exception as exc:
                job["state"]="failed"; job["error"]=f"{type(exc).__name__}: {exc}"
            finally:
                job["finished_at"]=time.time()
        threading.Thread(target=run,daemon=True).start()
        return dict(job)

    def install_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(x) for x in sorted(self._install_jobs.values(),key=lambda x:x["created_at"],reverse=True)[:50]]

    def remove_model(self, profile: ImageModelProfile) -> list[str]:
        removed=[]
        for spec in self._component_rows(profile):
            path=self.resolve(str(spec.get("path") or ""))
            if self._allowed_model_path(path) and path.is_file():
                path.unlink(); removed.append(str(path))
        return removed
