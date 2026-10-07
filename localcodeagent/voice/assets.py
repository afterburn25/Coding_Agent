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
import json
import urllib.request
from pathlib import Path

from ..fsutil import replace_with_retry

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


# Verified-content cache — after a file's sha256 is checked once, its
# (size, mtime_ns, sha256) signature is recorded here. Status re-checks
# trust unchanged metadata instead of re-hashing hundreds of MB of ONNX
# weights on every engine init; changed/unknown files are still hashed.
_VERIFY_CACHE = ".nexus-assets-verified.json"


def asset_status(asset_dir: Path) -> dict[str, dict]:
    asset_dir = Path(asset_dir)
    cache: dict = {}
    try:
        raw = json.loads(
            (asset_dir / _VERIFY_CACHE).read_text(encoding="utf-8"))
        if isinstance(raw, dict) and isinstance(raw.get("files"), dict):
            cache = raw["files"]
    except Exception:
        cache = {}
    out = {}
    new_cache: dict = {}
    dirty = False
    for name, meta in ASSETS.items():
        p = asset_dir / name
        entry = {"path": str(p), "kind": meta["kind"], "present": p.exists(),
                 "verified": False}
        if p.exists():
            try:
                stt = p.stat()
            except OSError:
                out[name] = entry
                continue
            sig = {"size": stt.st_size, "mtime_ns": stt.st_mtime_ns,
                   "sha256": meta["sha256"]}
            if cache.get(name) == sig:
                entry["verified"] = True
            else:
                try:
                    entry["verified"] = sha256_file(p) == meta["sha256"]
                    dirty = True
                except OSError:
                    pass
            if entry["verified"]:
                new_cache[name] = sig
        out[name] = entry
    if dirty or new_cache != cache:
        try:
            tmp = (asset_dir / _VERIFY_CACHE).with_suffix(".tmp")
            tmp.write_text(json.dumps({"files": new_cache}),
                           encoding="utf-8")
            tmp.replace(asset_dir / _VERIFY_CACHE)
        except OSError:
            pass
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
        replace_with_retry(tmp, path)
    return asset_status(asset_dir)
