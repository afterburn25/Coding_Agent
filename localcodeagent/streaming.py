"""Stream coalescing for SSE token events.

llama.cpp deltas can arrive character-by-character; forwarding each one to the
UI creates an artificial typewriter effect and floods the event channel. The
coalescer accumulates tiny deltas into natural chunks — flushing on a size
threshold, a word/punctuation boundary once enough text is buffered, or an
elapsed interval — while never holding text longer than the flush interval
between deltas.
"""

from __future__ import annotations

import time


class TokenCoalescer:
    """Merge tiny model deltas into natural UI-sized chunks."""

    def __init__(
        self,
        *,
        min_chars: int = 24,
        max_chars: int = 64,
        flush_interval: float = 0.035,
        clock=time.monotonic,
    ) -> None:
        self.min_chars = max(1, int(min_chars))
        self.max_chars = max(self.min_chars, int(max_chars))
        self.flush_interval = max(0.0, float(flush_interval))
        self._clock = clock
        self._buf: list[str] = []
        self._buf_len = 0
        self._last_flush = clock()

    def feed(self, piece: str) -> str | None:
        """Accept a delta; return a merged chunk when it is time to flush."""
        if not piece:
            return None
        self._buf.append(piece)
        self._buf_len += len(piece)
        if self._buf_len >= self.max_chars:
            return self.flush()
        if self._buf_len >= self.min_chars:
            tail = self._buf[-1]
            if tail[-1] in " \t\n.,;:!?)]}\"'" or (self._clock() - self._last_flush) >= self.flush_interval:
                return self.flush()
        return None

    def flush(self) -> str:
        """Return all buffered text and reset the accumulation window."""
        if not self._buf_len:
            return ""
        out = "".join(self._buf)
        self._buf.clear()
        self._buf_len = 0
        self._last_flush = self._clock()
        return out
