from __future__ import annotations

import hashlib
import os
import re
import shutil
import tarfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from ..jobs import JobManager
from ..procutil import no_window_flags

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


class _AuthScopedRedirect(urllib.request.HTTPRedirectHandler):
    """Redirect handler that never forwards Authorization (or any
    request credential header) across hosts — GitHub asset downloads
    302 to a signed CDN URL that must NOT receive the bearer token."""

    _STRIP = ("authorization", "proxy-authorization", "cookie")

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is None:
            return None
        try:
            old_host = urllib.parse.urlsplit(req.full_url).hostname
            new_host = urllib.parse.urlsplit(newurl).hostname
        except Exception:
            old_host = new_host = None
        if old_host and new_host and old_host.lower() != new_host.lower():
            for key in list(new.headers):
                if key.lower() in self._STRIP:
                    new.headers.pop(key, None)
            for key in list(getattr(new, "unredirected_hdrs", {}) or {}):
                if key.lower() in self._STRIP:
                    new.unredirected_hdrs.pop(key, None)
        return new


_AUTH_SAFE_OPENER = urllib.request.build_opener(_AuthScopedRedirect())


_WIN_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def _filename_from_url(url: str) -> str:
    name = url.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
    name = name.strip() or "download.bin"
    # Strip characters Windows forbids; guard reserved device names.
    name = "".join(c for c in name if c not in '<>:"|?*')[:120]
    stem = name.split(".")[0].lower()
    if stem in _WIN_RESERVED:
        name = name + "_"
    return name or "download.bin"


