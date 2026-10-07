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

from .vocalizations import canonicalize_vocals

SPEAK = "speak"
SUMMARIZE = "summarize"
SKIP = "skip"
LIST_ITEM = "list_item"


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
    LIST_MARKER_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+", re.M)
    LIST_ITEM_LINE_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
    # A contiguous run of at least this many list items is structured data
    # (recipes, steps, inventories) — summarized with a single spoken
    # mention instead of dictating every line. Shorter runs stay speakable
    # so conversational bullets still reach the user.
    LIST_SUMMARIZE_MIN = 3
    QUOTE_RE = re.compile(r"^\s*>\s?")
    # Status glyphs are verdicts — never read the glyph name ("check mark").
    # Items whose text already states the result are spoken as-is; opaque
    # items get the verdict appended. Applies to every spoken path (stream,
    # replay, speak tool) since all of them pass through _sanitize_prose.
    OK_GLYPH = "✅✔☑✓🟢\ufe0f"
    FAIL_GLYPH = "❌✗✖✘🔴\ufe0f"
    OK_LEAD_RE = re.compile(
        rf"(?m)^\s*(?:[{OK_GLYPH}]+|\[(?:x|✓|✔)\])\s*([^\n]*)$")
    FAIL_LEAD_RE = re.compile(rf"(?m)^\s*[{FAIL_GLYPH}]+\s*([^\n]*)$")
    PENDING_LEAD_RE = re.compile(r"(?m)^\s*\[ \]\s*([^\n]*)$")
    OK_INLINE_RE = re.compile(rf"[{OK_GLYPH}]+")
    FAIL_INLINE_RE = re.compile(rf"[{FAIL_GLYPH}]+")
    # Result words that make an appended verdict redundant — a checked item
    # whose text already states the pass is read as-is.
    PASS_SAID_RE = re.compile(
        r"\b(?:pass(?:ed|es|ing)?|ok(?:ay)?|healthy|read(?:y|ied)|running|"
        r"online|active|connected|available|working|green|succeed(?:ed|s|ing)?|"
        r"success(?:ful)?|normal(?:ly)?|verified|present|complet(?:e|ed|ion)|"
        r"done|enabled|good|fine|up|responding|reachable|loaded|detected|"
        r"found|operational|nominal)\b", re.I)
    FAIL_SAID_RE = re.compile(
        r"\b(?:fail(?:ed|s|ing|ure)?|errors?|down|offline|missing|broken|"
        r"unavailable|crash(?:ed|es|ing)?|dead|timed?\s*out|timeout|"
        r"refus(?:ed|al)|denied|unable|cannot|can't|bad|red|unhealthy|"
        r"disabled|absent|unreachable|not\s+\w+)\b", re.I)
    PENDING_SAID_RE = re.compile(
        r"\b(?:pending|queued|waiting|skipped|untested|unchecked|todo|"
        r"not\s+(?:yet\s+)?(?:run|done|complete[ds]?))\b", re.I)
    WARN_GLYPH_RE = re.compile(r"⚠️?")
    EMOJI_RUN_RE = re.compile(
        "[\U0001F000-\U0001FAFF☀-➿⬀-⯿️\ufe0f]+")
    ARROW_RE = re.compile(r"(?:→|⟶|->)")
    CMD_LINE_RE = re.compile(r"^\s*(\$ |>|PS C:|python3?\s+-\w|pip\s+install|npm\s+|git\s+\w|docker\s+|curl\s+|nvidia-smi|cd\s+\S)")
    PUNCT_RUN_RE = re.compile(r"[^\w\s]{4,}")
    # Hard-coded pronunciation — tokens TTS would otherwise read
    # letter-by-letter but that people say as words, and storage/frequency
    # units that expand to their full spoken name.
    WORD_ACRONYMS = {
        "RAM": "ram", "VRAM": "vee ram", "ROM": "rom",
        "GUI": "gooey", "JSON": "jason",
        "YAML": "yamel", "GIF": "gif", "JPG": "jay peg",
        "JPEG": "jay peg", "PNG": "ping", "WAV": "wave",
        "CUDA": "koo duh", "NVME": "en vee me", "NVMe": "en vee me",
        "LIDAR": "lie dar", "ASAP": "ay sap", "WiFi": "why fie",
        "Wi-Fi": "why fie", "macOS": "mac oh ess", "BIOS": "bye oss",
    }
    STORAGE_UNITS = {
        "KB": "kilobytes", "MB": "megabytes", "GB": "gigabytes",
        "TB": "terabytes", "PB": "petabytes",
    }
    # Symbol-bearing language/framework names TTS can't read literally —
    # "C#" must be "C sharp", not "C hash"/"C pound". Longest-first so
    # ASP.NET/VB.NET match before bare .NET.
    TECH_TOKENS = {
        "ASP.NET": "A S P dot net", "VB.NET": "V B dot net",
        "Node.js": "node jay ess", "NodeJS": "node jay ess",
        "C++": "C plus plus", "G++": "G plus plus",
        "C#": "C sharp", "F#": "F sharp", ".NET": "dot net",
    }
    TECH_TOKEN_RE = re.compile(
        r"ASP\.NET|VB\.NET|Node\.?js|NodeJS|"
        r"\b[CcFf]#\d*\.?\d*|\bC\+\+|\bG\+\+|\.NET\b")
    FREQ_UNITS = {
        "kHz": "kilohertz", "MHz": "megahertz", "GHz": "gigahertz",
    }
    TIME_UNITS = {
        "ms": "milliseconds", "ns": "nanoseconds", "µs": "microseconds",
    }
    # Raw vocalization spellings ("Mmm", "MMHMM", "HAHA") are normalized
    # to canonical lowercase tokens by canonicalize_vocals(); the
    # VocalizationEngine resolves them into TTS-safe renderings at speak
    # time. Two-letter "MM" stays literal (million, lens width).
    # "12 GB" / "3.0GHz" — number-attached units get singular/plural.
    NUM_UNIT_RE = re.compile(
        r"\b(\d+(?:\.\d+)?)\s*("
        r"KB|MB|GB|TB|PB|kHz|MHz|GHz|ms|ns|µs|fps|FPS"
        r")\b")
    ACRONYM_RE = re.compile(
        r"\b(" + "|".join(
            sorted((re.escape(k) for k in WORD_ACRONYMS), key=len, reverse=True))
        + r")\b")
    BARE_UNIT_RE = re.compile(
        r"\b(" + "|".join(
            sorted((re.escape(k)
                    for k in list(STORAGE_UNITS) + list(FREQ_UNITS)),
                   key=len, reverse=True))
        + r")\b")
    UNIT_WORDS = {**STORAGE_UNITS, **FREQ_UNITS,
                  "fps": "frames per second", "FPS": "frames per second",
                  **TIME_UNITS}

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
        if self.LIST_ITEM_LINE_RE.match(s):
            return LIST_ITEM
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
        list_seen = 0
        list_run: list[str] = []
        skipped_kinds: set[str] = set()

        def drop_orphan_label() -> None:
            # A short heading-style label ("Ingredients:", "Steps:") whose
            # block was summarized/skipped must not be spoken alone —
            # it names a structure the listener never hears. Sentence-like
            # labels ("Here's what I'd suggest:") stay: they carry meaning.
            if (
                out
                and out[-1].rstrip().endswith(":")
                and len(out[-1].strip().split()) <= 3
            ):
                out.pop()

        def flush_list() -> None:
            nonlocal list_seen
            if not list_run:
                return
            if len(list_run) >= self.LIST_SUMMARIZE_MIN:
                drop_orphan_label()
                list_seen += 1
            else:
                out.extend(x.rstrip() for x in list_run)
            list_run.clear()

        for line in text.split("\n"):
            if self.FENCE_RE.match(line) or self.TILDE_FENCE_RE.match(line):
                flush_list()
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
            if cls == LIST_ITEM:
                list_run.append(line)
                continue
            flush_list()
            if cls == SKIP:
                continue
            if cls == SUMMARIZE:
                drop_orphan_label()
                table_seen += 1
                continue
            out.append(line.rstrip())
        flush_list()

        prose = "\n".join(out)
        prose = self._sanitize_prose(prose)
        if fence_lang or skipped_kinds or table_seen or list_seen:
            if prose.strip():
                prose += " "
            bits = []
            if skipped_kinds:
                bits.append("I've included the code in the response.")
            if table_seen:
                bits.append("The details are shown in the table.")
            if list_seen:
                bits.append("The details are listed below.")
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
        # A leading status glyph is a verdict on the item — name the item,
        # then speak the verdict instead of the glyph name. When the item
        # text already states the result, no verdict is appended.
        t = self.OK_LEAD_RE.sub(self._lead_verdict(
            "operating within normal parameters", self.PASS_SAID_RE), t)
        t = self.FAIL_LEAD_RE.sub(self._lead_verdict(
            "failed to initialize", self.FAIL_SAID_RE), t)
        t = self.PENDING_LEAD_RE.sub(self._lead_verdict(
            "pending", self.PENDING_SAID_RE), t)
        # Inline glyphs mid-sentence become a short verdict — dropped when
        # the label before the glyph already states the result.
        t = self.OK_INLINE_RE.sub(
            self._inline_verdict("passed", self.PASS_SAID_RE), t)
        t = self.FAIL_INLINE_RE.sub(
            self._inline_verdict("failed", self.FAIL_SAID_RE), t)
        t = self.WARN_GLYPH_RE.sub(" — needs attention", t)
        t = self.ARROW_RE.sub(" to ", t)
        # Any emoji left unmapped is pictographic noise — drop it.
        t = self.EMOJI_RUN_RE.sub(" ", t)
        t = self.PATH_RE.sub(lambda m: self._speakable_path(m.group(0)), t)
        # Collapse markdown table pipes left inside SUMMARIZE leftovers.
        t = re.sub(r"\|+", ", ", t)
        t = self._pronounce(t)
        # Numbers/units read better with spaces normalized.
        t = re.sub(r"[ \t]{2,}", " ", t)
        t = re.sub(r"\n{3,}", "\n\n", t)
        return t

    @staticmethod
    def _lead_verdict(phrase: str, already: re.Pattern | None = None):
        """re.sub callback factory: '✅ GPU check' → 'GPU check — <phrase>'.
        When `already` matches the item text the result is self-stated and
        the glyph is simply dropped ('✅ Models verified' → 'Models verified')."""
        def repl(m):
            rest = m.group(1).strip()
            if not rest:
                return phrase.capitalize() + "."
            if already is not None and already.search(rest):
                return rest
            return f"{rest} — {phrase}"
        return repl

    @staticmethod
    def _inline_verdict(phrase: str, already: re.Pattern):
        """re.sub callback for mid-line glyphs. Appends ' — <phrase>' unless
        the label before the glyph already states the result."""
        def repl(m):
            head = m.string[: m.start()]
            seg = head[head.rfind("\n") + 1:]
            bpos = max(seg.rfind(c) for c in ".!?;,—:")
            if already.search(seg[bpos + 1:]):
                return ""
            return f" — {phrase}"
        return repl

    def _pronounce(self, t: str) -> str:
        """Expand number-attached units, then word-acronyms, then bare
        units — order matters so '64 GB RAM' reads 'sixty-four gigabytes
        ram', not '64 G B R A M'. Vocalization canonicalization runs
        first so 'MMM' becomes 'mmm' for the VocalizationEngine instead
        of spelled letters."""
        t = canonicalize_vocals(t)
        def tech(m):
            tok = m.group(0)
            # "c#" typed lowercase still means the language; a trailing
            # version ("C#9") reattaches with a space — "C sharp 9".
            ver = re.match(r"([CcFf]#)([\d.]+)", tok)
            if ver:
                return self.TECH_TOKENS[ver.group(1).capitalize()] \
                    + " " + ver.group(2).rstrip(".")
            return self.TECH_TOKENS.get(tok) or self.TECH_TOKENS.get(
                tok.upper()) or tok
        t = self.TECH_TOKEN_RE.sub(tech, t)
        def num_unit(m):
            n, u = m.group(1), m.group(2)
            word = self.UNIT_WORDS.get(u, u)
            if n in ("1", "1.0") and word.endswith("s"):
                word = word[:-1]
            return f"{n} {word}"
        t = self.NUM_UNIT_RE.sub(num_unit, t)
        t = self.ACRONYM_RE.sub(lambda m: self.WORD_ACRONYMS[m.group(1)], t)
        return self.BARE_UNIT_RE.sub(lambda m: self.UNIT_WORDS[m.group(1)], t)

    def _speakable_code(self, snippet: str) -> str:
        s = snippet.strip()
        if not s:
            return ""
        # A bare tech token ("`C#`", "`.NET`") names a language, not a
        # command — expand it like prose does.
        if self.TECH_TOKEN_RE.fullmatch(s):
            return self.TECH_TOKENS.get(s) or self.TECH_TOKENS.get(
                s.upper()) or s
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
