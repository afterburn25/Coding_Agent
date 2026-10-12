"""Durable Scheduler — one-time / interval / daily / weekly / monthly
schedules.

Schedules persist and survive restart. Daily/weekly/monthly use
wall-clock local time (computed fresh each fire), so DST transitions
re-anchor correctly instead of drifting by fixed 24h deltas.
"""
from __future__ import annotations

import calendar
import re
import threading
import time
import uuid
from typing import Any, Callable

KINDS = {"once", "interval", "daily", "weekly", "monthly"}

_WEEKDAY_NAMES = {
    "mon": 0, "monday": 0, "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "wednesday": 2, "thu": 3, "thur": 3, "thurs": 3,
    "thursday": 3, "fri": 4, "friday": 4, "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6}

_MONTH_NAMES = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3,
    "march": 3, "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6,
    "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9,
    "sept": 9, "september": 9, "oct": 10, "october": 10, "nov": 11,
    "november": 11, "dec": 12, "december": 12}

_MONTHS_NUM = ("", "january", "february", "march", "april", "may",
               "june", "july", "august", "september", "october",
               "november", "december")

_WORD_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
             "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
             "eleven": 11, "twelve": 12}

# Minute-of-hour words for 'ten to six'/'twenty past four' —
# numbers above twelve only ever appear on the minute side.
_MIN_NUM = {**_WORD_NUM, "thirteen": 13, "fourteen": 14,
            "fifteen": 15, "sixteen": 16, "seventeen": 17,
            "eighteen": 18, "nineteen": 19, "twenty": 20,
            "twenty-five": 25, "twenty five": 25, "thirty": 30}


def _hour_num(tok: str) -> int | None:
    if tok.isdigit():
        return int(tok)
    if tok == "noon":
        return 12
    if tok == "midnight":
        return 0
    return _WORD_NUM.get(tok)


def _min_num(tok: str) -> int | None:
    if tok.isdigit():
        return int(tok)
    return _MIN_NUM.get(tok)


_MIN_TOK = (r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|"
            r"ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|"
            r"seventeen|eighteen|nineteen|twenty|"
            r"twenty[- ]five|thirty)")


def _worded_clock(t: str) -> str:
    """'at half past three' → 'at 3:30', 'at quarter to five' →
    'at 4:45', 'at ten to 6' → 'at 5:50', 'at twenty past 4' →
    'at 4:20', 'at five pm' → 'at 5 pm'."""
    m = re.search(
        r"\b(one|two|three|four|five|six|seven|eight|nine|ten|"
        r"eleven|twelve)\s+(a\.?m\.?|p\.?m\.?)\b", t)
    if m:
        return (t[:m.start()] + f"{_WORD_NUM[m.group(1)]} "
                + m.group(2) + t[m.end():])
    m = re.search(r"\bquarter\s+(?:to|till?)\s+(\w+)\b", t)
    if m:
        htok = m.group(1).lower()
        if htok == "midnight":
            return t[:m.start()] + "11:45 pm" + t[m.end():]
        h = _hour_num(htok)
        if h is not None:
            return t[:m.start()] + f"{(h - 1) % 12 or 12}:45" + t[m.end():]
    m = re.search(r"\b(half|quarter)\s+past\s+(\w+)\b", t)
    if m:
        h = _hour_num(m.group(2))
        if h is not None:
            mins = "30" if m.group(1) == "half" else "15"
            if h == 0:
                return t[:m.start()] + f"12:{mins} am" + t[m.end():]
            return t[:m.start()] + f"{h}:{mins}" + t[m.end():]
    m = re.search(r"\b(" + _MIN_TOK + r")\s+(?:to|till?)\s+(\w+)\b",
                  t)
    if m:
        mn = _min_num(m.group(1))
        htok = m.group(2).lower()
        if mn is not None and 1 <= mn <= 59:
            if htok == "midnight":
                return (t[:m.start()] + f"11:{60 - mn:02d} pm"
                        + t[m.end():])
            if htok == "noon":
                return (t[:m.start()] + f"11:{60 - mn:02d}"
                        + t[m.end():])
            h = _hour_num(m.group(2))
            if h is not None and 1 <= h <= 12:
                return (t[:m.start()]
                        + f"{(h - 1) % 12 or 12}:{60 - mn:02d}"
                        + t[m.end():])
    m = re.search(r"\b(" + _MIN_TOK + r")\s+past\s+(\w+)\b", t)
    if m:
        mn = _min_num(m.group(1))
        htok = m.group(2).lower()
        if mn is not None and 1 <= mn <= 59:
            if htok == "midnight":
                return (t[:m.start()] + f"12:{mn:02d} am"
                        + t[m.end():])
            if htok == "noon":
                return t[:m.start()] + f"12:{mn:02d}" + t[m.end():]
            h = _hour_num(m.group(2))
            if h is not None and 1 <= h <= 12:
                return t[:m.start()] + f"{h}:{mn:02d}" + t[m.end():]
    # 'at half 6' — British 'half past' drops the 'past'. Guard the
    # duration shapes ('half an hour', 'half a day', 'half to…').
    m = re.search(r"\bhalf\s+(?!past\b|to\b|an?\b|hour|day|of\b)"
                  r"(\w+)\b", t)
    if m:
        h = _hour_num(m.group(1))
        if h is not None and 1 <= h <= 12:
            return t[:m.start()] + f"{h}:30" + t[m.end():]
    # 'at six thirty [pm]' / 'at seven-thirty' — worded hour +
    # worded minute, spaced or hyphenated.
    m = re.search(
        r"\b(one|two|three|four|five|six|seven|eight|nine|ten|"
        r"eleven|twelve)[-\s]+(" + _MIN_TOK + r")\b", t)
    if m:
        mn = _min_num(m.group(2))
        if mn is not None and 1 <= mn <= 59:
            return (t[:m.start()]
                    + f"{_WORD_NUM[m.group(1)]}:{mn:02d}"
                    + t[m.end():])
    return t


def _bad_clock(t: str) -> bool:
    """'at 25pm' / 'at 30 o'clock' / 'at 6:99' — out-of-range
    clocks that mktime would silently roll to another hour.
    Reject so the caller asks for a real time."""
    for m in re.finditer(
            r"\b(\d{1,3})(?::(\d{1,2}))?\s*"
            r"(?:a\.?m\.?|p\.?m\.?|am|pm|o'?clock)\b", t):
        if int(m.group(1)) > 12 or (m.group(2)
                                    and int(m.group(2)) > 59):
            return True
    for m in re.finditer(
            r"\bat\s+(\d{1,3}):(\d{1,2})\b", t):
        if int(m.group(1)) > 24 or int(m.group(2)) > 59:
            return True
    if re.search(r"\bat\s+(\d{1,3})\b", t):
        m = re.search(r"\bat\s+(\d{1,3})\b", t)
        if int(m.group(1)) > 24:
            return True
    return False


# 'a while'/'a bit'/'a sec' — conventional small delays a
# reminder may pin ('in a while' → +30m), but a timer's whole
# meaning is its duration, so these never satisfy a timer ask.
_VAGUE_DELAY_RE = re.compile(
    r"(?:an?\s+(?:little\s+)?(?:bit|moment|secs?|seconds?|while|"
    r"short\s+while|jiffy?|few|couple)(?:\s+or\s+so)?|"
    r"soon(?:ish)?|shortly)\s*$",
    re.IGNORECASE)


def is_vague_delay(text: str) -> bool:
    return bool(_VAGUE_DELAY_RE.search(str(text or "").strip()))


