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


def split_for_speech(text: str, limit: int = 240) -> list[str]:
    """Bound a speakable chunk to ~limit chars.

    Clause boundaries are preferred, then word wrap; a single oversized
    chunk becomes one monolithic TTS job whose synthesis latency shows up
    as dead air once playback drains the queue.
    """
    text = text.strip()
    out: list[str] = []
    while len(text) > limit:
        cut = 0
        for cm in _CLAUSE.finditer(text, 0, limit + 1):
            cut = cm.end()
        if not cut:
            sp = text.rfind(" ", 0, limit)
            cut = sp + 1 if sp > 0 else limit
        head, text = text[:cut].strip(), text[cut:].strip()
        if head:
            out.append(head)
    if text:
        out.append(text)
    return out


class SentenceStreamer:
    """feed(delta) -> [speakable sentences]; flush() -> tail.

    Complete lines are classified immediately (fence-aware). The trailing
    partial line also yields sentences once it contains a finished
    sentence boundary, so no newline is required to start speaking.
    """

    def __init__(self, filter_: SpeechTextFilter | None = None,
                 max_clause: int = 240, first_clause: int = 90) -> None:
        self.filter = filter_ or SpeechTextFilter()
        self.max_clause = max_clause
        # Speech should start as soon as the first clause is stable — waiting
        # for a full opening sentence makes voice lag visibly behind text.
        self.first_clause = first_clause
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
            # A run-on paragraph with no sentence end would otherwise sit in
            # _raw until the model finally punctuates — pull clause-stable
            # prefixes into the speakable buffer so speech keeps flowing.
            if len(self._raw) > self.max_clause:
                cut = 0
                for cm in _CLAUSE.finditer(self._raw, 0, self.max_clause + 1):
                    cut = cm.end()
                if not cut and len(self._raw) > self.max_clause * 2:
                    sp = self._raw.rfind(" ", 0, self.max_clause)
                    cut = sp + 1 if sp > 0 else 0
                if cut:
                    head, self._raw = self._raw[:cut], self._raw[cut:]
                    if self.filter.classify_line(head) == SPEAK:
                        self._text += self.filter._sanitize_prose(head)
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
            limit = self.first_clause if self._emitted == 0 else self.max_clause
            m = _SENT_END.search(buf)
            if m:
                sent, buf = buf[: m.end()].strip(), buf[m.end():]
                for part in split_for_speech(sent, limit):
                    out.append(part)
                    self._emitted += 1
                continue
            if force_all:
                for part in split_for_speech(buf.strip(), limit):
                    out.append(part)
                    self._emitted += 1
                buf = ""
                break
            if len(buf) > limit:
                # Emit the longest clause that fits; when nothing fits and
                # the buffer has run well past the limit, word-wrap so a
                # run-on sentence can't stall speech behind one huge TTS job.
                cut = 0
                for cm in _CLAUSE.finditer(buf, 0, limit + 1):
                    cut = cm.end()
                if not cut and len(buf) > limit * 2:
                    sp = buf.rfind(" ", 0, limit)
                    cut = sp + 1 if sp > 0 else limit
                if cut:
                    sent, buf = buf[:cut].strip(), buf[cut:]
                    if sent:
                        out.append(sent)
                        self._emitted += 1
                    continue
            break
        self._text = buf
        return out
