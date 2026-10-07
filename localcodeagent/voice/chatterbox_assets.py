"""Chatterbox Turbo model + runtime provisioning.

Model files come from ``ResembleAI/chatterbox-turbo`` (pinned revision);
each entry's SHA-256 was computed from the files actually exercised by
Nexus Core — same verification policy as ``voice/assets.py``.
Idempotent: a file whose sha256 already verifies is never redownloaded.

The *runtime* (isolated Python 3.12 with torch+CUDA and chatterbox-tts)
is provisioned separately by ``chatterbox_runtime.py`` through the
provisioning manager — it is not a single downloadable file.
"""
from __future__ import annotations

import hashlib
import urllib.request
from pathlib import Path

from ..fsutil import replace_with_retry

MODEL_REPO = "ResembleAI/chatterbox-turbo"
MODEL_REVISION = "749d1c1a46eb10492095d68fbcf55691ccf137cd"
_HF = f"https://huggingface.co/{MODEL_REPO}/resolve/{MODEL_REVISION}"

# sha256 computed 2026-01 from the downloaded, working snapshot.
MODEL_FILES = {
    "ve.safetensors": {
        "url": f"{_HF}/ve.safetensors",
        "sha256": "f0921cab452fa278bc25cd23ffd59d36f816d7dc5181dd1bef9751a7fb61f63c",
    },
    "t3_turbo_v1.safetensors": {
        "url": f"{_HF}/t3_turbo_v1.safetensors",
        "sha256": "fcf1f8c1d651bb7e3acd69ee5be269b4ac10c02980b7708213d598bc9f7cdf87",
    },
    "s3gen_meanflow.safetensors": {
        "url": f"{_HF}/s3gen_meanflow.safetensors",
        "sha256": "d65cb687a2ed581ee6cc297e919ffefa63386944f42364ae13b78a594945514f",
    },
    "conds.pt": {
        "url": f"{_HF}/conds.pt",
        "sha256": "b1852099306fd6a7814eb9d0bd10186caba7249596cc23868f78a0eefbfa5033",
    },
    "tokenizer_config.json": {
        "url": f"{_HF}/tokenizer_config.json",
        "sha256": "bca16a2ac1ddbd78b8d6228f0031884cc74b6ea54b967d6f6d2ebae9ccde23e6",
    },
    "vocab.json": {
        "url": f"{_HF}/vocab.json",
        "sha256": "f6bd25a65e4e63ca31360e9fb11c7e4f9a391a78385d640acd814092dd6eee4f",
    },
    "merges.txt": {
        "url": f"{_HF}/merges.txt",
        "sha256": "1ce1664773c50f3e0cc8842619a93edc4624525b728b188a9e0be33b7726adc5",
    },
    "added_tokens.json": {
        "url": f"{_HF}/added_tokens.json",
        "sha256": "72e4ab6acb0d9309ac3df4b526ae5fd80a2da5bc5ab7bb02d85096a374f69193",
    },
    "special_tokens_map.json": {
        "url": f"{_HF}/special_tokens_map.json",
        "sha256": "92ba8063bf40aa163eadebbfe0de07c2aebe44cf0d4a9e8726580b0781fd2640",
    },
}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def model_status(model_dir: Path) -> dict[str, dict]:
    out = {}
    for name, meta in MODEL_FILES.items():
        p = model_dir / name
        entry = {"path": str(p), "present": p.exists(), "verified": False}
        if p.exists():
            try:
                entry["verified"] = sha256_file(p) == meta["sha256"]
            except OSError:
                pass
        out[name] = entry
    return out


def model_ready(model_dir: Path) -> bool:
    st = model_status(model_dir)
    return all(e["verified"] for e in st.values())


def ensure_model(model_dir: Path, progress=None) -> dict[str, dict]:
    """Download + hash-verify missing model files. Idempotent."""
    model_dir = Path(model_dir)
    model_dir.mkdir(parents=True, exist_ok=True)
    for name, meta in MODEL_FILES.items():
        path = model_dir / name
        if path.exists() and sha256_file(path) == meta["sha256"]:
            continue
        tmp = path.with_suffix(path.suffix + ".part")
        req = urllib.request.Request(
            meta["url"], headers={"User-Agent": "nexus-core"})
        with urllib.request.urlopen(req, timeout=300) as resp, \
                open(tmp, "wb") as fh:
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
            raise RuntimeError(
                f"chatterbox model file {name} failed sha256 verification")
        replace_with_retry(tmp, path)
    return model_status(model_dir)