def parse_reminder_when(when: str, now: float) -> float | None:
    """Parse a reminder when-clause → wall-clock timestamp.

    Covers 'in 5 minutes', 'tomorrow [morning] [at 9am]',
    'at 14:30 [tomorrow]', 'tonight', 'this morning', 'friday
    [at 3]', 'next monday', 'next week', 'noon', 'midnight'.
    Returns None when nothing parseable is present."""
    t = " ".join(str(when or "").lower().split())
    if not t:
        return None
    t = re.sub(r"\bat\s+noon\b", "at 12:00 pm", t)
    t = re.sub(r"\bat\s+midnight\b", "at 12:00 am", t)
    # 'at 6 in the morning' — the daypart phrase is the meridiem.
    # Only when it follows a clock — 'in the morning' alone stays
    # a vague time the caller asks about honestly.
    t = re.sub(r"(\d{1,2}(?::\d{2})?)\s+in\s+the\s+morning\b",
               r"\1 am", t)
    t = re.sub(r"(\d{1,2}(?::\d{2})?)\s+in\s+the\s+afternoon\b",
               r"\1 pm", t)
    t = re.sub(r"(\d{1,2}(?::\d{2})?)\s+in\s+the\s+evening\b",
               r"\1 pm", t)
    t = re.sub(r"(\d{1,2}(?::\d{2})?)\s+at\s+night\b", r"\1 pm", t)
    # 'at 6 o'clock' — the redundant tail breaks the clock match.
    t = re.sub(r"\bo'?clock\b", "", t).strip()
    # 'at 7pm sharp' / 'at about 6' / 'around 6' — precision
    # hedges and fillers the clock grammar doesn't carry.
    t = re.sub(r"\b(?:sharp|exactly|precisely|on\s+the\s+dot)\b",
               "", t)
    t = re.sub(r"\bat\s+(?:about|around|approximately|roughly|"
               r"like)\s+", "at ", t)
    t = re.sub(r"(?<!in\s)(?<!for\s)\b(?:about|around|"
               r"approximately|roughly|like)\s+"
               r"(?=\d|noon|midnight|quarter|half)", "at ", t)
    # 'roughly at 6' — a hedge word before the preposition isn't
    # part of the clock.
    t = re.sub(
        r"\b(?:roughly|basically|maybe|probably|kinda|sorta|just|"
        r"like)\s+(?=(?:at|on|in|every|next)\s|tomorrow|tonight|"
        r"noon|midnight)", "", t)
    # 'at 7ish' — the colloquial -ish suffix on a digit clock.
    t = re.sub(r"\b(\d{1,2})\s*ish\b", r"\1", t)
    # 'at 8 and a half' — 'and a half' is the :30. Inside an
    # 'in <dur>' it's a compound duration, not a clock.
    if not re.match(r"in\b", t):
        t = re.sub(r"\b(\d{1,2})\s+and\s+a\s+half\b", r"\1:30", t)
    # 'on school days' / 'work days' — weekday cadence names.
    t = re.sub(r"\b(?:school|work|business)\s+days\b",
               "weekdays", t)
    # 'first thing [tomorrow/in the morning]' — the start of the
    # next day, pinned at 7:00.
    t = re.sub(
        r"\bfirst\s+thing(?:\s+tomorrow|\s+in\s+the\s+morning|"
        r"\s+in\s+the\s+am)?\b", "tomorrow at 7", t)
    # 'around lunchtime' / 'at dinner' / 'during lunch' — meal
    # dayparts carry conventional clocks.
    t = re.sub(
        r"\b(?:at|around|during)\s+lunch\s*(?:time)?\b",
        "at 12:00 pm", t)
    t = re.sub(
        r"\b(?:at|around|during)\s+(?:dinner|supper)\s*"
        r"(?:time)?\b", "at 6 pm", t)
    t = re.sub(
        r"\b(?:at|around|during)\s+breakfast\s*(?:time)?\b",
        "at 8 am", t)
    # 'midday'/'mid-morning' — conventional mid-daypart clocks;
    # 'dawn'/'dusk' stay pin-fails (variable twilight, not a
    # clock). Emit '12:00 pm' — the 'at noon' conversion above
    # already ran.
    t = re.sub(
        r"\b(?:at|around|during)?\s*midday\b", "at 12:00 pm", t)
    t = re.sub(
        r"\b(?:at|around|during)?\s*mid-morning\b", "at 10 am", t)
    # 'sometime tonight' / 'at some point tomorrow' — the vague
    # qualifier isn't part of the time.
    t = re.sub(r"\bat\s+some\s+point\s+(?:in\s+)?", "", t)
    t = re.sub(r"\bsometime\b\s*", "", t)
    # 'when it's 5pm' — the 'when it's' lead IS the clock phrase,
    # not an event trigger and not a task.
    t = re.sub(r"\bwhen\s+it'?s\s+(?=\d|noon|midnight)", "at ", t)
    # 'by 5pm' / 'no later than 6' — a deadline pins at the bound;
    # 'snooze by 20 minutes' never reaches here (that's the delta
    # grammar, not a clock lead).
    t = re.sub(
        r"\bno\s+later\s+than\s+|\bby\s+(?=\d|noon|midnight)",
        "at ", t)
    t = re.sub(
        r"\bby\s+(?=morning|afternoon|evening|night\b|tonight|"
        r"tomorrow|today|mon|tue|wed|thu|fri|sat|sun)",
        "", t)
    # 'before 8pm' — same deadline pin as 'by'; 'before the
    # meeting' keeps its 'the' so an event still refuses. noon/
    # midnight emit the converted clock — the 'at noon' rewrite
    # already ran. '5 minutes before 3pm' is a clock offset —
    # its own grammar below, don't touch the 'before'.
    if not re.search(
            r"\b(?:a|an|one|two|three|four|five|six|seven|eight|"
            r"nine|ten|few|couple|\d+(?:\.\d+)?)\s+(?:second|"
            r"minute|hour|day|week|month)s?\s+before\b", t):
        t = re.sub(r"\bbefore\s+noon\b", "at 12:00 pm", t)
        t = re.sub(r"\bbefore\s+midnight\b", "at 12:00 am", t)
        t = re.sub(r"\bbefore\s+(?=\d)", "at ", t)
        t = re.sub(
            r"\bbefore\s+(?=morning|afternoon|evening|night\b|"
            r"tonight|tomorrow|today|monday|tuesday|wednesday|"
            r"thursday|friday|saturday|sunday)", "", t)
    # 'the day after tomorrow' — a date anchor, not an event.
    t = re.sub(
        r"\b(?:the\s+)?day\s+after\s+tomorrow\b", "in 2 days", t)
    # 'next weekend' — a one-shot anchor (the coming Saturday),
    # not the recurring 'weekend' cadence; 'next' must not fall
    # into the task slot.
    t = re.sub(r"\bnext\s+weekend\b", "saturday", t)
    # 'the rest of the day' — a named end-of-day bound, not vague.
    t = re.sub(
        r"\b(?:the\s+)?rest\s+of\s+(?:the\s+|my\s+)?"
        r"(?:day|today|tonight)\b", "at 11:59 pm", t)
    # 'once per week' — 'per <period>' is just 'a <period>'; the
    # count-cadence grammar already holds 'once/twice a day'.
    t = re.sub(
        r"\bper\s+(second|minute|hour|day|week|month|year)s?\b",
        r"a \1", t)
    # 'a week from now' — 'X from now' is just 'in X'.
    t = re.sub(
        r"((?:a|an|one|two|three|four|five|six|seven|eight|nine|"
        r"ten|couple|few|\d+(?:\.\d+)?)\s+(?:second|minute|hour|"
        r"day|week|month|year)s?)\s+from\s+now\b",
        r"in \1", t)
    # 'at 5pm in 2 days' — same combined anchor clock-first;
    # reorder to 'in 2 days at 5pm'.
    t = re.sub(
        r"^at\s+((?:\d{1,2})(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?|am|"
        r"pm)?|noon|midnight)\s+in\s+"
        r"((?:a|an|one|two|three|four|five|six|seven|eight|nine|"
        r"ten|couple|few|\d+(?:\.\d+)?)\s+(?:second|minute|hour|"
        r"day|week|month)s?)\s*$",
        r"in \2 at \1", t)
    # 'later tonight' — 'later' is a hedge before a real daypart,
    # not a vagueness that should pin-fail.
    t = re.sub(
        r"\blater\s+(?=tonight|tomorrow|today|this\s+(?:morning|"
        r"afternoon|evening|week|month|year))", "", t)
    # 'at 930' / 'at 1030' / 'at 930pm' — a 3-4 digit bare clock
    # reads H:MM.
    t = re.sub(
        r"\bat\s+(\d{3,4})\s*(a\.?m\.?|p\.?m\.?)\b",
        lambda m: (f"at {m.group(1)[:-2]}:{m.group(1)[-2:]} "
                   + m.group(2)), t)
    t = re.sub(
        r"\bat\s+(\d{3,4})\b(?!\s*(?:a\.?m\.?|p\.?m\.?|:))",
        lambda m: f"at {m.group(1)[:-2]}:{m.group(1)[-2:]}", t)
    # 'a quarter after 3' — 'after' is 'past' in clock grammar;
    # a leading 'a' before quarter/half breaks the worded clock.
    t = re.sub(r"\bquarter\s+after\b", "quarter past", t)
    t = re.sub(
        r"\ba\s+(?=quarter\s+(?:past|to|till|til)\b|half\s+past)",
        "", t)
    # 'at seven tomorrow morning' — a worded hour + day + daypart
    # gets its meridiem from the daypart.
    t = re.sub(
        r"\bat\s+(one|two|three|four|five|six|seven|eight|nine|"
        r"ten|eleven|twelve)\s+(tomorrow|today)\s+"
        r"(morning|afternoon|evening|night)\b",
        lambda m: (f"at {m.group(1)} "
                   + ("am" if m.group(3) == "morning" else "pm")
                   + f" {m.group(2)}"), t)
    # 'every single day' — 'single' is an intensifier.
    t = re.sub(r"\bevery\s+single\s+", "every ", t)
    # 'the first of every month' — a worded ordinal is the day.
    # 'on the 15th of each month' — keep a single 'on'.
    t = re.sub(
        r"(?<!\bon\s)\bthe\s+(first|second|third|fourth|fifth|"
        r"sixth|seventh|eighth|ninth|tenth|(\d{1,2})(?:st|nd|rd|th)?)"
        r"\s+of\s+(?:every|each|the)\s+month\b",
        lambda m: "on the " + str({
            "first": 1, "second": 2, "third": 3, "fourth": 4,
            "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8,
            "ninth": 9, "tenth": 10}.get(m.group(1), m.group(2)))
        + " of every month", t)
    # 'at 5pm on the 20th' — a leading clock belongs after the
    # date phrase, matching 'on the 20th at 5pm'.
    t = re.sub(
        r"\bat\s+(\d{1,2}(?::\d{2})?\s*"
        r"(?:a\.?m\.?|p\.?m\.?|am|pm)?)\s+"
        r"(on\s+(?:(?:the\s+)?\d{1,2}(?:st|nd|rd|th)?|"
        r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
        r"\w*\s+\d{1,2}(?:st|nd|rd|th)?))",
        r"\2 at \1", t)
    # 'on 10/15' — a numeric date reads as october 15; 'at 7a' /
    # 'at 7p' shorthand meridiems; 'monday through friday' is the
    # weekdays cadence.
    t = re.sub(
        r"\b(\d{1,2})/(\d{1,2})\b",
        lambda m: (_MONTHS_NUM[int(m.group(1))] + " " + m.group(2))
        if 1 <= int(m.group(1)) <= 12
        and 1 <= int(m.group(2)) <= 31 else m.group(0), t)
    t = re.sub(r"\b(\d{1,2})\s*([ap])\.?(?=\s|$|[?,;])",
               r"\1 \2m", t)
    t = re.sub(
        r"\bmonday\s*(?:-\s*|\s+(?:through|thru|to|-)\s+)friday\b",
        "weekdays", t)
    # 'only on weekdays' — the filler isn't part of the cadence.
    t = re.sub(
        r"\bonly\s+(?=(?:on|at|in|every|next|tomorrow|tonight|"
        r"weekday|weekend|daily|weekly))", "", t)
    t = " ".join(t.split())
    t = _worded_clock(t)
    if _bad_clock(t):
        return None
    lt = time.localtime(now)

    # 'at the top of the hour' — the next :00 mark.
    if re.fullmatch(
            r"(?:at\s+)?(?:the\s+)?top\s+of\s+(?:the\s+)?hour", t):
        return time.mktime(
            (lt.tm_year, lt.tm_mon, lt.tm_mday,
             (lt.tm_hour + 1) % 24, 0, 0, -1, -1, -1)
            ) + (86400 if lt.tm_hour == 23 else 0)

    # '5 minutes before 3pm' / 'an hour after noon' — a clock
    # offset, not the event trigger the when-clause refused.
    m = re.fullmatch(
        r"(?:in\s+)?(\d+(?:\.\d+)?|an?|one|two|three|four|five|"
        r"six|seven|eight|nine|ten|eleven|twelve)\s*"
        r"(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h)\s+"
        r"(before|after)\s+(.+)", t)
    if m:
        base = (parse_reminder_when(m.group(4), now)
                or parse_reminder_when("at " + m.group(4), now))
        if base is not None:
            scale = {"s": 1, "m": 60, "h": 3600}[m.group(2)[0]]
            n_raw = m.group(1)
            n = (_WORD_NUM.get(n_raw)
                 or (1.0 if n_raw in ("a", "an") else None))
            if n is None:
                n = float(n_raw)
            d = n * scale
            return (base - d if m.group(3) == "before"
                    else base + d)

    def _meridian(hour: int, mer: str | None) -> int:
        mer = re.sub(r"[\s.]", "", mer or "")
        if mer == "pm" and hour < 12:
            return hour + 12
        if mer == "am" and hour == 12:
            return 0
        if not mer and hour <= 6:
            # Bare "at 3" in reminder speech is afternoon, not 3am.
            return hour + 12
        return hour

    def _clock(hour: int, minute: int, day_offset: int = 0,
               *, roll: bool = True) -> float:
        cand = time.mktime(
            (lt.tm_year, lt.tm_mon, lt.tm_mday + day_offset,
             hour, minute, 0, -1, -1, -1))
        if roll and cand <= now:
            cand += 86400
        return cand

    clock_tail = (r"(?:\s+at\s+(\d{1,2})(?::(\d{2}))?"
                  r"\s*(a\.?m\.?|p\.?m\.?|am|pm)?)?")

    _WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3,
              "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
              "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}
    m = re.fullmatch(
        r"in\s+(?:(?:about|around|roughly|like)\s+)?"
        r"(\d+(?:\.\d+)?|an?\s+couple\s+(?:of\s+)?|an?|one|two|"
        r"three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
        r"an?\s+few|few|half\s+an?|an?\s+half)\s*"
        r"(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|"
        r"days?|d|weeks?|w|months?)(?:\s+or\s+so)?"
        + clock_tail
        + r"(?:\s+at\s+(noon|midnight))?", t)
    if m:
        n_raw = m.group(1)
        n = (0.5 if "half" in n_raw else
             2.0 if "couple" in n_raw else
             3.0 if "few" in n_raw else
             _WORDS.get(n_raw, None))
        if n is None:
            n = float(n_raw)
        unit = m.group(2).rstrip("s")
        if unit == "month":
            # A real calendar advance — a fixed 30d span drifts the
            # landing date off the user's month boundary.
            import calendar as _cal
            lt2 = time.localtime(now)
            mon0 = lt2.tm_mon - 1 + int(n)
            yr, mon = lt2.tm_year + mon0 // 12, mon0 % 12 + 1
            day = min(lt2.tm_mday, _cal.monthrange(yr, mon)[1])
            ts = time.mktime(
                (yr, mon, day, lt2.tm_hour, lt2.tm_min, 0,
                 -1, -1, -1))
        else:
            scale = {"s": 1, "sec": 1, "second": 1, "m": 60,
                     "min": 60, "minute": 60, "h": 3600, "hr": 3600,
                     "hour": 3600, "d": 86400, "day": 86400,
                     "w": 604800, "week": 604800}[unit]
            ts = now + n * scale
        if m.group(3) or m.group(6):
            # 'in 2 days at 3pm' — the duration picks the date,
            # the clock picks the time on it.
            lt3 = time.localtime(ts)
            if m.group(6):
                hr2, mn2 = (12, 0) if m.group(6) == "noon" else (0, 0)
            else:
                hr2 = _meridian(int(m.group(3)), m.group(5))
                mn2 = int(m.group(4) or 0)
            ts2 = time.mktime(
                (lt3.tm_year, lt3.tm_mon, lt3.tm_mday,
                 hr2, mn2, 0, -1, -1, -1))
            return ts2 + 86400 if ts2 <= now else ts2
        return ts
    # 'in 15' / 'in about 20' — a bare number counts down minutes;
    # 'in 2015' (4+ digits) is not a delay.
    m = re.fullmatch(
        r"in\s+(?:(?:about|around|roughly|like)\s+)?"
        r"(\d{1,3}(?:\.\d+)?)", t)
    if m:
        return now + float(m.group(1)) * 60
    # 'in a bit' / 'in a moment' / 'in a while' — vague small
    # delays; pin conventional spans rather than narrate.
    m = re.fullmatch(
        r"in\s+an?\s+(little\s+bit|bit|moment|sec|second|while|"
        r"short\s+while|little\s+while|jiffy|jiff)", t)
    if m:
        return now + {"bit": 600.0, "little bit": 600.0,
                      "jiffy": 60.0, "jiff": 60.0,
                      "moment": 120.0, "sec": 60.0, "second": 60.0,
                      "while": 1800.0,
                      "little while": 1200.0,
                      "short while": 900.0}[m.group(1)]
    # 'soon'/'shortly'/'in a sec-ish' — bare vague-delay words.
    m = re.fullmatch(
        r"(?:in\s+)?(?:a\s+)?"
        r"(pretty\s+|real\s+|very\s+|so\s+)?"
        r"(soon|shortly)(?:ish)?", t)
    if m:
        return now + (1800.0 if m.group(2) == "soon" else 1200.0)
    # 'in an hour and twenty minutes' — compound units sum.
    m = re.fullmatch(
        r"in\s+(\d+|an?|one|two|three|four|five|six|seven|eight|"
        r"nine|ten|eleven|twelve)\s*"
        r"(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?)"
        r"\s+and\s+(?:an?\s+)?"
        r"(\d+|one|two|three|four|five|six|seven|eight|nine|ten|"
        r"eleven|twelve|twenty|thirty|forty|fifty)\s*"
        r"(seconds?|secs?|minutes?|mins?)", t)
    if m:
        _NUM2 = {**_WORDS, "twenty": 20, "thirty": 30,
                 "forty": 40, "fifty": 50}
        n1 = _WORDS.get(m.group(1)) or float(
            m.group(1) if m.group(1)[0].isdigit() else 1)
        n2 = _NUM2.get(m.group(3)) or float(m.group(3))
        scale1 = {"s": 1, "m": 60, "h": 3600, "d": 86400,
                  "w": 604800}[m.group(2)[0]]
        scale2 = {"s": 1, "m": 60}[m.group(4)[0]]
        return now + n1 * scale1 + n2 * scale2
    # 'in 2 and a half hours' — compound worded fraction.
    m = re.fullmatch(
        r"in\s+(\d+(?:\.\d+)?|one|two|three|four|five|six|seven|"
        r"eight|nine|ten|eleven|twelve)\s+and\s+a\s+half\s+"
        r"(minutes?|mins?|hours?|hrs?|days?)", t)
    if m:
        n = (_WORDS.get(m.group(1)) if not m.group(1)[0].isdigit()
             else float(m.group(1)))
        scale = {"m": 60, "h": 3600, "d": 86400}[m.group(2)[0]]
        return now + (n + 0.5) * scale
    # 'in an hour and a half' — the fraction trails the unit.
    m = re.fullmatch(
        r"in\s+an?\s+(minutes?|mins?|hours?|hrs?|days?)\s+"
        r"and\s+a\s+half\b", t)
    if m:
        scale = {"m": 60, "h": 3600, "d": 86400}[m.group(1)[0]]
        return now + 1.5 * scale
    # 'in a quarter of an hour' / 'in three quarters of an hour'.
    m = re.fullmatch(
        r"in\s+(?:an?\s+)?(quarter|three\s+quarters?)\s+"
        r"(?:of\s+an?\s+)?(minutes?|mins?|hours?|hrs?)", t)
    if m:
        frac = 0.25 if m.group(1) == "quarter" else 0.75
        scale = {"m": 60, "h": 3600}[m.group(2)[0]]
        return now + frac * scale
    # '(the) day after tomorrow' — a single +2d anchor; without
    # it the splitter banked 'day after' as the task.
    if re.fullmatch(
            r"(?:the\s+|a\s+)?days?\s+after\s+tomorrows?", t):
        return _clock(9, 0, 2, roll=False)
    # 'on the 15th [of march]' / 'on march 3rd' — calendar dates.
    m = re.fullmatch(
        r"(?:on\s+)?the\s+(\d{1,2})(?:st|nd|rd|th)?"
        r"(?:\s+of\s+(\w+))?(?:\s+at\s+(\d{1,2})(?::(\d{2}))?\s*"
        r"(a\.?m\.?|p\.?m\.?|am|pm)?)?", t)
    if m:
        month = (_MONTH_NAMES.get(m.group(2))
                 if m.group(2) else None)
        if m.group(2) is None or month is not None:
            hour = (_meridian(int(m.group(3)), m.group(5))
                    if m.group(3) else 9)
            minute = int(m.group(4) or 0)
            for mo in range(0, 15):
                year = (lt.tm_year
                        + (lt.tm_mon - 1 + mo) // 12)
                mon = (lt.tm_mon - 1 + mo) % 12 + 1
                if month is not None and mon != month:
                    continue
                mday = min(int(m.group(1)),
                           calendar.monthrange(year, mon)[1])
                cand = time.mktime(
                    (year, mon, mday, hour, minute, 0, 0, 0, -1))
                if cand > now:
                    return cand
                if month is not None:
                    continue
    m = re.fullmatch(
        r"(?:on\s+)?(\w+)\s+(\d{1,2})(?:st|nd|rd|th)?"
        + clock_tail, t)
    if m:
        month = _MONTH_NAMES.get(m.group(1))
        if month is not None:
            hour = (_meridian(int(m.group(3)), m.group(5))
                    if m.group(3) else 9)
            minute = int(m.group(4) or 0)
            for year in (lt.tm_year, lt.tm_year + 1):
                mday = min(int(m.group(2)),
                           calendar.monthrange(year, month)[1])
                cand = time.mktime(
                    (year, month, mday, hour, minute, 0,
                     0, 0, -1))
                if cand > now:
                    return cand
            return None
    m = re.fullmatch(
        r"(tomorrow|today)(?:\s+(morning|afternoon|evening|night))?"
        + clock_tail, t)
    if m:
        day_w, part, hr, mn, mer = m.groups()
        minute = 0
        if hr is not None:
            hour = _meridian(int(hr), mer)
            if not mer and part:
                # 'tomorrow morning at 6' — the daypart supplies
                # the meridiem; the bare-hour PM hack would pin
                # 18:00 (and 'evening at 11' would pin 11 AM).
                hour = (int(hr) if part == "morning"
                        else hour + 12 if hour < 12 else hour)
            minute = int(mn or 0)
        elif part:
            hour = {"morning": 9, "afternoon": 15,
                    "evening": 19, "night": 21}[part]
        else:
            hour = 9
        # 'today at noon' — same-day anchor; a passed clock rolls
        # to tomorrow like any bare 'at T'.
        if day_w == "today":
            return _clock(hour, minute, 0)
        return _clock(hour, minute, 1, roll=False)
    # 'tonight at 8' / 'this evening at 7' — a same-day anchor
    # word supplies the date and the default meridiem; an explicit
    # clock overrides the daypart's conventional hour.
    m = re.fullmatch(
        r"(tonight|this\s+night|this\s+evening|this\s+afternoon|"
        r"this\s+morning|in\s+the\s+(morning|afternoon|evening))"
        + clock_tail, t)
    if m:
        part_w = re.search(
            r"morning|afternoon|evening|night", m.group(1)).group(0)
        if m.group(3):
            hour = _meridian(int(m.group(3)), m.group(5))
            if not m.group(5):
                hour = (int(m.group(3)) if part_w == "morning"
                        else hour + 12 if hour < 12 else hour)
            minute = int(m.group(4) or 0)
        else:
            hour = {"morning": 9, "afternoon": 15,
                    "evening": 19, "night": 21}[part_w]
            minute = 0
        return _clock(hour, minute, 0)
    if re.fullmatch(r"(?:this\s+night|tonight|this\s+evening)", t):
        return _clock(21 if "night" in t else 19, 0)
    m = re.fullmatch(r"this\s+(morning|afternoon|evening)", t)
    if m:
        return _clock({"morning": 9, "afternoon": 15,
                       "evening": 19}[m.group(1)], 0)
    m = re.fullmatch(r"in\s+the\s+(morning|afternoon|evening)", t)
    if m:
        return _clock({"morning": 9, "afternoon": 15,
                       "evening": 19}[m.group(1)], 0)
    if t == "noon":
        return _clock(12, 0)
    if t == "midnight":
        return _clock(0, 0)
    if t == "next week":
        return _clock(9, 0, 7, roll=False)
    # 'at 6:30 pm friday' — clock-first weekday. Without this the
    # splitter took bare 'friday' as the when and banked 'at 6:30
    # pm' as the task.
    m = re.fullmatch(
        r"at\s+(\d{1,2})(?::(\d{2}))?\s*"
        r"(a\.?m\.?|p\.?m\.?|am|pm)?\s+(?:on\s+)?(\w+)", t)
    if m:
        wd = _WEEKDAY_NAMES.get(m.group(4))
        if wd is not None:
            hour = _meridian(int(m.group(1)), m.group(3))
            minute = int(m.group(2) or 0)
            delta = (wd - lt.tm_wday) % 7
            cand = _clock(hour, minute, delta, roll=False)
            if cand <= now:
                cand = _clock(hour, minute, delta + 7, roll=False)
            return cand
    m = re.fullmatch(
        r"(?:(on|next|this|coming)\s+)?(\w+?)" + clock_tail, t)
    if m:
        wd = _WEEKDAY_NAMES.get(m.group(2))
        if wd is not None and m.group(1) == "next":
            # 'next monday' — the coming week's occurrence, never
            # today-or-tomorrow.
            delta = (wd - lt.tm_wday) % 7
            return _clock(
                _meridian(int(m.group(3)), m.group(5))
                if m.group(3) else 9,
                int(m.group(4) or 0),
                delta if delta else 7, roll=False)
        if wd is not None:
            hr, mn, mer = m.group(3), m.group(4), m.group(5)
            hour = _meridian(int(hr), mer) if hr else 9
            delta = (wd - lt.tm_wday) % 7
            cand = _clock(hour, int(mn or 0), delta, roll=False)
            if cand <= now:
                cand = _clock(hour, int(mn or 0), delta + 7,
                              roll=False)
            return cand
    # 'at 5 tomorrow evening' — the trailing daypart supplies
    # the meridiem for a bare hour.
    m = re.fullmatch(
        r"(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*"
        r"(a\.?m\.?|p\.?m\.?|am|pm)?"
        r"\s+(tomorrow|tonight|today|\w+day)\s+"
        r"(morning|afternoon|evening|night)", t)
    if m:
        hr, mn, mer = int(m.group(1)), int(m.group(2) or 0), m.group(3)
        if mer:
            hour = _meridian(hr, mer)
        elif m.group(5) == "morning":
            hour = hr          # 'at 5 tomorrow morning' → 05:00
        elif hr < 12:
            hour = hr + 12     # 'at 5 tomorrow evening' → 17:00
        else:
            hour = hr
        dayword = m.group(4)
        wd = _WEEKDAY_NAMES.get(dayword)
        if dayword == "tomorrow":
            return _clock(hour, mn, 1, roll=False)
        if dayword == "tonight":
            if hour < 12:
                hour += 12
            return _clock(hour, mn, 0)
        if dayword == "today":
            return _clock(hour, mn, 0)
        delta = (wd - lt.tm_wday) % 7
        cand = _clock(hour, mn, delta, roll=False)
        if cand <= now:
            cand = _clock(hour, mn, delta + 7, roll=False)
        return cand
    m = re.fullmatch(
        r"(at\s+)?(\d{1,2})(?::(\d{2}))?\s*"
        r"(a\.?m\.?|p\.?m\.?|am|pm)?"
        r"(?:\s+(tomorrow|tonight|today))?", t)
    if m and (m.group(1) or m.group(3) or m.group(4)):
        hour = _meridian(int(m.group(2)), m.group(4))
        offset = 1 if m.group(5) == "tomorrow" else 0
        if m.group(5) == "tonight" and hour < 12:
            hour += 12
        return _clock(hour, int(m.group(3) or 0), offset)
    return None


def parse_reminder_schedule(when: str, now: float) -> dict | None:
    """Parse a when-clause → schedule spec.

    One-shots return ``{"kind": "once", "at": ts}``; cadences return
    ``{"kind": "daily"|"weekly"|"interval", ...}`` so 'remind me
    every friday to X' becomes a real weekly schedule instead of an
    "unparseable" decline. Returns None when nothing parseable."""
    t = " ".join(str(when or "").lower().split())
    if not t:
        return None
    t = re.sub(r"\bat\s+noon\b", "at 12:00 pm", t)
    t = re.sub(r"\bat\s+midnight\b", "at 12:00 am", t)
    # 'at 6 in the morning' — daypart phrase is the meridiem.
    t = re.sub(r"(\d{1,2}(?::\d{2})?)\s+in\s+the\s+morning\b",
               r"\1 am", t)
    t = re.sub(r"(\d{1,2}(?::\d{2})?)\s+in\s+the\s+afternoon\b",
               r"\1 pm", t)
    t = re.sub(r"(\d{1,2}(?::\d{2})?)\s+in\s+the\s+evening\b",
               r"\1 pm", t)
    t = re.sub(r"(\d{1,2}(?::\d{2})?)\s+at\s+night\b", r"\1 pm", t)
    t = re.sub(r"\bo'?clock\b", "", t).strip()
    t = re.sub(r"\b(?:sharp|exactly|precisely|on\s+the\s+dot)\b",
               "", t)
    t = re.sub(r"\bat\s+(?:about|around|approximately|roughly|"
               r"like)\s+", "at ", t)
    t = re.sub(r"(?<!in\s)(?<!for\s)\b(?:about|around|"
               r"approximately|roughly|like)\s+"
               r"(?=\d|noon|midnight|quarter|half)", "at ", t)
    # 'roughly at 6' — a hedge word before the preposition isn't
    # part of the clock.
    t = re.sub(
        r"\b(?:roughly|basically|maybe|probably|kinda|sorta|just|"
        r"like)\s+(?=(?:at|on|in|every|next)\s|tomorrow|tonight|"
        r"noon|midnight)", "", t)
    # 'at 7ish' — the colloquial -ish suffix on a digit clock.
    t = re.sub(r"\b(\d{1,2})\s*ish\b", r"\1", t)
    # 'at 8 and a half' — 'and a half' is the :30. Inside an
    # 'in <dur>' it's a compound duration, not a clock.
    if not re.match(r"in\b", t):
        t = re.sub(r"\b(\d{1,2})\s+and\s+a\s+half\b", r"\1:30", t)
    # 'on school days' / 'work days' — weekday cadence names.
    t = re.sub(r"\b(?:school|work|business)\s+days\b",
               "weekdays", t)
    # 'first thing [tomorrow/in the morning]' — the start of the
    # next day, pinned at 7:00.
    t = re.sub(
        r"\bfirst\s+thing(?:\s+tomorrow|\s+in\s+the\s+morning|"
        r"\s+in\s+the\s+am)?\b", "tomorrow at 7", t)
    # 'around lunchtime' / 'at dinner' / 'during lunch' — meal
    # dayparts carry conventional clocks.
    t = re.sub(
        r"\b(?:at|around|during)\s+lunch\s*(?:time)?\b",
        "at 12:00 pm", t)
    t = re.sub(
        r"\b(?:at|around|during)\s+(?:dinner|supper)\s*"
        r"(?:time)?\b", "at 6 pm", t)
    t = re.sub(
        r"\b(?:at|around|during)\s+breakfast\s*(?:time)?\b",
        "at 8 am", t)
    # 'midday'/'mid-morning' — conventional mid-daypart clocks;
    # 'dawn'/'dusk' stay pin-fails (variable twilight, not a
    # clock). Emit '12:00 pm' — the 'at noon' conversion above
    # already ran.
    t = re.sub(
        r"\b(?:at|around|during)?\s*midday\b", "at 12:00 pm", t)
    t = re.sub(
        r"\b(?:at|around|during)?\s*mid-morning\b", "at 10 am", t)
    # 'sometime tonight' / 'at some point tomorrow' — the vague
    # qualifier isn't part of the time.
    t = re.sub(r"\bat\s+some\s+point\s+(?:in\s+)?", "", t)
    t = re.sub(r"\bsometime\b\s*", "", t)
    # 'when it's 5pm' — the 'when it's' lead IS the clock phrase,
    # not an event trigger and not a task.
    t = re.sub(r"\bwhen\s+it'?s\s+(?=\d|noon|midnight)", "at ", t)
    # 'by 5pm' / 'no later than 6' — a deadline pins at the bound;
    # 'snooze by 20 minutes' never reaches here (that's the delta
    # grammar, not a clock lead).
    t = re.sub(
        r"\bno\s+later\s+than\s+|\bby\s+(?=\d|noon|midnight)",
        "at ", t)
    t = re.sub(
        r"\bby\s+(?=morning|afternoon|evening|night\b|tonight|"
        r"tomorrow|today|mon|tue|wed|thu|fri|sat|sun)",
        "", t)
    # 'before 8pm' — same deadline pin as 'by'; 'before the
    # meeting' keeps its 'the' so an event still refuses. noon/
    # midnight emit the converted clock — the 'at noon' rewrite
    # already ran. '5 minutes before 3pm' is a clock offset —
    # its own grammar below, don't touch the 'before'.
    if not re.search(
            r"\b(?:a|an|one|two|three|four|five|six|seven|eight|"
            r"nine|ten|few|couple|\d+(?:\.\d+)?)\s+(?:second|"
            r"minute|hour|day|week|month)s?\s+before\b", t):
        t = re.sub(r"\bbefore\s+noon\b", "at 12:00 pm", t)
        t = re.sub(r"\bbefore\s+midnight\b", "at 12:00 am", t)
        t = re.sub(r"\bbefore\s+(?=\d)", "at ", t)
        t = re.sub(
            r"\bbefore\s+(?=morning|afternoon|evening|night\b|"
            r"tonight|tomorrow|today|monday|tuesday|wednesday|"
            r"thursday|friday|saturday|sunday)", "", t)
    # 'the day after tomorrow' — a date anchor, not an event.
    t = re.sub(
        r"\b(?:the\s+)?day\s+after\s+tomorrow\b", "in 2 days", t)
    # 'next weekend' — a one-shot anchor (the coming Saturday),
    # not the recurring 'weekend' cadence; 'next' must not fall
    # into the task slot.
    t = re.sub(r"\bnext\s+weekend\b", "saturday", t)
    # 'the rest of the day' — a named end-of-day bound, not vague.
    t = re.sub(
        r"\b(?:the\s+)?rest\s+of\s+(?:the\s+|my\s+)?"
        r"(?:day|today|tonight)\b", "at 11:59 pm", t)
    # 'once per week' — 'per <period>' is just 'a <period>'; the
    # count-cadence grammar already holds 'once/twice a day'.
    t = re.sub(
        r"\bper\s+(second|minute|hour|day|week|month|year)s?\b",
        r"a \1", t)
    # 'a week from now' — 'X from now' is just 'in X'.
    t = re.sub(
        r"((?:a|an|one|two|three|four|five|six|seven|eight|nine|"
        r"ten|couple|few|\d+(?:\.\d+)?)\s+(?:second|minute|hour|"
        r"day|week|month|year)s?)\s+from\s+now\b",
        r"in \1", t)
    # 'at 5pm in 2 days' — same combined anchor clock-first;
    # reorder to 'in 2 days at 5pm'.
    t = re.sub(
        r"^at\s+((?:\d{1,2})(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?|am|"
        r"pm)?|noon|midnight)\s+in\s+"
        r"((?:a|an|one|two|three|four|five|six|seven|eight|nine|"
        r"ten|couple|few|\d+(?:\.\d+)?)\s+(?:second|minute|hour|"
        r"day|week|month)s?)\s*$",
        r"in \2 at \1", t)
    # 'later tonight' — 'later' is a hedge before a real daypart,
    # not a vagueness that should pin-fail.
    t = re.sub(
        r"\blater\s+(?=tonight|tomorrow|today|this\s+(?:morning|"
        r"afternoon|evening|week|month|year))", "", t)
    # 'at 930' / 'at 1030' / 'at 930pm' — a 3-4 digit bare clock
    # reads H:MM.
    t = re.sub(
        r"\bat\s+(\d{3,4})\s*(a\.?m\.?|p\.?m\.?)\b",
        lambda m: (f"at {m.group(1)[:-2]}:{m.group(1)[-2:]} "
                   + m.group(2)), t)
    t = re.sub(
        r"\bat\s+(\d{3,4})\b(?!\s*(?:a\.?m\.?|p\.?m\.?|:))",
        lambda m: f"at {m.group(1)[:-2]}:{m.group(1)[-2:]}", t)
    # 'a quarter after 3' — 'after' is 'past' in clock grammar;
    # a leading 'a' before quarter/half breaks the worded clock.
    t = re.sub(r"\bquarter\s+after\b", "quarter past", t)
    t = re.sub(
        r"\ba\s+(?=quarter\s+(?:past|to|till|til)\b|half\s+past)",
        "", t)
    # 'at seven tomorrow morning' — a worded hour + day + daypart
    # gets its meridiem from the daypart.
    t = re.sub(
        r"\bat\s+(one|two|three|four|five|six|seven|eight|nine|"
        r"ten|eleven|twelve)\s+(tomorrow|today)\s+"
        r"(morning|afternoon|evening|night)\b",
        lambda m: (f"at {m.group(1)} "
                   + ("am" if m.group(3) == "morning" else "pm")
                   + f" {m.group(2)}"), t)
    # 'every single day' — 'single' is an intensifier.
    t = re.sub(r"\bevery\s+single\s+", "every ", t)
    # 'the first of every month' — a worded ordinal is the day.
    # 'on the 15th of each month' — keep a single 'on'.
    t = re.sub(
        r"(?<!\bon\s)\bthe\s+(first|second|third|fourth|fifth|"
        r"sixth|seventh|eighth|ninth|tenth|(\d{1,2})(?:st|nd|rd|th)?)"
        r"\s+of\s+(?:every|each|the)\s+month\b",
        lambda m: "on the " + str({
            "first": 1, "second": 2, "third": 3, "fourth": 4,
            "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8,
            "ninth": 9, "tenth": 10}.get(m.group(1), m.group(2)))
        + " of every month", t)
    # '6 am weekdays' — a leading clock belongs inside the
    # cadence's 'at T' tail.
    t = re.sub(
        r"\b(\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?|am|pm))\s+"
        r"(weekdays?|weekends?|daily|weekly|mornings?|"
        r"afternoons?|evenings?|nights?)", r"\2 at \1", t)
    # 'at 5pm on the 20th' — a leading clock belongs after the
    # date phrase, matching 'on the 20th at 5pm'.
    t = re.sub(
        r"\bat\s+(\d{1,2}(?::\d{2})?\s*"
        r"(?:a\.?m\.?|p\.?m\.?|am|pm)?)\s+"
        r"(on\s+(?:(?:the\s+)?\d{1,2}(?:st|nd|rd|th)?|"
        r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
        r"\w*\s+\d{1,2}(?:st|nd|rd|th)?))",
        r"\2 at \1", t)
    # 'on 10/15' — a numeric date reads as october 15; 'at 7a' /
    # 'at 7p' shorthand meridiems; 'monday through friday' is the
    # weekdays cadence.
    t = re.sub(
        r"\b(\d{1,2})/(\d{1,2})\b",
        lambda m: (_MONTHS_NUM[int(m.group(1))] + " " + m.group(2))
        if 1 <= int(m.group(1)) <= 12
        and 1 <= int(m.group(2)) <= 31 else m.group(0), t)
    t = re.sub(r"\b(\d{1,2})\s*([ap])\.?(?=\s|$|[?,;])",
               r"\1 \2m", t)
    t = re.sub(
        r"\bmonday\s*(?:-\s*|\s+(?:through|thru|to|-)\s+)friday\b",
        "weekdays", t)
    # 'only on weekdays' — the filler isn't part of the cadence.
    t = re.sub(
        r"\bonly\s+(?=(?:on|at|in|every|next|tomorrow|tonight|"
        r"weekday|weekend|daily|weekly))", "", t)
    t = " ".join(t.split())
    t = _worded_clock(t)
    if _bad_clock(t):
        return None

    # '5 minutes before 3pm' — a clock offset.
    m = re.fullmatch(
        r"(?:in\s+)?(\d+(?:\.\d+)?|an?|one|two|three|four|five|"
        r"six|seven|eight|nine|ten|eleven|twelve)\s*"
        r"(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h)\s+"
        r"(before|after)\s+(.+)", t)
    if m:
        base = (parse_reminder_when(m.group(4), now)
                or parse_reminder_when("at " + m.group(4), now))
        if base is not None:
            scale = {"s": 1, "m": 60, "h": 3600}[m.group(2)[0]]
            n_raw = m.group(1)
            n = (_WORD_NUM.get(n_raw)
                 or (1.0 if n_raw in ("a", "an") else None))
            if n is None:
                n = float(n_raw)
            d = n * scale
            return {"kind": "once",
                    "at": (base - d if m.group(3) == "before"
                           else base + d)}

    def _meridian(hour: int, mer: str | None) -> int:
        mer = re.sub(r"[\s.]", "", mer or "")
        if mer == "pm" and hour < 12:
            return hour + 12
        if mer == "am" and hour == 12:
            return 0
        if not mer and hour <= 6:
            return hour + 12
        return hour

    clock_tail = (r"(?:\s+at\s+(\d{1,2})(?::(\d{2}))?"
                  r"\s*(a\.?m\.?|p\.?m\.?|am|pm)?)?")

    def _anchor(m, gi):
        """Interval cadence + 'at CLOCK' — the clock anchors the
        first fire; last_run + interval resumes the cadence."""
        hr, mn, mer = m.group(gi), m.group(gi + 1), m.group(gi + 2)
        if hr is None:
            return {}
        hour = _meridian(int(hr), mer)
        return {"at": _next_daily(hour, int(mn or 0), after=now),
                "hour": hour, "minute": int(mn or 0)}

    # 'monthly [on the 15th]' / 'every month on the 1st' /
    # 'on the 15th of every month' — day-of-month cadence.
    m = re.fullmatch(
        r"(?:every\s+month|each\s+month|monthly|once\s+a\s+month)"
        r"(?:\s+on\s+the\s+(\d{1,2})(?:st|nd|rd|th)?)?"
        + clock_tail, t)
    if m:
        hr, mn, mer = m.group(2), m.group(3), m.group(4)
        lt = time.localtime(now)
        return {"kind": "monthly",
                "day": (int(m.group(1)) if m.group(1)
                        else lt.tm_mday),
                "hour": _meridian(int(hr), mer) if hr else 9,
                "minute": int(mn or 0)}
    m = re.fullmatch(
        r"on\s+the\s+(\d{1,2})(?:st|nd|rd|th)?\s+"
        r"(?:day\s+)?of\s+(?:every|each|the)\s+month"
        + clock_tail, t)
    if m:
        hr, mn, mer = m.group(2), m.group(3), m.group(4)
        return {"kind": "monthly", "day": int(m.group(1)),
                "hour": _meridian(int(hr), mer) if hr else 9,
                "minute": int(mn or 0)}

    # 'weekly' / 'every week [at T]' — an unspecified day defaults
    # to today's weekday, mirroring 'monthly' → today's day-of-month.
    m = re.fullmatch(
        r"(?:weekly|every\s+week|once\s+a\s+week|each\s+week)"
        + clock_tail, t)
    if m:
        hr, mn, mer = m.group(1), m.group(2), m.group(3)
        lt = time.localtime(now)
        return {"kind": "weekly", "weekday": lt.tm_wday,
                "hour": _meridian(int(hr), mer) if hr else 9,
                "minute": int(mn or 0)}
    # 'fortnightly' / 'every other week' — a fixed 14-day interval.
    if re.fullmatch(r"fortnightly|every\s+fortnight|a\s+fortnight|"
                    r"bi\s*-?\s*weekly|semi\s*-?\s*weekly|"
                    r"every\s+(?:other|second|two|2)\s+weeks?", t):
        return {"kind": "interval", "interval_s": 1209600.0}
    # 'annual' / 'quarterly' / 'biannual' — fixed-span intervals
    # (the model fake-set 'annual reminder for taxes' over zero
    # rows).
    m = re.fullmatch(
        r"(?:annual(?:ly)?|yearly|every\s+year|once\s+a\s+year|"
        r"quarterly|every\s+quarter|semi\s*-?\s*annual(?:ly)?|"
        r"bi\s*-?\s*annual(?:ly)?|every\s+other\s+month|"
        r"every\s+2\s+months|bimonthly)", t)
    if m:
        if "quarter" in t:
            interval = 91 * 86400.0
        elif "month" in t:
            interval = 60 * 86400.0
        elif "semi" in t or "bi" in t:
            interval = 182 * 86400.0
        else:
            interval = 365 * 86400.0
        return {"kind": "interval", "interval_s": interval}
    # 'twice a day' / 'three times a week' — N fires spread over
    # the span becomes a fixed interval.
    m = re.fullmatch(
        r"twice\s+(?:a\s+(day|week|month|year)|daily)|"
        r"two\s+times\s+a\s+(day|week|month|year)|"
        r"(three|four|five|six|\d+)\s+times\s+a\s+"
        r"(day|week|month|year)", t)
    if m:
        unit = (m.group(1) or m.group(2) or m.group(4) or "day")
        n_f = float({"three": 3, "four": 4, "five": 5,
                     "six": 6}.get(m.group(3) or "",
                                   m.group(3) or 2) or 2)
        span = {"day": 86400.0, "week": 604800.0,
                "month": 30 * 86400.0,
                "year": 365 * 86400.0}[unit]
        return {"kind": "interval", "interval_s": span / n_f}
    m = re.fullmatch(
        r"(?:every\s+other\s+day|every\s+second\s+day)"
        + clock_tail, t)
    if m:
        return {"kind": "interval", "interval_s": 172800.0,
                **_anchor(m, 1)}
    # 'every third day' — an ordinal picks every Nth unit.
    m = re.fullmatch(
        r"every\s+(third|fourth|fifth|sixth|seventh|eighth|"
        r"ninth|tenth|(\d+)(?:st|nd|rd|th))\s+"
        r"(minutes?|mins?|hours?|hrs?|days?|weeks?)"
        + clock_tail, t)
    if m:
        n_o = float({"third": 3, "fourth": 4, "fifth": 5,
                     "sixth": 6, "seventh": 7, "eighth": 8,
                     "ninth": 9, "tenth": 10}.get(
                         m.group(1), m.group(2) or 1))
        unit_o = m.group(3)
        return {"kind": "interval",
                "interval_s": n_o * (
                    60 if unit_o.startswith(("m", "min")) else
                    3600 if unit_o.startswith("h") else
                    86400 if unit_o.startswith("d") else 604800),
                **_anchor(m, 4)}
    # 'every half hour' / 'every half a day' — fractional spans.
    m = re.fullmatch(
        r"every\s+(?:half|a\s+half)\s+(?:an?\s+)?(hour|day)"
        + clock_tail, t)
    if m:
        return {"kind": "interval",
                "interval_s": 1800.0 if m.group(1) == "hour"
                else 43200.0,
                **_anchor(m, 2)}
    # 'every couple (of) hours' / 'every few hours' — informal
    # two/three-unit spans.
    m = re.fullmatch(
        r"every\s+(?:a\s+)?(couple|few)\s+(?:of\s+)?"
        r"(minutes?|mins?|hours?|hrs?|days?)"
        + clock_tail, t)
    if m:
        unit = m.group(2)
        n = 2.0 if m.group(1) == "couple" else 3.0
        return {"kind": "interval",
                "interval_s": n * (
                    60 if unit.startswith(("m", "min")) else
                    3600 if unit.startswith("h") else 86400),
                **_anchor(m, 3)}
    # 'every N units' / 'every hour' / 'hourly' — interval cadence.
    m = re.fullmatch(
        r"(?:every\s+(\d+(?:\.\d+)?)\s*"
        r"(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|"
        r"weeks?)"
        r"|every\s+hour(?:\s+on\s+the\s+hour)?|hourly"
        r"|on\s+the\s+hour)"
        + clock_tail, t)
    if m:
        if m.group(1) is None:
            interval = 3600.0
        else:
            unit = m.group(2)
            interval = float(m.group(1)) * (
                1 if unit.startswith(("s", "sec")) else
                60 if unit.startswith(("m", "min")) else
                3600 if unit.startswith("h") else
                86400 if unit.startswith("d") else 604800)
        return {"kind": "interval", "interval_s": max(60.0, interval),
                **_anchor(m, 3)}
    # 'every day except sunday [at T]' — all week minus the named
    # day(s), a real weekly spec rather than a narrated exception.
    m = re.fullmatch(
        r"every\s+day\s+except\s+(\w+day)s?"
        r"(?:\s+and\s+(\w+day)s?)?" + clock_tail, t)
    if m:
        excl = {d for d in (_WEEKDAY_NAMES.get(m.group(1)),
                            _WEEKDAY_NAMES.get(m.group(2)))
                if d is not None}
        if excl:
            hr, mn, mer = m.group(3), m.group(4), m.group(5)
            return {"kind": "weekly",
                    "weekday": [d for d in range(7) if d not in excl],
                    "hour": _meridian(int(hr), mer) if hr else 9,
                    "minute": int(mn or 0)}
    # 'every day/morning/evening/night [at T]' — daily cadence.
    m = re.fullmatch(
        r"(?:every\s+day|daily|every\s+morning|every\s+afternoon|"
        r"every\s+evening|every\s+night|nightly|each\s+night|"
        r"each\s+(?:morning|evening|day))"
        + clock_tail, t)
    if m:
        hr, mn, mer = m.group(1), m.group(2), m.group(3)
        if hr is not None:
            hour = _meridian(int(hr), mer)
            if not mer:
                # 'every evening at 7' — the daypart implies the
                # meridiem for a bare hour ('nightly' counts as
                # 'night'); without this 7 pinned 07:00.
                pm = re.search(
                    r"morning|afternoon|evening|night", t)
                if pm is not None:
                    hour = (int(hr) if pm.group(0) == "morning"
                            else int(hr) + 12 if int(hr) < 12
                            else int(hr))
        else:
            hour = (9 if "morning" in t else
                    15 if "afternoon" in t else
                    20 if "night" in t else
                    19 if "evening" in t else 9)
        return {"kind": "daily", "hour": hour,
                "minute": int(mn or 0)}
    # 'every weekday/weekend [at T]' — multi-day weekly cadence.
    m = re.fullmatch(
        r"(?:every\s+|on\s+(?:the\s+)?)?(weekdays?|weekends?)"
        + clock_tail, t)
    if m:
        days = ([0, 1, 2, 3, 4] if m.group(1).startswith("weekday")
                else [5, 6])
        hr, mn, mer = m.group(2), m.group(3), m.group(4)
        return {"kind": "weekly", "weekday": days,
                "hour": _meridian(int(hr), mer) if hr else 9,
                "minute": int(mn or 0)}
    # Bare plurals — 'remind me fridays' recurs; the singular
    # 'friday' stays a one-shot handled by parse_reminder_when.
    m = re.fullmatch(r"(\w+day)s\b" + clock_tail, t)
    if m:
        wd = _WEEKDAY_NAMES.get(m.group(1))
        if wd is not None:
            hr, mn, mer = m.group(2), m.group(3), m.group(4)
            return {"kind": "weekly", "weekday": wd,
                    "hour": _meridian(int(hr), mer) if hr else 9,
                    "minute": int(mn or 0)}
    # 'every <weekday> [at T]' / 'on fridays [at T]' — weekly
    # cadence. 'on friday' (singular) is the coming Friday — a
    # one-shot; 'on' recurs only over the plural.
    m = re.fullmatch(
        r"(every|each|on)\s+(\w+?)(s)?\b" + clock_tail, t)
    if m:
        wd = (_WEEKDAY_NAMES.get(m.group(2))
              if m.group(1) != "on" or m.group(3)
              else None)
        if wd is not None:
            hr, mn, mer = m.group(4), m.group(5), m.group(6)
            return {"kind": "weekly", "weekday": wd,
                    "hour": _meridian(int(hr), mer) if hr else 9,
                    "minute": int(mn or 0)}
    # 'saturday mornings' / 'every friday night' / 'weekday
    # mornings' — the daypart phrase supplies the hour.
    m = re.fullmatch(
        r"(?:every|on|each)?\s*(weekdays?|weekends?|\w+?day)s?\s+"
        r"(morning|afternoon|evening|night)s?" + clock_tail, t)
    if m:
        tok, part = m.group(1), m.group(2)
        wd = ([0, 1, 2, 3, 4] if tok.startswith("weekday")
              else [5, 6] if tok.startswith("weekend")
              else _WEEKDAY_NAMES.get(tok.rstrip("s")))
        if wd is not None:
            hr, mn, mer = m.group(3), m.group(4), m.group(5)
            if hr:
                # 'every friday evening at 7' — the daypart implies
                # the meridiem for a bare hour.
                hour = (_meridian(int(hr), mer) if mer else
                        (int(hr) + 12
                         if part != "morning" and int(hr) < 12
                         else int(hr)))
            else:
                hour = (9 if part == "morning" else
                        15 if part == "afternoon" else
                        19 if part == "evening" else 20)
            return {"kind": "weekly", "weekday": wd,
                    "hour": hour, "minute": int(mn or 0)}
    # 'every monday and friday' / 'every mon, wed and fri' /
    # 'monday wednesday friday at 8' — explicit multi-day list;
    # the lead and the joins are all optional. Single-day strings
    # still fall through to the single-day branches.
    m = re.fullmatch(
        r"(?:every|on|each)?\s*"
        r"(\w+(?:(?![\s,]+at\s+\d)[\s,]+(?:and\s+)?\w+)*)"
        + clock_tail, t)
    if m:
        tokens = [p.rstrip("s") if p not in _WEEKDAY_NAMES else p
                  for p in re.findall(
                      r"[a-z]+", re.sub(r"\band\b", ",", m.group(1)))]
        days = [_WEEKDAY_NAMES.get(p) for p in tokens]
        if all(d is not None for d in days) and len(days) > 1:
            hr, mn, mer = m.group(2), m.group(3), m.group(4)
            return {"kind": "weekly", "weekday": sorted(set(days)),
                    "hour": _meridian(int(hr), mer) if hr else 9,
                    "minute": int(mn or 0)}
    # Clock-before-cadence — 'at 3pm every day', 'at 8am on weekdays'.
    cm = re.match(
        r"at\s+(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?|am|pm)?"
        r"\s+(.+)$", t)
    if cm:
        rest_spec = parse_reminder_schedule(cm.group(4), now)
        if (rest_spec is not None
                and rest_spec["kind"] in ("daily", "weekly")):
            rest_spec["hour"] = _meridian(int(cm.group(1)),
                                          cm.group(3))
            rest_spec["minute"] = int(cm.group(2) or 0)
            return rest_spec
    at = parse_reminder_when(t, now)
    if at is not None:
        return {"kind": "once", "at": at}
    return None


def _norm_weekday(weekday):
    """Normalize a weekday spec → int, sorted list of ints, or None.
    'every weekday' stores [0..4]; a single day stays an int."""
    if isinstance(weekday, (list, tuple, set)):
        days = sorted({int(d) for d in weekday if int(d) in range(7)})
        return days or None
    return int(weekday) if weekday in range(7) else None


def _next_daily(hour: int, minute: int, *, weekday=None,
                after: float | None = None) -> float:
    """Next local wall-clock occurrence of hour:minute (optionally only on
    specific weekdays, Mon=0; a list means 'first matching day').
    DST-safe because it anchors on local date."""
    if isinstance(weekday, (list, tuple, set)):
        wd_set = set(weekday) or None
    else:
        wd_set = {weekday} if weekday is not None else None
    base = time.localtime(after or time.time())
    for day_offset in range(0, 8):
        cand = time.mktime((
            base.tm_year, base.tm_mon, base.tm_mday + day_offset,
            hour, minute, 0, 0, 0, -1))
        cand_lt = time.localtime(cand)
        if wd_set is not None and cand_lt.tm_wday not in wd_set:
            continue
        if cand > (after or time.time()):
            return cand
    return (after or time.time()) + 86400  # unreachable fallback


def _next_monthly(day: int, hour: int, minute: int,
                  after: float | None = None) -> float:
    """Next occurrence of day-of-month `day` at hour:minute — clamps
    to the month end for days 29-31 on short months."""
    base = time.localtime(after or time.time())
    for month_offset in range(0, 15):
        year = base.tm_year + (base.tm_mon - 1 + month_offset) // 12
        month = (base.tm_mon - 1 + month_offset) % 12 + 1
        mday = min(int(day),
                   calendar.monthrange(year, month)[1])
        cand = time.mktime(
            (year, month, mday, hour, minute, 0, 0, 0, -1))
        if cand > (after or time.time()):
            return cand
    return (after or time.time()) + 2592000  # unreachable fallback


class Scheduler:
    def __init__(self, store, *,
                 on_fire: Callable[[dict], None] | None = None) -> None:
        self._store = store
        self.on_fire = on_fire
        self._lock = threading.RLock()

    # -- CRUD --------------------------------------------------------------

    def add(self, name: str, kind: str, *, at: float | None = None,
            interval_s: float = 0, hour: int = 3, minute: int = 0,
            weekday: int | None = None, day: int | None = None,
            action: dict | None = None,
            enabled: bool = True, created_by: str = "user") -> dict:
        if kind not in KINDS:
            raise ValueError(f"unknown schedule kind '{kind}'")
        row = {
            "id": f"sc-{uuid.uuid4().hex[:10]}",
            "name": str(name or kind)[:140],
            "kind": kind,
            "at": float(at) if at else None,
            "interval_s": max(60.0, float(interval_s)) if interval_s else 0,
            "hour": max(0, min(23, int(hour))),
            "minute": max(0, min(59, int(minute))),
            "weekday": _norm_weekday(weekday),
            "day": (max(1, min(31, int(day)))
                    if day is not None else None),
            "action": dict(action or {}),
            "enabled": bool(enabled),
            "created_by": created_by,
            "created_at": time.time(),
            "last_run": None,
            "next_run": None,
        }
        row["next_run"] = self._compute_next(row)
        with self._lock:
            self._store.schedules.data.setdefault("schedules", []).append(row)
            self._store.schedules.save()
        return dict(row)

    def remove(self, schedule_id: str) -> bool:
        with self._lock:
            rows = self._store.schedules.data.setdefault("schedules", [])
            for i, r in enumerate(rows):
                if r.get("id") == schedule_id:
                    rows.pop(i)
                    self._store.schedules.save()
                    return True
        return False

    def set_enabled(self, schedule_id: str, enabled: bool) -> bool:
        with self._lock:
            for r in self._store.schedules.data.setdefault("schedules", []):
                if r.get("id") == schedule_id:
                    r["enabled"] = bool(enabled)
                    r["next_run"] = self._compute_next(r) if enabled else None
                    self._store.schedules.save()
                    return True
        return False

    def update(self, schedule_id: str, **fields) -> dict | None:
        """Patch schedule fields (name, at, interval_s, hour, minute,
        weekday, action, enabled) and recompute next_run — the write
        path behind 'snooze the X reminder' and similar edits."""
        with self._lock:
            for r in self._store.schedules.data.setdefault("schedules", []):
                if r.get("id") != schedule_id:
                    continue
                for key, value in fields.items():
                    if key in ("id", "created_at", "created_by",
                               "last_run", "next_run"):
                        continue
                    if key == "at":
                        value = float(value) if value else None
                    elif key == "interval_s":
                        value = max(60.0, float(value)) if value else 0
                    elif key in ("hour", "minute"):
                        value = max(0, min(23 if key == "hour" else 59,
                                         int(value)))
                    elif key == "day":
                        value = (max(1, min(31, int(value)))
                                 if value is not None else None)
                    elif key == "action" and isinstance(value, dict):
                        value = dict(value)
                    r[key] = value
                r["next_run"] = (self._compute_next(r)
                                 if r.get("enabled", True) else None)
                self._store.schedules.save()
                return dict(r)
        return None

    def list(self) -> list[dict]:
        return [dict(r) for r in self._store.schedules.rows()]

    # -- firing -------------------------------------------------------------

    def _compute_next(self, row: dict, *, after: float | None = None) -> float | None:
        now = after or time.time()
        kind = row.get("kind")
        if kind == "once":
            # Fire on next tick even if 'at' passed while the app was down —
            # a missed one-shot must not be silently dropped across restarts.
            at = row.get("at")
            return float(at) if at else None
        if kind == "interval":
            last = row.get("last_run")
            if last is None and row.get("at"):
                # 'every other day at 9' — 'at' anchors the first
                # fire; after it runs, last_run + interval resumes.
                return float(row["at"])
            last = last or row.get("created_at") or now
            return float(last) + float(row.get("interval_s") or 3600)
        if kind == "daily":
            return _next_daily(int(row.get("hour", 3)), int(row.get("minute", 0)),
                               after=now)
        if kind == "weekly":
            wd = row.get("weekday")
            return _next_daily(
                int(row.get("hour", 3)), int(row.get("minute", 0)),
                weekday=wd if wd is not None else 6, after=now)
        if kind == "monthly":
            return _next_monthly(
                int(row.get("day") or 1), int(row.get("hour", 3)),
                int(row.get("minute", 0)), after=now)
        return None

    def tick(self) -> list[dict]:
        """Fire due schedules; recompute next_run. Returns fired rows."""
        fired: list[dict] = []
        now = time.time()
        with self._lock:
            for row in self._store.schedules.data.setdefault("schedules", []):
                if not row.get("enabled"):
                    continue
                nxt = row.get("next_run")
                if nxt is None:
                    row["next_run"] = self._compute_next(row)
                    continue
                if now >= float(nxt):
                    row["last_run"] = now
                    row["next_run"] = (self._compute_next(row, after=now)
                                       if row.get("kind") != "once" else None)
                    if row.get("kind") == "once":
                        row["enabled"] = False
                    fired.append(dict(row))
            if fired:
                self._store.schedules.save()
            else:
                self._store.schedules.save()
        for row in fired:
            if self.on_fire is not None:
                try:
                    self.on_fire(row)
                except Exception:
                    pass
        return fired

    def next_due(self) -> dict | None:
        rows = [r for r in self._store.schedules.rows()
                if r.get("enabled") and r.get("next_run")]
        if not rows:
            return None
        return min(rows, key=lambda r: r["next_run"])