class UserDownloadManager:
    """Durable user-facing downloads — 'download <url>'.

    Streams to ``<dest>.part`` so interrupted downloads resume via HTTP
    Range; verifies size/SHA-256 before renaming into place; never
    overwrites an unrelated existing file; reports byte-level progress
    through the JobManager so the Tasks panel stays live.
    """

    def __init__(self, jobs: JobManager, download_dir: Path) -> None:
        self.jobs = jobs
        self.download_dir = Path(download_dir)
        self._cancel_flags: dict[str, threading.Event] = {}
        # Per-job request headers (e.g. GitHub asset auth) live only in
        # memory — never in job metadata, the ledger, or logs.
        self._job_headers: dict[str, dict[str, str]] = {}
        # Optional completion hook: fn(job_record_metadata) — AppState
        # registers finished downloads as artifacts through it.
        self.on_done: Callable[[dict], None] | None = None

    # -- API -----------------------------------------------------------------

    def start(self, url: str, dest: str = "", *,
              sha256: str = "", expected_size: int = 0,
              headers: dict | None = None,
              artifact: dict | None = None) -> dict[str, Any]:
        try:
            url = _safe_url(url)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        try:
            target = self._resolve_dest(str(dest or ""), url)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

        # Dedupe: a verified matching artifact is the download already done.
        if target.is_file():
            if sha256 and self._hash_path(target) == sha256.lower():
                return {"ok": True, "deduplicated": True, "verified": True,
                        "path": str(target),
                        "size": target.stat().st_size,
                        "sha256": sha256.lower()}
            if not sha256 and expected_size and \
                    target.stat().st_size == expected_size:
                return {"ok": True, "deduplicated": True, "verified": True,
                        "path": str(target), "size": expected_size}
            target = self._unique(target)

        meta = {"url": url, "dest": str(target), "phase": "queued"}
        if artifact:
            # Non-secret registration hints (kind, provenance, tool) —
            # consumed by the on_done hook when the bytes land.
            meta["artifact"] = dict(artifact)
        record = self.jobs.submit(
            "user_download", f"Download {target.name}",
            metadata=meta)
        flag = threading.Event()
        self._cancel_flags[record.id] = flag
        if headers:
            self._job_headers[record.id] = dict(headers)
        threading.Thread(
            target=self._run,
            args=(record.id, url, target, sha256.lower(),
                  int(expected_size or 0), flag),
            daemon=True).start()
        return {"ok": True, "job_id": record.id, "path": str(target),
                "url": url, "verified": False, "started": True}

    def status(self, job_id: str) -> dict[str, Any]:
        try:
            job = self.jobs.get(job_id)
        except KeyError:
            return {"ok": False, "error": f"no download job {job_id}"}
        meta = dict(job.metadata or {})
        return {"ok": True, "job_id": job.id, "state": job.state,
                "progress": job.progress, "status": job.status,
                "error": job.error, "url": meta.get("url", ""),
                "path": meta.get("dest", ""),
                "bytes_done": meta.get("bytes_done", 0),
                "bytes_total": meta.get("bytes_total", 0),
                "sha256": meta.get("sha256", ""),
                "verified": meta.get("verified", False)}

    def active(self) -> list[dict[str, Any]]:
        rows = []
        try:
            for job in self.jobs.list_jobs():
                if job.get("kind") == "user_download" and \
                        job.get("state") not in {"completed", "failed",
                                                 "cancelled"}:
                    rows.append({"job_id": job.get("id"),
                                 "state": job.get("state"),
                                 "status": job.get("status"),
                                 "dest": (job.get("metadata") or {})
                                 .get("dest", "")})
        except Exception:
            pass
        return rows

    def cancel(self, job_id: str = "") -> dict[str, Any]:
        """Cancel a specific job, or the most recent active download."""
        if not job_id:
            active = self.active()
            if not active:
                return {"ok": False, "error": "no download is running"}
            job_id = active[-1]["job_id"]
        flag = self._cancel_flags.get(job_id)
        if flag is not None:
            flag.set()
        try:
            self.jobs.cancel(job_id)
            return {"ok": True, "job_id": job_id, "cancelled": True}
        except KeyError:
            return {"ok": False, "error": f"no download job {job_id}"}

    # -- internals ------------------------------------------------------------

    def _resolve_dest(self, dest: str, url: str) -> Path:
        name = _filename_from_url(url)
        if not dest:
            return (self.download_dir / name).resolve()
        p = Path(dest)
        if not p.is_absolute() and not re.match(r"^[a-zA-Z]:[\\/]", dest):
            p = self.download_dir / p
        p = p.resolve()
        if str(dest).endswith(("/", "\\")) or p.is_dir():
            p = p / name
        # Never let a download target system roots.
        lowered = str(p).lower()
        for bad in (os.environ.get("SystemRoot", r"C:\Windows").lower(),
                    r"c:\program files"):
            if lowered.startswith(bad):
                raise ValueError(
                    f"refusing to download into a system directory: {p}")
        return p

    @staticmethod
    def _unique(dest: Path) -> Path:
        i = 1
        while True:
            cand = dest.with_name(f"{dest.stem} ({i}){dest.suffix}")
            if not cand.exists():
                return cand
            i += 1
            if i > 999:
                raise ValueError("could not find a unique file name")

    @staticmethod
    def _hash_path(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(8 * _CHUNK), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _progress(self, job_id: str, **meta: Any) -> None:
        try:
            self.jobs.update(
                job_id, state="running", status="downloading",
                metadata=self.jobs.get(job_id).metadata | meta)
        except KeyError:
            pass

    def _run(self, job_id: str, url: str, dest: Path, sha256: str,
             expected_size: int, flag: threading.Event) -> None:
        part = dest.with_name(dest.name + ".part")
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            self._fetch(job_id, url, part, expected_size, flag)
            if flag.is_set():
                raise _Cancelled()
            size = part.stat().st_size
            if expected_size and size != expected_size:
                raise ValueError(
                    f"size mismatch: got {size}, expected {expected_size}")
            self._progress(job_id, phase="verifying",
                           bytes_done=size, bytes_total=size)
            digest = self._hash_path(part)
            if sha256 and digest != sha256:
                raise ValueError(f"SHA-256 mismatch for {dest.name}")
            part.replace(dest)
            meta = self.jobs.get(job_id).metadata | {
                "phase": "finished", "verified": True,
                "size": size, "sha256": digest,
                "dest": str(dest)}
            self.jobs.update(
                job_id, state="completed", status="finished",
                progress=1.0, detail=str(dest), metadata=meta)
            if self.on_done is not None:
                try:
                    self.on_done(dict(meta))
                except Exception:
                    pass
        except _Cancelled:
            try:
                self.jobs.update(job_id, state="cancelled",
                                 status="cancelled",
                                 error="Download cancelled — partial "
                                       "file kept for resume")
            except KeyError:
                pass
        except Exception as exc:
            try:
                self.jobs.update(job_id, state="failed", status="failed",
                                 error=str(exc)[:300])
            except KeyError:
                pass
        finally:
            self._cancel_flags.pop(job_id, None)
            self._job_headers.pop(job_id, None)

    def _fetch(self, job_id: str, url: str, part: Path,
               expected_size: int, flag: threading.Event) -> None:
        have = part.stat().st_size if part.is_file() else 0
        if expected_size and have >= expected_size:
            return  # a previous attempt already fetched everything
        headers = {"User-Agent": "chat-nexus-download"}
        headers.update(self._job_headers.get(job_id) or {})
        if have:
            headers["Range"] = f"bytes={have}-"
        last_exc: Exception | None = None
        for attempt in range(3):
            if flag.is_set():
                raise _Cancelled()
            try:
                req = urllib.request.Request(url, headers=headers)
                resp = _AUTH_SAFE_OPENER.open(req, timeout=60)
                break
            except urllib.error.HTTPError as exc:
                # 416: stale .part bigger than the resource — restart clean.
                if exc.code == 416 and have:
                    part.unlink(missing_ok=True)
                    have = 0
                    headers.pop("Range", None)
                    continue
                last_exc = exc
                exc.close()
                if 400 <= exc.code < 500:
                    raise
            except Exception as exc:
                last_exc = exc
            time.sleep(min(2 ** attempt, 4))
        else:
            raise last_exc or ValueError("download failed")

        with resp:
            resumed = have > 0 and resp.status == 206
            done = have if resumed else 0
            remaining = int(resp.headers.get("Content-Length") or 0)
            total = expected_size or (done + remaining)
            last_emit = 0.0
            last_bytes, last_time = done, time.monotonic()
            mode = "ab" if resumed else "wb"
            with part.open(mode) as out:
                while True:
                    if flag.is_set():
                        raise _Cancelled()
                    read1 = getattr(resp, "read1", None)
                    chunk = read1(_CHUNK) if read1 else resp.read(_CHUNK)
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    now = time.monotonic()
                    if now - last_emit > 0.4:
                        rate = (done - last_bytes) / max(
                            now - last_time, 1e-6)
                        last_emit, last_bytes, last_time = now, done, now
                        frac = done / total if total else 0.0
                        self.jobs.update(
                            job_id, state="running",
                            status="downloading", progress=frac,
                            metadata=self.jobs.get(job_id).metadata | {
                                "phase": "downloading",
                                "bytes_done": done, "bytes_total": total,
                                "bytes_per_sec": int(rate)})


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
            try:
                # 416 = range unsatisfiable — a stale .part bigger than the real
                # resource. Drop it and restart clean rather than failing forever.
                if exc.code == 416 and have:
                    archive.unlink(missing_ok=True)
                    return self._download(job_id, url, archive, expected, flag)
                raise
            finally:
                exc.close()
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
                    [cand, "--version"], capture_output=True, text=True, timeout=15,
                    creationflags=no_window_flags())
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
                [tar, "-tf", str(archive)], capture_output=True, text=True, timeout=120,
                creationflags=no_window_flags())
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
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                creationflags=no_window_flags())
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
            if proc.stdout is not None and not proc.stdout.closed:
                proc.stdout.close()
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


