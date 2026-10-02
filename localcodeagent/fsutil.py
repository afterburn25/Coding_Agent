"""Filesystem helpers shared across subsystems."""
import os
import time
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
