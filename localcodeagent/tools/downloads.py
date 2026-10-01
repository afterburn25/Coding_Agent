from __future__ import annotations

import hashlib
import os
import shutil
import tarfile
import threading
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from ..jobs import JobManager

# Archive-based tool installs (manifest "install": {"method": "archive"}).
# Downloads a remote archive/file to a .part file, verifies SHA-256, extracts it
# under the install root, and reports two progress channels on the job record:
#   metadata.download_progress — remote fetch phase (bytes)
#   metadata.extract_progress  — extraction/finalization phase (files)
# plus metadata.current_file / current_path naming what is being written, and an
# overall record.progress (0..1) weighted across phases. Jobs emit through the
# JobManager on_change hook so the Tools page sees live updates over SSE.

_CHUNK = 1024 * 1024
# Overall weighting: download 70%, verify 5%, extract/finalize 25%.
_W_DOWNLOAD, _W_VERIFY, _W_EXTRACT = 0.70, 0.05, 0.25
# Progress emissions are throttled — each update rewrites the job ledger and
# pushes an SSE event, so per-file updates on multi-thousand-file archives would
# make extraction dramatically slower and flood the event bus.
_EMIT_INTERVAL = 0.25


def _safe_url(url: str) -> str:
    url = str(url or "").strip()
    if not url:
        raise ValueError("install.url is required")
    lowered = url.lower()
    if lowered.startswith("https://"):
        return url
    if lowered.startswith("http://"):
        host = lowered[len("http://"):].split("/")[0].split(":")[0]
        if host in {"localhost", "127.0.0.1", "::1"}:
            return url
    raise ValueError("Tool downloads require HTTPS (HTTP is allowed only for localhost testing).")


def _safe_member_name(name: str) -> str | None:
    """Normalize an archive member name; None if it would escape the dest dir."""
    p = PurePosixPath(str(name).replace("\\", "/"))
    if p.is_absolute() or ".." in p.parts:
        return None
    return str(p)


def _sevenzip_progress(on_file: Callable[[str, int], None]):
    """Build a py7zr ExtractCallback-compatible progress shim.

    py7zr >= 1.0 validates that the callback subclasses its ExtractCallback
    ABC, so this must subclass the real type (which is why the class is built
    lazily — py7zr is an optional dependency). Extra report_* aliases cover
    the method-name drift across py7zr releases.
    """
    try:
        from py7zr.callbacks import ExtractCallback
    except Exception:
        return None

    class _Progress(ExtractCallback):
        def __init__(self) -> None:
            self._written = 0

        def report_start_preparation(self) -> None:
            pass

        def report_start(self, processing_file_path: str, processing_bytes: int) -> None:
            on_file(str(processing_file_path or ""), self._written)

        def report_update(self) -> None:
            pass

        def report_end(self, processing_file_path: str, wrote_bytes: int) -> None:
            self._written += int(wrote_bytes or 0)

        def report_postprocess(self) -> None:
            pass

        def report_postprocessing(self) -> None:
            pass

        def report_warning(self, message: str) -> None:
            pass

        def report_finish(self) -> None:
            pass

    return _Progress()


