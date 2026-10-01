"""Incremental speech segmentation for token streams.

Feeds on assistant token deltas, tracks markdown fence state so code
fragments never reach the speech queue, and yields complete speakable
sentences as soon as they're stable. Never emits half words.
"""
from __future__ import annotations

import re

from .speech_filter import SPEAK, SpeechTextFilter

_SENT_END = re.compile(r"[.!?…](?=\s|$)")
_FENCE = re.compile(r"^\s*(```|~~~)")
_CLAUSE = re.compile(r"[,;:—–]\s")


class SentenceStreamer:
    """feed(delta) -> [speakable sentences]; flush() -> tail.

    Complete lines are classified immediately (fence-aware). The trailing
    partial line also yields sentences once it contains a finished
    sentence boundary, so no newline is required to start speaking.
    """

    def __init__(self, filter_: SpeechTextFilter | None = None,
                 max_clause: int = 400) -> None:
        self.filter = filter_ or SpeechTextFilter()
        self.max_clause = max_clause
        self._raw = ""           # unprocessed deltas
        self._text = ""          # speakable text awaiting sentence boundary
        self._in_fence = False
        self._emitted = 0
        self._skipped_blocks = 0

    @property
    def emitted_count(self) -> int:
        return self._emitted

    def feed(self, delta: str) -> list[str]:
        self._raw += delta
        out: list[str] = []
        while "\n" in self._raw:
            line, self._raw = self._raw.split("\n", 1)
            out.extend(self._handle_line(line + "\n"))
        if not self._in_fence:
            # Sentence boundary inside the still-incomplete final line.
            m = _SENT_END.search(self._raw)
            while m:
                head, self._raw = self._raw[: m.end()], self._raw[m.end():]
                if _FENCE.match(head):
                    self._in_fence = True
                    break
                if self.filter.classify_line(head) == SPEAK:
                    self._text += self.filter._sanitize_prose(head)
                m = _SENT_END.search(self._raw)
        out.extend(self._pop_ready())
        return out

    def flush(self) -> list[str]:
        out: list[str] = []
        if self._raw:
            out.extend(self._handle_line(self._raw))
            self._raw = ""
        out.extend(self._pop_ready(force_all=True))
        self._text = ""
        return out

    # -- internals ------------------------------------------------------
    def _handle_line(self, line: str) -> list[str]:
        if _FENCE.match(line):
            out = self._pop_ready(force_all=True)
            self._text = ""
            self._in_fence = not self._in_fence
            if self._in_fence:
                self._skipped_blocks += 1
            return out
        if self._in_fence:
            return []
        if self.filter.classify_line(line) == SPEAK:
            self._text += self.filter._sanitize_prose(line)
        return []

    def _pop_ready(self, force_all: bool = False) -> list[str]:
        out: list[str] = []
        buf = self._text
        while buf.strip():
            m = _SENT_END.search(buf)
            if m:
                sent, buf = buf[: m.end()].strip(), buf[m.end():]
                if sent:
                    out.append(sent)
                    self._emitted += 1
                continue
            if force_all:
                sent = buf.strip()
                buf = ""
                if sent:
                    out.append(sent)
                    self._emitted += 1
                break
            if len(buf) > self.max_clause:
                m2 = _CLAUSE.search(buf)
                if m2:
                    sent, buf = buf[: m2.end()].strip(), buf[m2.end():]
                    if sent:
                        out.append(sent)
                        self._emitted += 1
                    continue
            break
        self._text = buf
        return out