def register_download_tools(registry, downloads: "UserDownloadManager") -> None:
    """User-facing durable downloads — url -> file with progress,
    resume, hash verification, and cancel. All HTTP(S)-only."""
    import json as _json
    from .base import ToolSpec

    def _cap(fn):
        def inner(args):
            return _json.dumps(fn(args), ensure_ascii=False)
        return inner

    registry.register(ToolSpec(
        "download_file",
        "Download a file from an HTTP(S) URL to the Downloads folder or a named path. Resumable, hash-verified, progress in Tasks. Never claims completion before the bytes land.",
        {"type": "object", "properties": {
            "url": {"type": "string"},
            "dest": {"type": "string",
                     "description": "Destination file path or directory (default: Downloads)"},
            "sha256": {"type": "string"},
            "expected_size": {"type": "integer"}},
         "required": ["url"]},
        "network.read",
        _cap(lambda a: downloads.start(
            str(a.get("url") or ""), str(a.get("dest") or ""),
            sha256=str(a.get("sha256") or ""),
            expected_size=int(a.get("expected_size") or 0))),
        category="utilities", capabilities=["download", "network"]))

    registry.register(ToolSpec(
        "download_status",
        "Check the state/progress of a download job by id.",
        {"type": "object", "properties": {"job_id": {"type": "string"}},
         "required": ["job_id"]},
        "network.read",
        _cap(lambda a: downloads.status(str(a.get("job_id") or ""))),
        category="utilities", capabilities=["download"]))

    registry.register(ToolSpec(
        "download_cancel",
        "Cancel a running download (partial file is kept for resume).",
        {"type": "object", "properties": {
            "job_id": {"type": "string",
                       "description": "omit to cancel the most recent download"}},
        },
        "network.read",
        _cap(lambda a: downloads.cancel(str(a.get("job_id") or ""))),
        category="utilities", capabilities=["download"]))
