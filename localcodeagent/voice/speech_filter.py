"""SpeechTextFilter — decides what may be spoken aloud.

Parses a response into structural blocks (fence-aware, not a single giant
regex) and classifies each as SPEAK / SUMMARIZE / SKIP. Code, terminal
output, JSON, diffs, URLs, hashes, paths, traces and dumps never reach TTS;
they get replaced by a short natural mention or dropped. Then inline markup
is sanitized so prose reads naturally (no "underscore underscore open
paren").
"""
from __future__ import annotations

import re

SPEAK = "speak"
SUMMARIZE = "summarize"
SKIP = "skip"


class SpeechTextFilter:
    """Block classifier + prose sanitizer for TTS input."""

    FENCE_RE = re.compile(r"^\s*```")
    TILDE_FENCE_RE = re.compile(r"^\s*~~~")
    HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+")
    TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
    DIFF_RE = re.compile(r"^\s*(@@|\+{3}|-{3}|diff --git|index [0-9a-f]{7,})")
    URL_RE = re.compile(r"https?://\S+|www\.\S+", re.I)
    PATH_RE = re.compile(r"(?:[A-Za-z]:[\\/]|(?:[\w.\-]+[\\/]){1,}[\w.\-]+)")
    HASH_RE = re.compile(r"\b[0-9a-f]{32,64}\b", re.I)
    SHA_RE = re.compile(r"\b[0-9a-f]{7,40}\b")
    TRACE_RE = re.compile(r"(Traceback \(most recent call last\)|^\s*File \"|at \w+\.\w+\()")
    LOGISH_RE = re.compile(
        r"^\s*(\[?\d{4}-\d{2}-\d{2}|INFO|DEBUG|WARN(?:ING)?|ERROR|TRACE|"
        r"PASSED|FAILED|FAIL\b|ok(?=\s+\d)|not ok\b|===|>>>)")
    BASE64_RE = re.compile(r"\b[A-Za-z0-9+/]{80,}={0,2}\b")
    UUID_RUN_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
    JSONISH_LINE_RE = re.compile(r'^\s*[{}\[\]"\w].*[:{}\[\]]\s*,?\s*$')
    INLINE_CODE_RE = re.compile(r"`([^`]+)`")
    LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
    IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
    BOLD_RE = re.compile(r"(\*\*|__)(.+?)\1")
    ITALIC_RE = re.compile(r"(\*|_)([^*_]+)\1")
    LIST_MARKER_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
    QUOTE_RE = re.compile(r"^\s*>\s?")
    CMD_LINE_RE = re.compile(r"^\s*(\$ |>|PS C:|python3?\s+-\w|pip\s+install|npm\s+|git\s+\w|docker\s+|curl\s+|nvidia-smi|cd\s+\S)")
    PUNCT_RUN_RE = re.compile(r"[^\w\s]{4,}")

    def classify_line(self, line: str) -> str:
        s = line.strip()
        if not s:
            return SKIP
        if self.DIFF_RE.match(s) or self.TRACE_RE.search(s):
            return SKIP
        if self.CMD_LINE_RE.match(s):
            return SKIP
        if self.LOGISH_RE.match(s):
            return SKIP
        if self.TABLE_ROW_RE.match(s):
            return SUMMARIZE
        if self.BASE64_RE.search(s) or self.HASH_RE.search(s):
            return SKIP
        if self.PUNCT_RUN_RE.search(s) and not self.HEADING_RE.match(s):
            return SKIP
        # A line that is mostly URLs/hashes/paths is not prose — but only
        # when such tokens were actually present, and only if the leftover
        # lacks real words (keeps "See https://x for details" speakable).
        stripped = self.URL_RE.sub("", s)
        stripped = self.HASH_RE.sub("", stripped)
        stripped = self.PATH_RE.sub("", stripped)
        stripped = self.UUID_RUN_RE.sub("", stripped)
        if stripped != s and len(stripped.split()) < 3:
            return SKIP
        # JSON-ish dense line (many braces/colons, little spaces of prose).
        if self.JSONISH_LINE_RE.match(s) and s.count('"') + s.count(":") >= 3 \
                and not re.search(r"[a-z]{3,}\s+[a-z]{3,}\s+[a-z]{3,}", s, re.I):
            return SKIP
        return SPEAK

    def filter(self, text: str) -> str:
        """Return speakable text with unsafe blocks removed/summarized."""
        out: list[str] = []
        in_fence = False
        fence_lang = ""
        table_seen = 0
        skipped_kinds: set[str] = set()

        for line in text.split("\n"):
            if self.FENCE_RE.match(line) or self.TILDE_FENCE_RE.match(line):
                if not in_fence:
                    in_fence = True
                    fence_lang = line.strip("`~ \n")[:20] or "code"
                    skipped_kinds.add(fence_lang)
                else:
                    in_fence = False
                continue
            if in_fence:
                continue

            cls = self.classify_line(line)
            if cls == SKIP:
                continue
            if cls == SUMMARIZE:
                table_seen += 1
                continue
            out.append(line.rstrip())

        prose = "\n".join(out)
        prose = self._sanitize_prose(prose)
        if fence_lang or skipped_kinds or table_seen:
            if prose.strip():
                prose += " "
            bits = []
            if skipped_kinds:
                bits.append("I've included the code in the response.")
            if table_seen:
                bits.append("The details are shown in the table.")
            prose += " ".join(bits)
        return prose.strip()

    # -- inline cleanup -------------------------------------------------
    def _sanitize_prose(self, text: str) -> str:
        t = text
        t = self.IMG_RE.sub("", t)
        t = self.LINK_RE.sub(r"\1", t)
        t = self.URL_RE.sub("the link", t)
        t = self.HASH_RE.sub("the checksum", t)
        t = self.INLINE_CODE_RE.sub(lambda m: self._speakable_code(m.group(1)), t)
        t = self.BOLD_RE.sub(r"\2", t)
        t = self.ITALIC_RE.sub(r"\2", t)
        t = self.HEADING_RE.sub("", t)
        t = self.QUOTE_RE.sub("", t)
        t = self.LIST_MARKER_RE.sub("", t)
        t = self.PATH_RE.sub(lambda m: self._speakable_path(m.group(0)), t)
        # Collapse markdown table pipes left inside SUMMARIZE leftovers.
        t = re.sub(r"\|+", ", ", t)
        # Numbers/units read better with spaces normalized.
        t = re.sub(r"[ \t]{2,}", " ", t)
        t = re.sub(r"\n{3,}", "\n\n", t)
        return t

    def _speakable_code(self, snippet: str) -> str:
        s = snippet.strip()
        if not s:
            return ""
        # Very short identifiers can be spoken naturally.
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", s) and len(s) <= 30:
            return s.replace("_", " ").replace(".", " ")
        # Function call — name reads fine.
        m = re.match(r"([A-Za-z_][A-Za-z0-9_.]*)\s*\(", s)
        if m:
            return m.group(1).replace("_", " ")
        # Anything longer / symbolic — describe rather than dictate.
        return "the command shown"

    def _speakable_path(self, path: str) -> str:
        p = path.strip()
        if not p:
            return ""
        base = re.split(r"[\\/]", p)[-1]
        base = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", base)
        spoken = base.replace("_", " ").replace("-", " ").strip()
        return spoken if spoken else "the file"


# Back-compat alias matching the spec's naming.
SpeechFilter = SpeechTextFilter
