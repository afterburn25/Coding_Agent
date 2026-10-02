"""Notification Center — mission-visible notifications with policies,
dedupe, and quiet hours. Persisted so unattended alerts survive restart.
"""
from __future__ import annotations

import hashlib
import time
import uuid
from typing import Any, Callable

POLICIES = {"all", "important", "failures", "completion", "silent"}

# Which severities each policy forwards.
_LEVEL_FOR = {
    "all": {"info", "important", "failure", "approval", "completion"},
    "important": {"important", "failure", "approval", "completion"},
    "failures": {"failure", "approval"},
    "completion": {"completion", "failure"},
    "silent": set(),
}

# During quiet hours only these severities surface (failures can be
# configured off via policy; approvals/criticals still pass).
_QUIET_ALLOWED = {"failure", "approval"}


class NotificationCenter:
    def __init__(self, store, *, publish: Callable[[dict], None] | None = None,
                 quiet_hours: tuple[int, int] | None = None) -> None:
        self._store = store
        self._publish = publish
        self.quiet_hours = quiet_hours  # (start_hour, end_hour) local, e.g. (23, 7)
        self._recent: dict[str, float] = {}

    def _in_quiet_hours(self) -> bool:
        if not self.quiet_hours:
            return False
        start, end = self.quiet_hours
        hour = int(time.strftime("%H"))
        if start <= end:
            return start <= hour < end
        return hour >= start or hour < end  # overnight window

    def notify(self, message: str, *, level: str = "important",
               policy: str = "important", mission_id: str = "",
               title: str = "", detail: str = "",
               actions: list[str] | None = None) -> dict | None:
        """Record + emit a notification if the policy and quiet hours allow.
        Dedupes identical (mission, level, message) within 30 minutes."""
        level = level if level in {"info", "important", "failure",
                                   "approval", "completion"} else "info"
        policy = policy if policy in POLICIES else "important"

        if level not in _LEVEL_FOR.get(policy, set()):
            return None
        if self._in_quiet_hours() and level not in _QUIET_ALLOWED:
            row = self._record(message, level="muted", mission_id=mission_id,
                               title=title, detail="muted by quiet hours: " + detail)
            return row

        sig = hashlib.sha1(
            f"{mission_id}|{level}|{message}".encode()).hexdigest()[:12]
        now = time.time()
        if now - self._recent.get(sig, 0) < 1800:
            return None
        self._recent[sig] = now

        return self._record(message, level=level, mission_id=mission_id,
                            title=title, detail=detail, actions=actions)

    def _record(self, message: str, *, level: str, mission_id: str,
                title: str, detail: str, actions: list[str] | None = None) -> dict:
        row = {
            "id": f"n-{uuid.uuid4().hex[:10]}",
            "ts": time.time(),
            "level": level,
            "mission_id": mission_id,
            "title": str(title)[:140],
            "message": str(message)[:1000],
            "detail": str(detail)[:2000],
            "actions": list(actions or []),
            "read": False,
        }
        data = self._store.notifications.data.setdefault("notifications", [])
        data.append(row)
        self._store.notifications.save()
        if self._publish is not None and row["level"] != "muted":
            try:
                self._publish({"type": "notification", "notification": dict(row)})
            except Exception:
                pass
        return row

    def list(self, *, unread_only: bool = False, limit: int = 100) -> list[dict]:
        rows = self._store.notifications.rows()
        if unread_only:
            rows = [r for r in rows if not r.get("read")]
        return rows[-limit:][::-1]

    def mark_read(self, notification_id: str | None = None) -> int:
        data = self._store.notifications.data.setdefault("notifications", [])
        count = 0
        for r in data:
            if notification_id is None or r.get("id") == notification_id:
                if not r.get("read"):
                    r["read"] = True
                    count += 1
        if count:
            self._store.notifications.save()
        return count

    def pending_count(self) -> int:
        return sum(1 for r in self._store.notifications.rows() if not r.get("read"))