class ToolDownloadManager:
    """Runs manifest archive installs on worker threads as tracked jobs."""

    def __init__(self, jobs: JobManager, install_root: Path) -> None:
        self.jobs = jobs
        self.install_root = Path(install_root).resolve()
        # Invoked with the tool id after an install job reaches a terminal state;
        # wired to the registry's install-status refresh.
        self.on_done: Callable[[str], None] | None = None
        self._lock = threading.Lock()
        self._active: dict[str, str] = {}  # tool_id -> job_id
        self._cancel_flags: dict[str, threading.Event] = {}

    # -- public API -----------------------------------------------------------

    def install(self, tool_id: str, display_name: str, install: dict[str, Any], *, version: str = "") -> dict[str, Any]:
        spec = dict(install or {})
        try:
            url = _safe_url(spec.get("url"))
            dest = self._resolve_dest(str(spec.get("dest") or tool_id))
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        size = int(spec.get("size_bytes") or 0) or None
        fmt = str(spec.get("format") or "").strip().lower() or self._guess_format(url)

        with self._lock:
            existing = self._active.get(tool_id)
            if existing:
                try:
                    job = self.jobs.get(existing)
                    if job.state not in {"completed", "failed", "cancelled"}:
                        if job.kind == "tool_remove":
                            return {"ok": False,
                                    "error": "a removal is running — wait for it to finish"}
                        return {"ok": True, "job_id": job.id, "tool": tool_id, "deduplicated": True}
                except KeyError:
                    pass
            record = self.jobs.submit(
                "tool_install",
                f"Install {display_name}",
                metadata={"tool": tool_id, "phase": "queued", "dest": str(dest)},
            )
            flag = threading.Event()
            self._active[tool_id] = record.id
            self._cancel_flags[record.id] = flag

        threading.Thread(
            target=self._run,
            args=(record.id, tool_id, display_name, url, dest, fmt, size,
                  str(spec.get("sha256") or ""), version, flag),
            daemon=True,
        ).start()
        return {"ok": True, "job_id": record.id, "tool": tool_id}

    def cancel(self, job_id: str) -> bool:
        flag = self._cancel_flags.get(job_id)
        if flag is not None:
            flag.set()
        try:
            self.jobs.cancel(job_id)
            return True
        except KeyError:
            return False

    def uninstall(self, tool_id: str, display_name: str,
                  install: dict[str, Any]) -> dict[str, Any]:
        """Delete an archive-installed tool's dest dir and partial download."""
        try:
            dest = self._resolve_dest(str(install.get("dest") or tool_id))
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        with self._lock:
            existing = self._active.get(tool_id)
            if existing:
                try:
                    if self.jobs.get(existing).state not in {
                            "completed", "failed", "cancelled"}:
                        return {"ok": False,
                                "error": "an install is running — cancel it first"}
                except KeyError:
                    pass
            record = self.jobs.submit(
                "tool_remove", f"Remove {display_name}",
                metadata={"tool": tool_id, "phase": "queued", "dest": str(dest)})
            self._active[tool_id] = record.id

        def _run() -> None:
            part = self.install_root / ".agent" / "downloads" / f"{tool_id}.part"
            try:
                self.jobs.update(record.id, state="running", status="removing",
                                 detail=str(dest))
                if dest.is_dir():
                    shutil.rmtree(dest)
                elif dest.exists():
                    dest.unlink()
                part.unlink(missing_ok=True)
                self.jobs.update(record.id, state="completed", status="finished",
                                 progress=1.0, detail=str(dest))
            except Exception as exc:
                try:
                    self.jobs.update(record.id, state="failed",
                                     error=str(exc)[:300])
                except KeyError:
                    pass
            finally:
                with self._lock:
                    self._active.pop(tool_id, None)
                if self.on_done is not None:
                    try:
                        self.on_done(tool_id)
                    except Exception:
                        pass

        threading.Thread(target=_run, daemon=True).start()
        return {"ok": True, "job_id": record.id, "tool": tool_id}

    def shutdown(self) -> None:
        for flag in self._cancel_flags.values():
            flag.set()

    # -- internals ------------------------------------------------------------

    def _resolve_dest(self, dest: str) -> Path:
        target = (self.install_root / dest).resolve()
        if not target.is_relative_to(self.install_root):
            raise ValueError("install.dest must stay inside the application directory")
        return target

    @staticmethod
    def _guess_format(url: str) -> str:
        lowered = url.split("?")[0].lower()
        if lowered.endswith(".7z"):
            return "7z"
        if lowered.endswith(".zip"):
            return "zip"
        if lowered.endswith((".tar.gz", ".tgz")):
            return "tar.gz"
        if lowered.endswith((".tar.bz2", ".tbz2")):
            return "tar.bz2"
        return "file"

    def _progress(self, job_id: str, *, phase: str, overall: float, **meta: Any) -> None:
        try:
            self.jobs.update(
                job_id,
                state="running",
                status=phase,
                progress=max(0.0, min(1.0, overall)),
                metadata=self.jobs.get(job_id).metadata | {"phase": phase} | meta,
            )
        except KeyError:
            pass

    def _is_cancelled(self, job_id: str, flag: threading.Event) -> bool:
        if flag.is_set():
            return True
        try:
            return self.jobs.get(job_id).state == "cancelled"
        except KeyError:
            return True

    def _run(self, job_id: str, tool_id: str, name: str, url: str, dest: Path,
             fmt: str, size: int | None, sha256: str, version: str,
             flag: threading.Event) -> None:
        # Per-tool stable .part name: a cancelled/failed download keeps its
        # partial file so a later install resumes via HTTP Range instead of
        # restarting a multi-GB fetch from zero.
        archive = self.install_root / ".agent" / "downloads" / f"{tool_id}.part"
        try:
            archive.parent.mkdir(parents=True, exist_ok=True)
            self._download(job_id, url, archive, size, flag)
            if self._is_cancelled(job_id, flag):
                raise _Cancelled()

            if sha256:
                self._progress(job_id, phase="verifying", overall=_W_DOWNLOAD,
                               download_progress=1.0, current_file=archive.name,
                               current_path=str(archive))
                if self._hash(archive, flag) != sha256.lower():
                    raise ValueError(f"SHA-256 mismatch for {archive.name}")
            if self._is_cancelled(job_id, flag):
                raise _Cancelled()

            if fmt == "file":
                dest.mkdir(parents=True, exist_ok=True)
                target = dest / (url.split("/")[-1].split("?")[0] or f"{tool_id}.bin")
                self._progress(job_id, phase="extracting", overall=_W_DOWNLOAD + _W_VERIFY,
                               download_progress=1.0, extract_progress=0.5,
                               current_file=target.name, current_path=str(target))
                shutil.move(str(archive), str(target))
            else:
                self._extract(job_id, archive, dest, fmt, flag)
            archive.unlink(missing_ok=True)

            marker = dest / ".chatnexus-version"
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(f"{version or 'unknown'}\n{url}\n", encoding="utf-8")
            self.jobs.update(job_id, state="completed", status="finished",
                             progress=1.0, detail=str(dest),
                             metadata=self.jobs.get(job_id).metadata | {"phase": "finished",
                                 "extract_progress": 1.0, "current_file": "",
                                 "current_path": str(dest)})
        except _Cancelled:
            # Keep the .part file — the next install resumes from it.
            try:
                self.jobs.update(job_id, state="cancelled", status="cancelled",
                                 error="Install cancelled")
            except KeyError:
                pass
        except Exception as exc:
            # Keep the .part file — the next install resumes from it.
            try:
                self.jobs.update(job_id, state="failed", status="failed",
                                 error=str(exc)[:300])
            except KeyError:
                pass
        finally:
            with self._lock:
                self._active.pop(tool_id, None)
                self._cancel_flags.pop(job_id, None)
            if self.on_done is not None:
                try:
                    self.on_done(tool_id)
                except Exception:
                    pass

    def _download(self, job_id: str, url: str, archive: Path,
                  expected: int | None, flag: threading.Event) -> None:
        have = archive.stat().st_size if archive.is_file() else 0
        if expected and have >= expected:
            return  # previous attempt already fetched the full payload
        headers = {"User-Agent": "chat-nexus-tool-installer"}
        if have:
            headers["Range"] = f"bytes={have}-"
        req = urllib.request.Request(url, headers=headers)
        try:
            resp = urllib.request.urlopen(req, timeout=60)
        except urllib.error.HTTPError as exc:
            # 416 = range unsatisfiable — a stale .part bigger than the real
            # resource. Drop it and restart clean rather than failing forever.
            if exc.code == 416 and have:
                archive.unlink(missing_ok=True)
                return self._download(job_id, url, archive, expected, flag)
            raise
        with resp:
            resumed = have > 0 and resp.status == 206
            done = have if resumed else 0
            remaining = int(resp.headers.get("Content-Length") or 0)
            total = expected or (done + remaining)
            last_emit = 0.0
            last_bytes = done
            last_time = time.monotonic()
            with archive.open("ab" if resumed else "wb") as out:
                while True:
                    if flag.is_set():
                        raise _Cancelled()
                    # read1 returns whatever is buffered so progress/cancel stay
                    # responsive on slow streams (read() waits for a full chunk).
                    read1 = getattr(resp, "read1", None)
                    chunk = read1(_CHUNK) if read1 else resp.read(_CHUNK)
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    now = time.monotonic()
                    if now - last_emit > 0.4:
                        rate = (done - last_bytes) / max(now - last_time, 1e-6)
                        eta = int((total - done) / rate) if total and rate > 0 else None
                        last_emit, last_bytes, last_time = now, done, now
                        self._progress(
                            job_id, phase="downloading",
                            overall=_W_DOWNLOAD * (done / total if total else 0.0),
                            download_progress=(done / total if total else 0.0),
                            bytes_done=done, bytes_total=total,
                            bytes_per_sec=int(rate), eta_seconds=eta,
                            current_file=archive.name, current_path=str(archive),
                        )

    def _hash(self, archive: Path, flag: threading.Event) -> str:
        digest = hashlib.sha256()
        with archive.open("rb") as f:
            while True:
                if flag.is_set():
                    raise _Cancelled()
                chunk = f.read(8 * _CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()

    def _stream_member(self, src, dest: Path, name: str, job_id: str,
                       flag: threading.Event, state: dict[str, Any]) -> bool:
        """Write one archive member; emit throttled byte-weighted progress."""
        safe = _safe_member_name(name)
        if safe is None or not safe or safe.endswith("/"):
            return False
        target = (dest / safe).resolve()
        if not target.is_relative_to(dest.resolve()):
            return False
        if flag.is_set():
            raise _Cancelled()
        target.parent.mkdir(parents=True, exist_ok=True)
        state["files_done"] += 1
        state["current_file"] = PurePosixPath(safe).name
        state["current_path"] = str(target)
        with target.open("wb") as out:
            while True:
                if flag.is_set():
                    raise _Cancelled()
                chunk = src.read(_CHUNK)
                if not chunk:
                    break
                out.write(chunk)
                state["bytes_done"] += len(chunk)
                self._maybe_emit_extract(job_id, dest, state)
        self._maybe_emit_extract(job_id, dest, state, force=True)
        return True

    def _maybe_emit_extract(self, job_id: str, dest: Path,
                            state: dict[str, Any], *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - state["last_emit"] < _EMIT_INTERVAL:
            return
        state["last_emit"] = now
        total = state["bytes_total"] or 0
        frac = state["bytes_done"] / total if total else (
            state["files_done"] / max(state["files_total"], 1))
        self._progress(
            job_id, phase="extracting",
            overall=_W_DOWNLOAD + _W_VERIFY + _W_EXTRACT * min(frac, 1.0),
            download_progress=1.0, extract_progress=min(frac, 1.0),
            files_done=state["files_done"], files_total=state["files_total"],
            bytes_done=state["bytes_done"], bytes_total=total,
            current_file=state["current_file"], current_path=state["current_path"],
        )

    def _extract_stream(self, job_id: str, dest: Path, members, open_member,
                        flag: threading.Event) -> None:
        state = {
            "bytes_total": sum(getattr(m, "file_size", getattr(m, "size", 0)) or 0
                               for m in members),
            "files_total": len(members),
            "bytes_done": 0, "files_done": 0,
            "current_file": "", "current_path": str(dest), "last_emit": 0.0,
        }
        self._maybe_emit_extract(job_id, dest, state, force=True)
        for m in members:
            name = getattr(m, "filename", None) or getattr(m, "name", "")
            src = open_member(m)
            if src is None:
                continue
            with src:
                self._stream_member(src, dest, name, job_id, flag, state)

    def _extract(self, job_id: str, archive: Path, dest: Path,
                 fmt: str, flag: threading.Event) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        if fmt == "zip":
            with zipfile.ZipFile(archive) as zf:
                members = [m for m in zf.infolist() if not m.is_dir()]
                self._extract_stream(job_id, dest, members, zf.open, flag)
            return
        if fmt in {"tar.gz", "tgz", "tar.bz2", "tbz2"}:
            mode = "r:gz" if fmt in {"tar.gz", "tgz"} else "r:bz2"
            with tarfile.open(archive, mode) as tf:
                members = [m for m in tf.getmembers() if m.isfile()]
                self._extract_stream(job_id, dest, members, tf.extractfile, flag)
            return
        if fmt == "7z":
            if self._extract_7z_native(job_id, archive, dest, flag):
                return
            self._extract_7z_py(job_id, archive, dest, flag)
            return
        raise ValueError(f"unsupported archive format '{fmt}'")

    # -- 7z: prefer the OS tar (bsdtar/libarchive, native speed, verbose file
    # listing for progress); fall back to py7zr when unavailable. -------------

    @staticmethod
    def _system_tar() -> str | None:
        """Return a libarchive-capable tar, or None.

        `which tar` may resolve to GNU tar (Git Bash/MSYS on PATH), which
        cannot read 7z — probe --version for the libarchive build. Windows
        System32 ships bsdtar; check it explicitly before PATH.
        """
        import subprocess
        candidates: list[str] = []
        if os.name == "nt":
            sys32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "tar.exe"
            candidates.append(str(sys32))
        for name in ("bsdtar", "tar"):
            found = shutil.which(name)
            if found:
                candidates.append(found)
        for cand in candidates:
            if not Path(cand).is_file():
                continue
            try:
                out = subprocess.run(
                    [cand, "--version"], capture_output=True, text=True, timeout=15)
            except (OSError, subprocess.TimeoutExpired):
                continue
            banner = f"{out.stdout or ''} {out.stderr or ''}".lower()
            if "libarchive" in banner or "bsdtar" in banner:
                return cand
        return None

    def _extract_7z_native(self, job_id: str, archive: Path, dest: Path,
                           flag: threading.Event) -> bool:
        tar = self._system_tar()
        if not tar:
            return False
        import subprocess
        try:
            listing = subprocess.run(
                [tar, "-tf", str(archive)], capture_output=True, text=True, timeout=120)
            if listing.returncode != 0:
                return False
        except (OSError, subprocess.TimeoutExpired):
            return False
        names = [n for n in listing.stdout.splitlines() if _safe_member_name(n)]
        state = {
            "bytes_total": 0, "files_total": len(names),
            "bytes_done": 0, "files_done": 0,
            "current_file": "", "current_path": str(dest), "last_emit": 0.0,
        }
        try:
            proc = subprocess.Popen(
                [tar, "-xvf", str(archive), "-C", str(dest)],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        except OSError:
            return False
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                if flag.is_set():
                    raise _Cancelled()
                name = line.strip().lstrip("x ").strip()
                safe = _safe_member_name(name)
                if not safe:
                    continue
                state["files_done"] += 1
                state["current_file"] = PurePosixPath(safe).name
                state["current_path"] = str(dest / safe)
                self._maybe_emit_extract(job_id, dest, state)
            rc = proc.wait(timeout=30)
        except _Cancelled:
            raise
        except Exception:
            return False
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        if rc != 0:
            return False
        state["files_done"] = len(names)
        state["bytes_done"] = state["bytes_total"]
        self._maybe_emit_extract(job_id, dest, state, force=True)
        return True

    def _extract_7z_py(self, job_id: str, archive: Path, dest: Path,
                       flag: threading.Event) -> None:
        try:
            import py7zr
        except ImportError as exc:
            raise RuntimeError(
                "7z archives need Windows tar.exe (built-in) or the py7zr package") from exc
        base = _W_DOWNLOAD + _W_VERIFY
        self._progress(job_id, phase="extracting", overall=base,
                       download_progress=1.0, current_file="", current_path=str(dest))
        with py7zr.SevenZipFile(archive, "r") as zf:
            names = [n for n in zf.getnames() if _safe_member_name(n)]
            state = {
                "bytes_total": 0, "files_total": len(names),
                "bytes_done": 0, "files_done": 0,
                "current_file": "", "current_path": str(dest), "last_emit": 0.0,
            }

            def on_file(name: str, _written: int) -> None:
                if flag.is_set():
                    raise _Cancelled()
                if not name:
                    return
                safe = _safe_member_name(name)
                state["files_done"] = min(state["files_done"] + 1, len(names))
                state["current_file"] = PurePosixPath(safe or name).name
                state["current_path"] = str(dest / (safe or ""))
                self._maybe_emit_extract(job_id, dest, state)

            callback = _sevenzip_progress(on_file)
            try:
                if callback is not None:
                    zf.extractall(path=dest, callback=callback)
                else:
                    zf.extractall(path=dest)
            except TypeError:
                # Older py7zr without callback support — extract, then report done.
                zf.extractall(path=dest)
        state["files_done"] = len(names)
        self._maybe_emit_extract(job_id, dest, state, force=True)


class _Cancelled(Exception):
    pass
