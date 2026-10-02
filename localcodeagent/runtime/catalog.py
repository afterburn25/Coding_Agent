from __future__ import annotations

import hashlib
import json
import shutil
import threading
import time
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class CodingModelAsset:
    id: str
    title: str
    filename: str
    url: str
    sha256: str
    size_bytes: int
    license: str
    source_repo: str
    source_type: str
    roles: tuple[str, ...]
    hardware_note: str
    description: str

    def as_dict(self) -> dict[str, Any]:
        row = asdict(self)
        row["roles"] = list(self.roles)
        row["size_gb"] = round(self.size_bytes / (1024 ** 3), 2)
        return row


CODING_MODEL_CATALOG: tuple[CodingModelAsset, ...] = (
    CodingModelAsset(
        id="qwen3-4b-instruct-q4-k-m",
        title="Qwen3 4B Instruct 2507 · Q4_K_M",
        filename="Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf",
        url="https://huggingface.co/bartowski/Qwen_Qwen3-4B-Instruct-2507-GGUF/resolve/main/Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf",
        sha256="2fde00ce69dd4899c70d020845e2638353015bba0fdf161b3eb965f2bca4464e",
        size_bytes=2_497_280_736,
        license="Apache-2.0 base model",
        source_repo="bartowski/Qwen_Qwen3-4B-Instruct-2507-GGUF",
        source_type="community quantization of Qwen/Qwen3-4B-Instruct-2507",
        roles=("utility",),
        hardware_note="Tiny fast-lane model; fully GPU-resident on ~6 GB VRAM and comfortable on CPU fallback.",
        description="Non-thinking Qwen3 4B instruct tuned for low-latency general conversation and routing.",
    ),
    CodingModelAsset(
        id="qwen3-8b-q4-k-m",
        title="Qwen3 8B · Q4_K_M",
        filename="Qwen3-8B-Q4_K_M.gguf",
        url="https://huggingface.co/Qwen/Qwen3-8B-GGUF/resolve/main/Qwen3-8B-Q4_K_M.gguf",
        sha256="d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785",
        size_bytes=5_027_783_488,
        license="Apache-2.0",
        source_repo="Qwen/Qwen3-8B-GGUF",
        source_type="official",
        roles=("lightweight_reasoner", "light_coder", "general_assistant"),
        hardware_note="Tier-2 light reasoner; GPU-resident on ~8 GB VRAM, hybrid offload from ~4 GB.",
        description="Light reasoning: moderate conversation, short planning, repository navigation, basic debugging, short tool workflows.",
    ),
    CodingModelAsset(
        id="qwen3-14b-q4-k-m",
        title="Qwen3 14B · Q4_K_M",
        filename="Qwen3-14B-Q4_K_M.gguf",
        url="https://huggingface.co/Qwen/Qwen3-14B-GGUF/resolve/main/Qwen3-14B-Q4_K_M.gguf",
        sha256="500a8806e85ee9c83f3ae08420295592451379b4f8cf2d0f41c15dffeb6b81f0",
        size_bytes=9_001_752_960,
        license="Apache-2.0",
        source_repo="Qwen/Qwen3-14B-GGUF",
        source_type="official",
        roles=("utility", "fast_coder", "primary_coder"),
        hardware_note="Good starter for ~12 GB VRAM; context/KV cache also consumes memory.",
        description="Official Qwen GGUF. Fast general reasoning/coding starter for local use.",
    ),
    CodingModelAsset(
        id="qwen3-coder-30b-a3b-q4-k-m",
        title="Qwen3-Coder 30B-A3B Instruct · Q4_K_M",
        filename="Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf",
        url="https://huggingface.co/lm-kit/qwen3-coder-30b-a3b-instruct-gguf/resolve/main/Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf",
        sha256="956682fa9d36d4d0e5a80eb90ff8a001f2c48f988a497e565ae4d0c42af4fe44",
        size_bytes=18_556_688_384,
        license="Apache-2.0 base model",
        source_repo="lm-kit/qwen3-coder-30b-a3b-instruct-gguf",
        source_type="community quantization of Qwen/Qwen3-Coder-30B-A3B-Instruct",
        roles=("deep_reasoner", "reviewer"),
        hardware_note="For 12 GB VRAM, expect llama.cpp CPU/GPU offload; 64 GB system RAM is useful.",
        description="Code-focused Qwen3-Coder MoE quantization for stronger repository work and review.",
    ),
)


