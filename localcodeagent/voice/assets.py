"""Kokoro model/voice asset manifest + verified download.

Sources verified 2025 (upstream hexgrad/Kokoro-82M, Apache-2.0, v1.0):
  model : https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx
  voices: https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin
SHA256 were computed from the files actually downloaded and exercised by
Nexus Core on this machine — not copied from a page. If upstream republishes,
update these hashes deliberately.
"""
from __future__ import annotations

import hashlib
import urllib.request
from pathlib import Path

ASSETS = {
    "kokoro-v1.0.onnx": {
        "url": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx",
        "sha256": "7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5",
        "kind": "model",
    },
    "voices-v1.0.bin": {
        "url": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin",
        "sha256": "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d",
        "kind": "voices",
    },
}

UPSTREAM = {
    "model_repo": "hexgrad/Kokoro-82M",
    "package": "kokoro-onnx",
    "package_version": "0.6.1",
    "onnxruntime_version": "1.30.0",
    "license": "Apache-2.0",
}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def asset_status(asset_dir: Path) -> dict[str, dict]:
    out = {}
    for name, meta in ASSETS.items():
        p = asset_dir / name
        entry = {"path": str(p), "kind": meta["kind"], "present": p.exists(),
                 "verified": False}
        if p.exists():
            try:
                entry["verified"] = sha256_file(p) == meta["sha256"]
            except OSError:
                pass
        out[name] = entry
    return out


def ensure_assets(asset_dir: Path, progress=None) -> dict[str, dict]:
    """Download + hash-verify any missing/unverified assets. Idempotent —
    already-verified files are never redownloaded."""
    asset_dir.mkdir(parents=True, exist_ok=True)
    for name, meta in ASSETS.items():
        path = asset_dir / name
        if path.exists() and sha256_file(path) == meta["sha256"]:
            continue
        tmp = path.with_suffix(path.suffix + ".part")
        req = urllib.request.Request(meta["url"], headers={"User-Agent": "nexus-core"})
        with urllib.request.urlopen(req, timeout=120) as resp, open(tmp, "wb") as fh:
            done = 0
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                if progress:
                    progress(name, done)
        if sha256_file(tmp) != meta["sha256"]:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"voice asset {name} failed sha256 verification")
        tmp.replace(path)
    return asset_status(asset_dir)
