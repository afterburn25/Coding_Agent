"""Managed lifecycle for a local InvokeAI server (default port 9090).

Mirrors ComfyUIRuntime: discovery across configured dirs, bundled venvs,
managed-python script dirs, and PATH; marker files for orphan reclaim;
probe/start/stop/recover. InvokeAI ships as the ``invokeai`` pip package
whose launcher is ``invokeai-web`` — it serves the REST API that
InvokeAIBackend talks to.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .invokeai import InvokeAIBackend
from ..fsutil import atomic_write_text
from ..procutil import no_window_flags


@dataclass(slots=True)
class InvokeRuntimeStatus:
    state: str = "external"
    pid: int | None = None
    managed: bool = False
    healthy: bool = False
    installed: bool = False
    error: str = ""
    log_path: str = ""
    restarts: int = 0
    started_at: float = 0.0
    installed_version: str = ""

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "pid": self.pid,
            "managed": self.managed,
            "healthy": self.healthy,
            "installed": self.installed,
            "error": self.error,
            "log_path": self.log_path,
            "restarts": self.restarts,
            "started_at": self.started_at,
            "installed_version": self.installed_version,
        }


class InvokeAIRuntime:
    """Optionally owns a local InvokeAI process while also supporting external servers."""

    def __init__(self, *, base_dir: Path, backend: InvokeAIBackend, config, leak_tracker=None) -> None:
        self.leak_tracker = leak_tracker
        self.base_dir = base_dir.resolve()
        self.backend = backend
        self.config = config
        self._process: subprocess.Popen | None = None
        self._log_handle = None
        self._lock = threading.RLock()
        self._discover_cache: tuple[Path | None, list[str] | None] | None = None
        self._discover_ts = 0.0
        self.status = InvokeRuntimeStatus()
        if not getattr(config, "invokeai_auto_start", False):
            self.status.state = "external"

    def _resolve(self, value: str) -> Path:
        p = Path(value).expanduser()
        return p.resolve() if p.is_absolute() else (self.base_dir / p).resolve()

    def discover(self) -> tuple[Path | None, list[str] | None]:
        """Locate InvokeAI. Returns (install_root, command_prefix) where the
        prefix is ``[invokeai-web]`` or ``[python, -m, invokeai.app.run]``.
        Results cache 60s — the import probe can be slow."""
        if self._discover_cache is not None \
                and time.time() - self._discover_ts < 60.0:
            return self._discover_cache
        result = self._discover_uncached()
        self._discover_cache = result
        self._discover_ts = time.time()
        return result

    def invalidate_discovery(self) -> None:
        """Drop the 60s discovery cache — call after install/repair/
        removal/runtime-path changes so the next probe re-scans."""
        self._discover_cache = None
        self._discover_ts = 0.0

    def installed_version(self, root: Path | None = None) -> str:
        """The invokeai package version inside the discovered venv — read
        from dist-info metadata on disk (a subprocess import would stall
        callers for seconds)."""
        if root is None:
            root, _prefix = self.discover()
        if root is None:
            return ""
        try:
            for site in (root / "Lib" / "site-packages",
                         *(root / "lib").glob("python*/site-packages")):
                if not site.is_dir():
                    continue
                for d in site.glob("invokeai-*.dist-info"):
                    return d.name[len("invokeai-"):-len(".dist-info")]
        except OSError:
            pass
        return ""

    def registry_models(self) -> list[dict]:
        """InvokeAI's registered models read directly from its SQLite
        registry — works while the server is stopped.

        The registry lives at ``<managed-root>/databases/invokeai.db``;
        each ``models.config`` row is the same JSON shape the REST API
        returns, so rows carry the real ``key``/``name``/``base``/``type``
        the graph builder needs. Returns [] when no managed registry
        exists or the schema isn't recognized — callers fall back to the
        live API or an empty pool."""
        data_root = self.base_dir / "data" / "invokeai"
        db_path = data_root / "databases" / "invokeai.db"
        if not db_path.is_file():
            return []
        import sqlite3
        rows: list[dict] = []
        try:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                for (raw,) in con.execute("select config from models"):
                    try:
                        cfg = json.loads(raw)
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(cfg, dict) or not cfg.get("key"):
                        continue
                    rows.append(cfg)
            finally:
                con.close()
        except Exception:
            return []
        return rows

    def _discover_uncached(self) -> tuple[Path | None, list[str] | None]:
        configured = str(getattr(self.config, "invokeai_dir", "")).strip()
        dirs: list[Path] = []
        if configured:
            dirs.append(self._resolve(configured))
        dirs.extend([
            self.base_dir / "tools" / "InvokeAI",
            self.base_dir / "tools" / "invokeai",
            self.base_dir / "InvokeAI",
            self.base_dir / "invokeai",
        ])
        script_names = ("invokeai-web.exe", "invokeai-web.bat",
                        "invokeai-web.cmd", "invokeai-web")
        for d in dirs:
            if not d.is_dir():
                continue
            # The dir itself may be a venv, or may contain one a level down.
            homes = [d] + [p for p in d.iterdir()
                           if p.is_dir() and (p / ("Scripts" if os.name == "nt" else "bin")).is_dir()]
            for home in homes:
                for sub in ("Scripts", "bin"):
                    for name in script_names:
                        exe = home / sub / name
                        if exe.is_file():
                            return home.resolve(), [str(exe.resolve())]
                for pyname in ("python.exe", "python"):
                    py = home / ("Scripts" if os.name == "nt" else "bin") / pyname
                    if not py.is_file():
                        continue
                    try:
                        # v5/v6 expose invokeai.app.run_app; v4 used
                        # invokeai.app.run. Neither module is runnable
                        # with -m (no __main__ entry — exits 0 silently),
                        # so call the console-script function directly:
                        # invokeai-web → invokeai.app.run_app:run_app.
                        probe = subprocess.run(
                            [str(py), "-c",
                             "from invokeai.app.run_app import run_app"],
                            capture_output=True, timeout=30,
                            creationflags=no_window_flags())
                        if probe.returncode == 0:
                            return home.resolve(), [str(py.resolve()), "-c",
                                                    "from invokeai.app.run_app import run_app; run_app()"]
                        probe = subprocess.run(
                            [str(py), "-c", "from invokeai.app.run import invoke_ai_api"],
                            capture_output=True, timeout=30,
                            creationflags=no_window_flags())
                        if probe.returncode == 0:
                            return home.resolve(), [str(py.resolve()), "-c",
                                                    "from invokeai.app.run import invoke_ai_api; invoke_ai_api()"]
                    except (OSError, subprocess.TimeoutExpired):
                        continue
        configured_python = str(getattr(self.config, "invokeai_python", "")).strip()
        # The import probe is a subprocess — only worth it for a python the
        # user explicitly pointed at, never in the ambient PATH search (a
        # 30s module import inside discover() would stall API summary calls).
        if configured_python:
            py = str(self._resolve(configured_python))
            scripts = Path(py).resolve().parent
            for name in script_names:
                exe = scripts / name
                if exe.is_file():
                    return scripts.parent.resolve(), [str(exe.resolve())]
            try:
                probe = subprocess.run(
                    [py, "-c", "from invokeai.app.run_app import run_app"],
                    capture_output=True, timeout=30,
                    creationflags=no_window_flags())
                if probe.returncode == 0:
                    return scripts.parent.resolve(), [py, "-c",
                                                      "from invokeai.app.run_app import run_app; run_app()"]
                probe = subprocess.run(
                    [py, "-c", "from invokeai.app.run import invoke_ai_api"],
                    capture_output=True, timeout=30,
                    creationflags=no_window_flags())
                if probe.returncode == 0:
                    return scripts.parent.resolve(), [py, "-c",
                                                      "from invokeai.app.run import invoke_ai_api; invoke_ai_api()"]
            except (OSError, subprocess.TimeoutExpired):
                pass
        exe = shutil.which("invokeai-web")
        if exe:
            p = Path(exe).resolve()
            return p.parent.parent, [str(p)]
        return None, None

    def _command(self) -> tuple[list[str], Path]:
        root, prefix = self.discover()
        if not root or not prefix:
            raise RuntimeError(
                "InvokeAI was not found. Install the invokeai package (pip install invokeai) "
                "or set invokeai_dir to a venv containing invokeai-web.")
        parts = urlsplit(self.backend.endpoint)
        host = parts.hostname or "127.0.0.1"
        port = parts.port or 9090
        data_root = self.base_dir / "data" / "invokeai"
        data_root.mkdir(parents=True, exist_ok=True)
        # InvokeAI v5/v6's invokeai-web takes only --root/--config — host
        # and port live in invokeai.yaml inside the root (or INVOKEAI_HOST/
        # INVOKEAI_PORT env vars). Write Nexus's managed values so the
        # endpoint setting actually takes effect.
        config_file = data_root / "invokeai.yaml"
        try:
            existing = config_file.read_text(encoding="utf-8") if config_file.is_file() else ""
        except OSError:
            existing = ""
        managed = f"# Nexus-managed settings\nhost: {host}\nport: {port}\n"
        body = existing
        if "# Nexus-managed settings" in body:
            body = "\n".join(
                ln for ln in body.splitlines()
                if not ln.strip().startswith(("host:", "port:"))
            ) + "\n"
            body = body.split("# Nexus-managed settings")[0]
        if not any(ln.strip().startswith("schema_version:")
                   for ln in body.splitlines()):
            # InvokeAI's load_and_migrate_config KeyErrors on a missing
            # schema_version — a managed-only yaml (fresh root, or state
            # rebuilt after data loss) must seed one or the server never
            # starts. Prefer the version the installed InvokeAI stamped
            # into its example file; fall back to the oldest version so
            # InvokeAI's own migration upgrades it.
            body = (f"schema_version: {self._invokeai_schema_version(data_root)}\n"
                    + body)
        config_file.write_text(body.rstrip() + "\n" + managed, encoding="utf-8")
        self._apply_spandrel_guard(root)
        self._apply_load_file_guard(root)
        cmd = prefix + ["--root", str(data_root)]
        extra = list(getattr(self.config, "invokeai_extra_args", []) or [])
        cmd.extend(str(x) for x in extra)
        return cmd, root

    @staticmethod
    def _invokeai_schema_version(data_root: Path) -> str:
        try:
            for ln in (data_root / "invokeai.example.yaml").read_text(
                    encoding="utf-8", errors="ignore").splitlines():
                ln = ln.strip()
                if ln.startswith("schema_version:"):
                    return ln.split(":", 1)[1].strip() or "4.0.0"
        except OSError:
            pass
        return "4.0.0"

    @staticmethod
    def _apply_spandrel_guard(root: Path) -> None:
        """Work around an InvokeAI probe crash on multi-GB checkpoints.

        InvokeAI's install classifier runs *every* candidate config class,
        including ``Spandrel_Checkpoint_Config``, which fully loads the
        state dict via ``safetensors.torch.load_file``. On Windows that
        segfaults (access violation — no traceback, invokeai-web dies) for
        multi-GB SDXL checkpoints, so fleet installs killed the server
        (verified on 6.14.2 / torch 2.14.1+cu126). Real spandrel
        image-to-image nets are far smaller than 2 GiB, so size-rejecting
        before the load is a safe pre-check. Idempotent; lives in the
        installed venv so it is re-applied on every managed start.
        """
        rel = Path("invokeai/backend/model_manager/configs/spandrel.py")
        spandrel = root / "Lib" / "site-packages" / rel
        if not spandrel.is_file():
            for cand in sorted((root / "lib").glob("python*/site-packages/" + str(rel).replace("\\", "/"))):
                spandrel = cand
                break
        if not spandrel.is_file():
            return
        try:
            src = spandrel.read_text(encoding="utf-8")
        except OSError:
            return
        anchor = ("    @classmethod\n"
                  "    def _validate_spandrel_loads_model(cls, mod: ModelOnDisk) -> None:\n"
                  "        try:")
        if "NEXUS PATCH" in src or anchor not in src:
            return
        guard = ("    @classmethod\n"
                 "    def _validate_spandrel_loads_model(cls, mod: ModelOnDisk) -> None:\n"
                 "        # NEXUS PATCH: reject oversized files before the full state-dict\n"
                 "        # load below — safetensors.torch.load_file segfaults (access\n"
                 "        # violation) on multi-GB checkpoints on Windows, killing\n"
                 "        # invokeai-web mid-install. Spandrel image-to-image nets are\n"
                 "        # far smaller than 2 GiB.\n"
                 "        if mod.path.stat().st_size > 2 * 1024**3:\n"
                 "            raise NotAMatchError(\"file too large to be a SpandrelImageToImage model\")\n"
                 "        try:")
        try:
            spandrel.write_text(src.replace(anchor, guard, 1), encoding="utf-8")
            for pyc in (spandrel.parent / "__pycache__").glob("spandrel.*.pyc"):
                pyc.unlink(missing_ok=True)
        except OSError:
            pass

    @staticmethod
    def _apply_load_file_guard(root: Path) -> None:
        """Work around a safetensors segfault on multi-GB checkpoint loads.

        ``ModelOnDisk.load_state_dict`` is the funnel every install-time
        classifier (and any other state-dict consumer) uses. Its fast
        path — ``safetensors.torch.load_file`` — segfaults (exit 139, no
        traceback) on multi-GB .safetensors on this platform, killing
        invokeai-web mid-install after the hash step (verified live on
        6.14.2 / torch 2.14.1 — Juggernaut/RealVis-class checkpoints
        every time; a 2.8 GB single-tensor file loads fine). Per-tensor
        ``safe_open`` reads on the same file succeed for all 2526
        tensors, so above 2 GiB we read tensors one at a time — same
        result dict, slightly slower, and no monolithic mmap copy.
        Idempotent; lives in the installed venv like the spandrel guard.
        """
        rel = Path("invokeai/backend/model_manager/model_on_disk.py")
        mod_file = root / "Lib" / "site-packages" / rel
        if not mod_file.is_file():
            for cand in sorted((root / "lib").glob("python*/site-packages/" + str(rel).replace("\\", "/"))):
                mod_file = cand
                break
        if not mod_file.is_file():
            return
        try:
            src = mod_file.read_text(encoding="utf-8")
        except OSError:
            return
        anchor = ("            elif path.suffix.endswith(\".safetensors\"):\n"
                  "                if _is_sdnq_safetensors(path):\n"
                  "                    checkpoint = sdnq_sd_loader(path, compute_dtype=torch.float32)\n"
                  "                else:\n"
                  "                    checkpoint = safetensors.torch.load_file(path)")
        if "NEXUS PATCH" in src or anchor not in src:
            return
        guard = ("            elif path.suffix.endswith(\".safetensors\"):\n"
                 "                if _is_sdnq_safetensors(path):\n"
                 "                    checkpoint = sdnq_sd_loader(path, compute_dtype=torch.float32)\n"
                 "                elif path.stat().st_size > 2 * 1024**3:\n"
                 "                    # NEXUS PATCH: safetensors.torch.load_file segfaults on\n"
                 "                    # multi-GB checkpoint files on this platform (access\n"
                 "                    # violation, no traceback — verified on 6.14.2); per-tensor\n"
                 "                    # safe_open reads verified working for the same files.\n"
                 "                    with safe_open(str(path), framework=\"pt\", device=\"cpu\") as _nx_f:\n"
                 "                        checkpoint = {k: _nx_f.get_tensor(k) for k in _nx_f.keys()}\n"
                 "                else:\n"
                 "                    checkpoint = safetensors.torch.load_file(path)")
        try:
            mod_file.write_text(src.replace(anchor, guard, 1), encoding="utf-8")
            for pyc in (mod_file.parent / "__pycache__").glob("model_on_disk.*.pyc"):
                pyc.unlink(missing_ok=True)
        except OSError:
            pass

    def _managed_marker_path(self) -> Path:
        return self._resolve(str(getattr(self.config, "invokeai_logs_dir", ".agent/runtime"))) / "invokeai-managed.json"

    def _orphaned_managed_pid(self) -> int | None:
        try:
            data = json.loads(self._managed_marker_path().read_text(encoding="utf-8"))
            pid = int(data.get("pid") or 0)
            exe = str(data.get("exe") or "")
        except Exception:
            return None
        if pid <= 0 or not exe:
            return None
        return pid if self._pid_cmdline_matches(pid, exe) else None

    def _pid_cmdline_matches(self, pid: int, exe: str) -> bool:
        try:
            if os.name == "nt":
                out = subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
                    capture_output=True, text=True, timeout=10,
                    creationflags=no_window_flags(),
                encoding="utf-8", errors="replace").stdout
            else:
                raw = Path(f"/proc/{pid}/cmdline").read_bytes()
                out = raw.replace(b"\0", b" ").decode("utf-8", "replace")
        except Exception:
            return False
        return "invokeai" in out.lower() and exe.lower() in out.lower()

    def _kill_orphan(self, pid: int) -> None:
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                               capture_output=True, timeout=10,
                               creationflags=no_window_flags())
            else:
                os.kill(pid, 9)
        except Exception:
            pass

    def ensure_ready(self) -> None:
        with self._lock:
            healthy, detail = self.backend.health()
            if healthy and self._process is None:
                orphan = self._orphaned_managed_pid()
                if orphan is not None:
                    self._kill_orphan(orphan)
                    try:
                        self._managed_marker_path().unlink(missing_ok=True)
                    except OSError:
                        pass
                    healthy, detail = self.backend.health()
            if healthy:
                self.status.healthy = True
                self.status.state = "running" if self._process else "external"
                return
            auto_start = bool(getattr(self.config, "invokeai_auto_start", False))
            start_on_request = bool(getattr(self.config, "invokeai_start_on_image_request", True))
            if not auto_start and not start_on_request:
                self.status.healthy = False
                self.status.state = "external_offline"
                self.status.error = detail[:300]
                raise RuntimeError(f"InvokeAI backend is offline: {detail}")

            if not auto_start and start_on_request:
                root, _prefix = self.discover()
                if not root:
                    self.status.healthy = False
                    self.status.state = "setup_required"
                    self.status.error = (
                        "InvokeAI is offline and no local install was found. "
                        "Install the InvokeAI package or set invokeai_dir to a venv containing invokeai-web."
                    )
                    raise RuntimeError(self.status.error)

            self.start()

    def start(self) -> None:
        with self._lock:
            if self._process and self._process.poll() is None:
                healthy, _ = self.backend.health()
                if healthy:
                    self.status.state = "running"
                    self.status.healthy = True
                    return
                budget = 2 * max(10, int(getattr(self.config, "invokeai_startup_timeout", 180)))
                if time.time() - (self.status.started_at or time.time()) > budget:
                    self.stop()
            if not (self._process and self._process.poll() is None):
                self._spawn()
            self._wait_ready()

    def _spawn(self) -> None:
        cmd, cwd = self._command()
        logs = self._resolve(str(getattr(self.config, "invokeai_logs_dir", ".agent/runtime")))
        logs.mkdir(parents=True, exist_ok=True)
        log_path = logs / "invokeai.log"
        try:
            if log_path.exists() and log_path.stat().st_size > 8 * 1024 * 1024:
                log_path.write_bytes(log_path.read_bytes()[-4 * 1024 * 1024:])
        except OSError:
            pass
        self._log_handle = open(log_path, "a", encoding="utf-8", buffering=1)
        flags = 0
        if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
            flags = subprocess.CREATE_NO_WINDOW
        self._process = subprocess.Popen(
            cmd, cwd=str(cwd), stdout=self._log_handle,
            stderr=subprocess.STDOUT, text=True, creationflags=flags, encoding="utf-8", errors="replace")
        try:
            marker = self._managed_marker_path()
            marker.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(marker, json.dumps({"pid": self._process.pid, "exe": cmd[0]}))
        except OSError:
            pass
        self.status = InvokeRuntimeStatus(
            state="loading", pid=self._process.pid, managed=True, healthy=False,
            log_path=str(log_path), restarts=self.status.restarts,
            started_at=time.time())
        if self.leak_tracker is not None:
            try:
                self.leak_tracker.begin("invokeai")
            except Exception:
                pass

    def _wait_ready(self) -> None:
        started = self.status.started_at or time.time()
        budget = 2 * max(10, int(getattr(self.config, "invokeai_startup_timeout", 180)))
        deadline = started + budget
        last = ""
        while time.time() < deadline:
            if self._process is None or self._process.poll() is not None:
                code = self._process.returncode if self._process is not None else "?"
                self.status.state = "error"
                self.status.error = f"InvokeAI exited with code {code}; see {self.status.log_path}"
                self.status.pid = None
                if self.leak_tracker is not None:
                    try:
                        self.leak_tracker.end("invokeai")
                    except Exception:
                        pass
                raise RuntimeError(self.status.error)
            healthy, last = self.backend.health()
            if healthy:
                self.status.state = "running"
                self.status.healthy = True
                return
            if self.leak_tracker is not None:
                try:
                    self.leak_tracker.sample("invokeai")
                except Exception:
                    pass
            time.sleep(0.5)
        self.status.error = f"InvokeAI is still starting: {last[:300]}"
        raise TimeoutError(self.status.error)

    def stop(self) -> None:
        with self._lock:
            p = self._process
            self._process = None
            if p and p.poll() is None:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                                   capture_output=True, timeout=15,
                                   creationflags=no_window_flags())
                else:
                    p.terminate()
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    p.kill(); p.wait(timeout=3)
            if self._log_handle:
                try: self._log_handle.close()
                except Exception: pass
                self._log_handle = None
            try:
                self._managed_marker_path().unlink(missing_ok=True)
            except OSError:
                pass
            if self.leak_tracker is not None:
                try:
                    self.leak_tracker.end("invokeai")
                except Exception:
                    pass
            self.status.state = "stopped"
            self.status.healthy = False
            self.status.pid = None

    def evict_if_managed(self) -> bool:
        """Stop this backend only when Nexus owns the resident process.

        Two resident image servers can exhaust VRAM/RAM together, so a
        Nexus-managed backend is eligible for arbitration eviction — the
        next request brings it back via start_on_image_request. A
        user-owned external InvokeAI is never touched.
        Returns True when a Nexus-owned process was stopped."""
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                self.stop()
                return True
            orphan = self._orphaned_managed_pid()
            if orphan is None:
                return False
            self._kill_orphan(orphan)
            try:
                self._managed_marker_path().unlink(missing_ok=True)
            except OSError:
                pass
            self.status.state = "stopped"
            self.status.healthy = False
            self.status.pid = None
            return True

    def recover(self) -> None:
        with self._lock:
            self.status.restarts += 1
            self.stop()
            self.start()

    def probe(self) -> dict:
        healthy, detail = self.backend.health()
        self.status.healthy = healthy
        root, _prefix = self.discover()
        self.status.installed = root is not None
        self.status.installed_version = self.installed_version(root) \
            if root is not None else ""
        if self._process and self._process.poll() is not None:
            self.status.state = "error"
            self.status.pid = None
            self.status.error = f"InvokeAI exited with code {self._process.returncode}"
        elif self._process:
            self.status.state = "running" if healthy else "loading"
        elif healthy:
            self.status.state = "external"
        else:
            self.status.state = "external_offline" if not getattr(self.config, "invokeai_auto_start", False) else "stopped"
            self.status.error = detail[:300]
        return self.status.as_dict()
