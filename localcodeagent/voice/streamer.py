"""Incremental speech segmentation for token streams.

Feeds on assistant token deltas, tracks markdown fence state so code
fragments never reach the speech queue, and yields complete speakable
sentences as soon as they're stable. Never emits half words.
"""
from __future__ import annotations

import re

from .speech_filter import LIST_ITEM, SPEAK, SpeechTextFilter

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
        self._raw_fed = 0        # total raw chars ever fed
        self._text = ""          # speakable text awaiting sentence boundary
        self._in_fence = False
        self._emitted = 0
        self._skipped_blocks = 0
        # Parallel to each emitted string since the last feed/flush: the
        # raw-stream offset where its source text ends. The chat page uses
        # it to reveal display text exactly in step with audio playback.
        self._emit_spans: list[int] = []
        # List items are held until the run ends: a run of >= 3 is
        # structured data (recipe steps, ingredient lists) and collapses
        # to one spoken mention; shorter runs are spoken normally.
        self._list_run: list[str] = []
        self._list_mentioned = False

    @property
    def emitted_count(self) -> int:
        return self._emitted

    def feed(self, delta: str) -> list[str]:
        self._emit_spans = []
        self._raw += delta
        self._raw_fed += len(str(delta or ""))
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
                cls = self.filter.classify_line(head)
                if cls == SPEAK:
                    self._text += self.filter._sanitize_prose(head)
                elif cls == LIST_ITEM:
                    self._list_run.append(head)
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
                    cls = self.filter.classify_line(head)
                    if cls == SPEAK:
                        self._text += self.filter._sanitize_prose(head)
                    elif cls == LIST_ITEM:
                        self._list_run.append(head)
        out.extend(self._pop_ready())
        return out

    def flush(self) -> list[str]:
        self._emit_spans = []
        out: list[str] = []
        if self._raw:
            out.extend(self._handle_line(self._raw))
            self._raw = ""
        self._flush_list_run()
        out.extend(self._pop_ready(force_all=True))
        self._text = ""
        return out

    def pop_emit_spans(self) -> list[int]:
        """Raw-stream end offsets for the strings returned by the most
        recent feed()/flush() call, in order. ``len`` may be shorter than
        the emitted list when a chunk had no usable span."""
        spans, self._emit_spans = self._emit_spans, []
        return spans

    # -- internals ------------------------------------------------------
    def _handle_line(self, line: str) -> list[str]:
        if _FENCE.match(line):
            self._flush_list_run()
            out = self._pop_ready(force_all=True)
            self._text = ""
            self._in_fence = not self._in_fence
            if self._in_fence:
                self._skipped_blocks += 1
            return out
        if self._in_fence:
            return []
        cls = self.filter.classify_line(line)
        if cls == LIST_ITEM:
            self._list_run.append(line)
            return []
        self._flush_list_run()
        if cls == SPEAK:
            self._text += self.filter._sanitize_prose(line)
        return []

    def _flush_list_run(self) -> None:
        if not self._list_run:
            return
        items, self._list_run = self._list_run, []
        if len(items) >= self.filter.LIST_SUMMARIZE_MIN:
            if not self._list_mentioned:
                self._list_mentioned = True
                self._text += " The details are listed below."
            return
        for line in items:
            self._text += self.filter._sanitize_prose(line)

    def _pop_ready(self, force_all: bool = False) -> list[str]:
        out: list[str] = []
        buf = self._text

        def _emit(part: str) -> None:
            out.append(part)
            self._emitted += 1
            self._emit_spans.append(self._raw_fed - len(self._raw))

        while buf.strip():
            limit = self.first_clause if self._emitted == 0 else self.max_clause
            m = _SENT_END.search(buf)
            if m:
                sent, buf = buf[: m.end()].strip(), buf[m.end():]
                for part in split_for_speech(sent, limit):
                    _emit(part)
                continue
            if force_all:
                for part in split_for_speech(buf.strip(), limit):
                    _emit(part)
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
                        _emit(sent)
                    continue
            break
        self._text = buf
        return out
