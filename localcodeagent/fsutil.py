"""Filesystem helpers shared across subsystems."""
import os
import time
import uuid
from pathlib import Path


def replace_with_retry(src, dst, attempts: int = 8, delay: float = 0.15) -> None:
    """os.replace with retry for transient destination locks.

    On Windows, MoveFileEx fails with WinError 32/5 when an antivirus,
    indexer, or a concurrent reader momentarily holds the target open.
    Retry with linear backoff so a momentary lock never fails a job.
    """
    last: OSError | None = None
    for i in range(max(1, attempts)):
        try:
            os.replace(src, dst)
            return
        except OSError as e:
            last = e
            if i < attempts - 1:
                time.sleep(delay * (i + 1))
    raise last  # type: ignore[misc]


def _tmp_for(path: Path) -> Path:
    """Per-writer tmp name — a shared `file.ext.tmp` collides when two
    threads persist the same file concurrently (one renames it out from
    under the other → WinError 2)."""
    return path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")


def atomic_write_text(path, text: str, encoding: str = "utf-8") -> None:
    """Write text to path atomically: unique tmp in the same dir, then replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_for(path)
    try:
        tmp.write_text(text, encoding=encoding)
        replace_with_retry(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def atomic_write_bytes(path, data: bytes) -> None:
    """Write bytes to path atomically: unique tmp in the same dir, then replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_for(path)
    try:
        tmp.write_bytes(data)
        replace_with_retry(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