def catalog_by_id() -> dict[str, CodingModelAsset]:
    return {item.id: item for item in CODING_MODEL_CATALOG}


def _sha256(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


class CodingModelCatalogManager:
    """Explicit checksummed model installer; never silently replaces files."""

    def __init__(self, models_dir: Path) -> None:
        self.models_dir = models_dir.resolve()
        self.models_dir.mkdir(parents=True, exist_ok=True)
        self.metadata_dir = self.models_dir / ".catalog"
        self.metadata_dir.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, dict[str, Any]] = {}
        self._cancel: dict[str, threading.Event] = {}
        self._lock = threading.RLock()

    def asset(self, catalog_id: str) -> CodingModelAsset:
        item = catalog_by_id().get(str(catalog_id))
        if item is None:
            raise KeyError(catalog_id)
        return item

    def target(self, asset: CodingModelAsset) -> Path:
        return (self.models_dir / asset.filename).resolve()

    def _metadata_path(self, asset: CodingModelAsset) -> Path:
        return self.metadata_dir / f"{asset.id}.json"

    def _trusted_metadata(self, asset: CodingModelAsset) -> bool:
        path = self._metadata_path(asset)
        target = self.target(asset)
        if not path.is_file() or not target.is_file():
            return False
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
            return row.get("sha256") == asset.sha256 and int(row.get("size_bytes") or 0) == asset.size_bytes and target.stat().st_size == asset.size_bytes
        except (OSError, ValueError, TypeError):
            return False

    def status(self, asset: CodingModelAsset) -> dict[str, Any]:
        target = self.target(asset)
        exists = target.is_file()
        size = target.stat().st_size if exists else 0
        trusted = self._trusted_metadata(asset)
        return {**asset.as_dict(), "path": str(target), "installed": exists, "verified": trusted, "size_on_disk": size, "needs_repair": bool(exists and not trusted)}

    def catalog(self) -> list[dict[str, Any]]:
        return [self.status(item) for item in CODING_MODEL_CATALOG]

    def _write_metadata(self, asset: CodingModelAsset) -> None:
        self._metadata_path(asset).write_text(json.dumps({"catalog_id": asset.id, "filename": asset.filename, "sha256": asset.sha256, "size_bytes": asset.size_bytes, "verified_at": time.time(), "source_repo": asset.source_repo}, indent=2) + "\n", encoding="utf-8")

    def verify(self, catalog_id: str, deep_hash: bool = True) -> dict[str, Any]:
        asset = self.asset(catalog_id)
        target = self.target(asset)
        if not target.is_file():
            return {**self.status(asset), "verified": False, "reason": "missing"}
        if target.stat().st_size != asset.size_bytes:
            return {**self.status(asset), "verified": False, "reason": "size_mismatch"}
        if not deep_hash and self._trusted_metadata(asset):
            return {**self.status(asset), "verified": True, "reason": "trusted_install_metadata"}
        digest = _sha256(target)
        verified = digest.lower() == asset.sha256.lower()
        if verified:
            self._write_metadata(asset)
        return {**self.status(asset), "verified": verified, "reason": "sha256" if verified else "sha256_mismatch", "actual_sha256": digest}

    def jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in sorted(self._jobs.values(), key=lambda x: x["created_at"], reverse=True)]

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            if job_id not in self._jobs:
                raise KeyError(job_id)
            return dict(self._jobs[job_id])

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            if job_id not in self._cancel:
                raise KeyError(job_id)
            self._cancel[job_id].set()
            if self._jobs[job_id]["state"] in {"queued", "downloading", "verifying"}:
                self._jobs[job_id]["state"] = "cancelling"
            return dict(self._jobs[job_id])

    def start_install(self, catalog_id: str, repair: bool = False) -> dict[str, Any]:
        asset = self.asset(catalog_id)
        with self._lock:
            for row in self._jobs.values():
                if row.get("catalog_id") == asset.id and row.get("state") in {"queued", "downloading", "verifying", "cancelling"}:
                    return dict(row)
        existing = self.status(asset)
        if existing["verified"]:
            now = time.time()
            return {"id": f"reuse-{asset.id}", "catalog_id": asset.id, "state": "finished", "progress": 1.0, "bytes_done": asset.size_bytes, "bytes_total": asset.size_bytes, "message": "Already installed and verified; existing model reused.", "path": existing["path"], "created_at": now, "finished_at": now, "error": ""}
        if existing["installed"] and not repair:
            raise FileExistsError(f"{asset.filename} already exists but is not trusted/verified. Choose Repair to verify/replace it explicitly.")
        if not existing["installed"]:
            usage = shutil.disk_usage(self.models_dir)
            reserve = 512 * 1024 * 1024
            required = asset.size_bytes + reserve
            if usage.free < required:
                raise OSError(
                    f"Not enough free disk space in {self.models_dir}. "
                    f"{asset.title} needs about {asset.size_bytes / (1024 ** 3):.2f} GB "
                    f"plus 0.5 GB working space, but only {usage.free / (1024 ** 3):.2f} GB is free."
                )
        job_id = uuid.uuid4().hex
        cancel = threading.Event()
        job = {"id": job_id, "catalog_id": asset.id, "title": asset.title, "state": "queued", "progress": 0.0, "bytes_done": 0, "bytes_total": asset.size_bytes, "message": "Queued", "path": str(self.target(asset)), "created_at": time.time(), "finished_at": None, "error": ""}
        with self._lock:
            self._jobs[job_id] = job
            self._cancel[job_id] = cancel
        threading.Thread(target=self._install_worker, args=(job_id, asset, cancel, repair), name=f"chat-nexus-model-{asset.id}", daemon=True).start()
        return dict(job)

    def _update(self, job_id: str, **values: Any) -> None:
        with self._lock:
            if job_id in self._jobs:
                self._jobs[job_id].update(values)

    def _install_worker(self, job_id: str, asset: CodingModelAsset, cancel: threading.Event, repair: bool) -> None:
        target = self.target(asset)
        temp = target.with_suffix(target.suffix + f".{job_id}.part")
        digest = hashlib.sha256()
        done = 0
        try:
            if target.exists() and repair:
                self._update(job_id, state="verifying", message="Verifying existing file before repair")
                if target.stat().st_size == asset.size_bytes and _sha256(target).lower() == asset.sha256.lower():
                    self._write_metadata(asset)
                    self._update(job_id, state="finished", progress=1.0, bytes_done=asset.size_bytes, message="Existing model verified; no download needed.", finished_at=time.time())
                    return
            self._update(job_id, state="downloading", message=f"Downloading {asset.title}")
            request = urllib.request.Request(asset.url, headers={"User-Agent": "Chat-Nexus/0.6"})
            with urllib.request.urlopen(request, timeout=60) as response, temp.open("wb") as output:
                while True:
                    if cancel.is_set():
                        raise InterruptedError("Download cancelled by user.")
                    chunk = response.read(4 * 1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    self._update(job_id, bytes_done=done, progress=min(0.98, done / max(1, asset.size_bytes)))
            self._update(job_id, state="verifying", message="Verifying size and SHA256")
            if done != asset.size_bytes:
                raise ValueError(f"Downloaded size {done} does not match expected {asset.size_bytes}.")
            actual = digest.hexdigest()
            if actual.lower() != asset.sha256.lower():
                raise ValueError(f"SHA256 mismatch. Expected {asset.sha256}; got {actual}.")
            temp.replace(target)
            self._write_metadata(asset)
            self._update(job_id, state="finished", progress=1.0, bytes_done=done, message="Installed and verified.", finished_at=time.time())
        except InterruptedError as exc:
            temp.unlink(missing_ok=True)
            self._update(job_id, state="cancelled", message=str(exc), error=str(exc), finished_at=time.time())
        except Exception as exc:
            temp.unlink(missing_ok=True)
            self._update(job_id, state="failed", message=f"{type(exc).__name__}: {exc}", error=f"{type(exc).__name__}: {exc}", finished_at=time.time())
