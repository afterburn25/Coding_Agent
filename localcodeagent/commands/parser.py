"""Strict slash-command parser.

A command exists ONLY when the first non-whitespace character is ``/``
and the token ``/[A-Za-z][A-Za-z0-9_-]*`` is followed by end-of-message
or whitespace. Everything else — mid-sentence slashes, URLs, Unix paths
(``/home/user``), API paths (``/api/models``) — is ordinary text and
must never be interpreted as a command.
"""
from __future__ import annotations

import re

from .types import ParsedCommand

_TOKEN_RE = re.compile(r"^/([A-Za-z][A-Za-z0-9_-]*)(?:\s+(.*))?$", re.S)


def parse_command(text: str) -> ParsedCommand | None:
    """Return a ParsedCommand for strict leading-slash syntax, else None."""
    s = str(text or "").lstrip()
    if not s.startswith("/"):
        return None
    m = _TOKEN_RE.match(s)
    if m is None:
        return None
    return ParsedCommand(
        name=m.group(1).lower(),
        raw_args=(m.group(2) or "").strip(),
        raw=s,
    )
