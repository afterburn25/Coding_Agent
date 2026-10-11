"""Durable Scheduler — one-time / interval / daily / weekly schedules.

Schedules persist and survive restart. Daily/weekly use wall-clock local
time (computed fresh each fire), so DST transitions re-anchor correctly
instead of drifting by fixed 24h deltas.
"""
from __future__ import annotations

import re
import threading
import time
import uuid
from typing import Any, Callable

KINDS = {"once", "interval", "daily", "weekly"}

_WEEKDAY_NAMES = {
    "mon": 0, "monday": 0, "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "wednesday": 2, "thu": 3, "thur": 3, "thurs": 3,
    "thursday": 3, "fri": 4, "friday": 4, "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6}


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
        r"in\s+(?:about\s+|around\s+|roughly\s+)?"
        r"(\d+(?:\.\d+)?|an?\s+couple\s+of|an?|one|two|three|four|"
        r"five|six|seven|eight|nine|ten|eleven|twelve|half\s+an?)\s*"
        r"(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|"
        r"days?|d)", t)
    if m:
        n_raw = m.group(1)
        n = (0.5 if n_raw.startswith("half") else
             2.0 if "couple" in n_raw else
             _WORDS.get(n_raw, None))
        if n is None:
            n = float(n_raw)
        scale = {"s": 1, "m": 60, "h": 3600, "d": 86400}[
            m.group(2)[0]]
        return now + n * scale
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
    if t == "noon":
        return _clock(12, 0)
    if t == "midnight":
        return _clock(0, 0)
    if t == "next week":
        return _clock(9, 0, 7, roll=False)
    m = re.fullmatch(
        r"(?:(on|next)\s+)?(\w+?)" + clock_tail, t)
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

    # 'twice a day' / 'every other day' — fixed-span intervals.
    if re.fullmatch(r"twice\s+a\s+day|two\s+times\s+a\s+day", t):
        return {"kind": "interval", "interval_s": 43200.0}
    if re.fullmatch(r"every\s+other\s+day|every\s+second\s+day", t):
        return {"kind": "interval", "interval_s": 172800.0}
    # 'every N units' / 'every hour' / 'hourly' — interval cadence.
    m = re.fullmatch(
        r"(?:every\s+(\d+(?:\.\d+)?)\s*"
        r"(minutes?|mins?|hours?|hrs?|days?)|every\s+hour|hourly)", t)
    if m:
        if m.group(1) is None:
            interval = 3600.0
        else:
            unit = m.group(2)
            interval = float(m.group(1)) * (
                60 if unit.startswith(("m", "min")) else
                3600 if unit.startswith("h") else 86400)
        return {"kind": "interval", "interval_s": max(60.0, interval)}
    # 'every day/morning/evening [at T]' — daily cadence.
    m = re.fullmatch(
        r"(?:every\s+day|daily|every\s+morning|every\s+afternoon|"
        r"every\s+evening|each\s+(?:morning|evening|day))"
        + clock_tail, t)
    if m:
        hr, mn, mer = m.group(1), m.group(2), m.group(3)
        if hr is not None:
            hour = _meridian(int(hr), mer)
        else:
            hour = (9 if "morning" in t else
                    15 if "afternoon" in t else
                    19 if "evening" in t else 9)
        return {"kind": "daily", "hour": hour,
                "minute": int(mn or 0)}
    # 'every weekday/weekend [at T]' — multi-day weekly cadence.
    m = re.fullmatch(
        r"every\s+(weekdays?|weekends?)" + clock_tail, t)
    if m:
        days = ([0, 1, 2, 3, 4] if m.group(1).startswith("weekday")
                else [5, 6])
        hr, mn, mer = m.group(2), m.group(3), m.group(4)
        return {"kind": "weekly", "weekday": days,
                "hour": _meridian(int(hr), mer) if hr else 9,
                "minute": int(mn or 0)}
    # 'every <weekday> [at T]' — weekly cadence.
    m = re.fullmatch(
        r"every\s+(\w+?)" + clock_tail, t)
    if m:
        wd = _WEEKDAY_NAMES.get(m.group(1))
        if wd is not None:
            hr, mn, mer = m.group(2), m.group(3), m.group(4)
            return {"kind": "weekly", "weekday": wd,
                    "hour": _meridian(int(hr), mer) if hr else 9,
                    "minute": int(mn or 0)}
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


class Scheduler:
    def __init__(self, store, *,
                 on_fire: Callable[[dict], None] | None = None) -> None:
        self._store = store
        self.on_fire = on_fire
        self._lock = threading.RLock()

    # -- CRUD --------------------------------------------------------------

    def add(self, name: str, kind: str, *, at: float | None = None,
            interval_s: float = 0, hour: int = 3, minute: int = 0,
            weekday: int | None = None, action: dict | None = None,
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
