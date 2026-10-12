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
    m = re.search(r"\bquarter\s+to\s+(\w+)\b", t)
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
    m = re.search(r"\b(" + _MIN_TOK + r")\s+to\s+(\w+)\b", t)
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
    return t


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
    t = _worded_clock(t)
    lt = time.localtime(now)

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
        r"half\s+an?)\s*"
        r"(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|"
        r"days?|d|weeks?|w|months?)", t)
    if m:
        n_raw = m.group(1)
        n = (0.5 if n_raw.startswith("half") else
             2.0 if "couple" in n_raw else
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
            return time.mktime(
                (yr, mon, day, lt2.tm_hour, lt2.tm_min, 0,
                 -1, -1, -1))
        scale = {"s": 1, "sec": 1, "second": 1, "m": 60, "min": 60,
                 "minute": 60, "h": 3600, "hr": 3600, "hour": 3600,
                 "d": 86400, "day": 86400, "w": 604800,
                 "week": 604800}[unit]
        return now + n * scale
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
        r"tomorrow(?:\s+(morning|afternoon|evening|night))?"
        + clock_tail, t)
    if m:
        part, hr, mn, mer = m.groups()
        minute = 0
        if hr is not None:
            hour = _meridian(int(hr), mer)
            minute = int(mn or 0)
        elif part:
            hour = {"morning": 9, "afternoon": 15,
                    "evening": 19, "night": 21}[part]
        else:
            hour = 9
        return _clock(hour, minute, 1, roll=False)
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
        r"at\s+(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?|am|pm)?"
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
        r"(?:\s+(tomorrow|tonight))?", t)
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
    t = _worded_clock(t)

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

    # 'monthly [on the 15th]' / 'every month on the 1st' /
    # 'on the 15th of every month' — day-of-month cadence.
    m = re.fullmatch(
        r"(?:every\s+month|monthly|once\s+a\s+month)"
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
        r"(?:day\s+)?of\s+every\s+month" + clock_tail, t)
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
    if re.fullmatch(r"fortnightly|every\s+(?:other|second|two|2)\s+"
                    r"weeks?", t):
        return {"kind": "interval", "interval_s": 1209600.0}
    # 'twice a day' / 'every other day' — fixed-span intervals.
    if re.fullmatch(
            r"twice\s+(?:a\s+day|daily)|two\s+times\s+a\s+day", t):
        return {"kind": "interval", "interval_s": 43200.0}
    if re.fullmatch(r"every\s+other\s+day|every\s+second\s+day", t):
        return {"kind": "interval", "interval_s": 172800.0}
    # 'every half hour' / 'every half a day' — fractional spans.
    m = re.fullmatch(
        r"every\s+(?:half|a\s+half)\s+(?:an?\s+)?(hour|day)", t)
    if m:
        return {"kind": "interval",
                "interval_s": 1800.0 if m.group(1) == "hour"
                else 43200.0}
    # 'every couple (of) hours' — informal two-unit span.
    m = re.fullmatch(
        r"every\s+(?:a\s+)?couple\s+(?:of\s+)?"
        r"(minutes?|mins?|hours?|hrs?|days?)", t)
    if m:
        unit = m.group(1)
        return {"kind": "interval",
                "interval_s": 2.0 * (
                    60 if unit.startswith(("m", "min")) else
                    3600 if unit.startswith("h") else 86400)}
    # 'every N units' / 'every hour' / 'hourly' — interval cadence.
    m = re.fullmatch(
        r"(?:every\s+(\d+(?:\.\d+)?)\s*"
        r"(minutes?|mins?|hours?|hrs?|days?|weeks?)"
        r"|every\s+hour(?:\s+on\s+the\s+hour)?|hourly"
        r"|on\s+the\s+hour)", t)
    if m:
        if m.group(1) is None:
            interval = 3600.0
        else:
            unit = m.group(2)
            interval = float(m.group(1)) * (
                60 if unit.startswith(("m", "min")) else
                3600 if unit.startswith("h") else
                86400 if unit.startswith("d") else 604800)
        return {"kind": "interval", "interval_s": max(60.0, interval)}
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
    # 'every monday and friday' / 'every mon, wed and fri' —
    # explicit multi-day list, comma- or 'and'-joined.
    m = re.fullmatch(
        r"(?:every|on|each)\s+(\w+(?:\s*,\s*\w+)*(?:\s*,?\s+and\s+\w+|"
        r"\s+and\s+\w+)?)" + clock_tail, t)
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
            last = row.get("last_run") or row.get("created_at") or now
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
