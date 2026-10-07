"""Chatterbox isolated-runtime provisioning.

The engine worker needs Python <= 3.13 (torch) while the frozen backend
is 3.14 — so a self-contained CPython is installed under
``runtime/voice/chatterbox`` from python-build-standalone (the same
relocatable builds uv ships), then pip installs the pinned voice stack
into it. No system Python required; nothing touches the backend's own
environment or any other tool venv.

Layout produced:
    runtime/voice/chatterbox/python.exe            (CPython 3.12.15)
    runtime/voice/chatterbox/Lib/site-packages/... (torch+CUDA, chatterbox-tts)
    runtime/voice/chatterbox/.nexus-runtime.json   (verification marker)

Idempotent: a runtime that verifies is never reinstalled; a partial
install (no marker / failed import probe) is completed in place.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

from ..fsutil import replace_with_retry
from ..procutil import no_window_flags
from .chatterbox_assets import sha256_file

# Pinned relocatable CPython — digest published by the upstream release
# (astral-sh/python-build-standalone tag 20261003).
_PBS_TAG = "20261003"
_PBS_NAME = ("cpython-3.12.15+20261003-x86_64-pc-windows-msvc"
             "-install_only.tar.gz")
PBS_URL = (f"https://github.com/astral-sh/python-build-standalone/"
           f"releases/download/{_PBS_TAG}/"
           "cpython-3.12.15%2B20261003-x86_64-pc-windows-msvc"
           "-install_only.tar.gz")
PBS_SHA256 = "4b6f0beebbb695a0f3ea237b8c3eaa5bd424f47a7bc25b2fbe3a43390c770f08"
PBS_BYTES = 46_509_797

TORCH_VERSION = "2.6.0"
CHATTERBOX_VERSION = "0.1.7"
RUNTIME_MARKER = ".nexus-runtime.json"
RUNTIME_SCHEMA = 1


def _has_nvidia() -> bool:
    if os.name != "nt":
        try:
            return subprocess.run(
                ["nvidia-smi", "-L"], capture_output=True,
                timeout=15).returncode == 0
        except Exception:
            return False
    for cand in (shutil.which("nvidia-smi"),
                 r"C:\Windows\System32\nvidia-smi.exe"):
        if not cand:
            continue
        try:
            return subprocess.run(
                [cand, "-L"], capture_output=True,
                timeout=15,
                creationflags=no_window_flags()).returncode == 0
        except Exception:
            continue
    return False


def _torch_index() -> str:
    return ("https://download.pytorch.org/whl/cu124"
            if _has_nvidia() else "https://download.pytorch.org/whl/cpu")


def runtime_python(runtime_dir: Path) -> Path | None:
    """Worker interpreter — supports both layouts: the provisioned
    standalone (python.exe at root) and a dev-built venv."""
    rd = Path(runtime_dir)
    candidates = (
        [rd / "python.exe", rd / "Scripts" / "python.exe",
         rd / "python" / "python.exe"]
        if os.name == "nt" else
        [rd / "bin" / "python", rd / "python" / "bin" / "python3"])
    for p in candidates:
        if p.exists():
            return p
    return None


def _marker_path(runtime_dir: Path) -> Path:
    return Path(runtime_dir) / RUNTIME_MARKER


def _run(py: Path, args: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(py), "-u", *args], capture_output=True, text=True,
        timeout=timeout,
        creationflags=(subprocess.CREATE_NO_WINDOW
                       if os.name == "nt" else 0))


def _packages_ok(py: Path) -> tuple[bool, str]:
    """Import probe — the marker alone could survive a half-installed
    site-packages, so the truth test is 'does the stack import'."""
    r = _run(py, ["-c",
                  "import torch, chatterbox; "
                  "print(torch.__version__, chatterbox.__version__)"],
             timeout=120)
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "import failed")[-400:]
    return True, r.stdout.strip()


def runtime_status(runtime_dir: Path) -> dict:
    rd = Path(runtime_dir)
    py = runtime_python(rd)
    marker = {}
    try:
        marker = json.loads(_marker_path(rd).read_text(encoding="utf-8"))
    except Exception:
        pass
    st = {"runtime_dir": str(rd), "python": str(py) if py else "",
          "python_present": py is not None,
          "packages_ok": False, "verified": False, "detail": ""}
    if py is None:
        st["detail"] = "python not installed"
        return st
    ok, detail = _packages_ok(py)
    st["packages_ok"] = ok
    st["detail"] = detail
    st["verified"] = ok and marker.get("schema") == RUNTIME_SCHEMA
    return st


def _download(url: str, sha256: str, dest_dir: Path,
              progress=None) -> Path:
    tmp = dest_dir / (Path(url.split("?")[0]).name + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "nexus-core"})
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
                progress("python-runtime", done)
    if sha256_file(tmp) != sha256:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("python runtime archive failed sha256 verification")
    return tmp


def _extract_python(archive: Path, runtime_dir: Path) -> None:
    """pbs tarball unpacks to python/ — flatten into runtime_dir so
    python.exe sits at the root (matching runtime_python() layout)."""
    stage = Path(tempfile.mkdtemp(prefix="nexus-py-",
                                  dir=str(runtime_dir.parent)))
    try:
        with tarfile.open(archive) as tf:
            tf.extractall(stage, filter="data")
        root = stage / "python"
        if not (root / ("python.exe" if os.name == "nt"
                        else "bin" / "python3")).exists():
            raise RuntimeError("python runtime archive had unexpected layout")
        runtime_dir.mkdir(parents=True, exist_ok=True)
        for child in root.iterdir():
            replace_with_retry(child, runtime_dir / child.name)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def ensure_runtime(runtime_dir: Path, progress=None) -> dict:
    """Install or complete the isolated voice runtime. Idempotent —
    a verified runtime short-circuits before touching the network."""
    rd = Path(runtime_dir)
    st = runtime_status(rd)
    if st["verified"]:
        return st
    rd.mkdir(parents=True, exist_ok=True)

    py = runtime_python(rd)
    if py is None:
        if progress:
            progress("python-runtime", 0)
        archive = _download(PBS_URL, PBS_SHA256, rd, progress)
        _extract_python(archive, rd)
        archive.unlink(missing_ok=True)
        py = runtime_python(rd)
        if py is None:
            raise RuntimeError("python extraction produced no interpreter")

    ok, detail = _packages_ok(py)
    if not ok:
        if progress:
            progress("pip-install", 0)
        # torch wheels come from the PyTorch index (CUDA or CPU build);
        # everything else from PyPI. chatterbox deps must NOT reinstall
        # torch — install order matters.
        r = _run(py, ["-m", "pip", "install", "--no-input",
                      f"torch=={TORCH_VERSION}",
                      f"torchaudio=={TORCH_VERSION}",
                      "--index-url", _torch_index()], timeout=3600)
        if r.returncode != 0:
            raise RuntimeError(
                f"torch install failed: {(r.stderr or '')[-400:]}")
        r = _run(py, ["-m", "pip", "install", "--no-input",
                      f"chatterbox-tts=={CHATTERBOX_VERSION}"],
                 timeout=3600)
        if r.returncode != 0:
            raise RuntimeError(
                f"chatterbox-tts install failed: {(r.stderr or '')[-400:]}")
        ok, detail = _packages_ok(py)
        if not ok:
            raise RuntimeError(f"voice stack import probe failed: {detail}")

    marker = {"schema": RUNTIME_SCHEMA,
              "python": "3.12.15", "pbs_tag": _PBS_TAG,
              "torch": TORCH_VERSION,
              "chatterbox_tts": CHATTERBOX_VERSION,
              "device_stack": "cu124" if _has_nvidia() else "cpu"}
    _marker_path(rd).write_text(json.dumps(marker, indent=2),
                                encoding="utf-8")
    return runtime_status(rd)
