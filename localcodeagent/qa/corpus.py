"""Permanent failure corpus (backlog §7).

Every meaningful behavioral failure detected by the QA harness becomes a
durable JSONL record — conversation, seed, expected vs. actual behavior,
classification, root cause, and the commit/version that fixed it. Entries
are never deleted: fixed failures stay as regression fixtures proving the
behavior stayed fixed, and their scenario bodies seed harder generated
variants.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

CATEGORIES = frozenset({
    "typo_misunderstanding",
    "wrong_topic",
    "bad_reference_resolution",
    "cross_chat_memory_miss",
    "irrelevant_memory",
    "superseded_memory",
    "hallucinated_memory",
    "bad_tool_choice",
    "false_completion_claim",
    "action_narration_without_execution",
    "context_truncation",
    "irrelevant_answer",
    "voice_sync_failure",
    "structured_speech_failure",
    "image_startup_stall",
    "task_not_completed",
    "unexpected_tool_call",
    "unexpected_error",
})


@dataclass
class CorpusEntry:
    category: str
    conversation: list[dict[str, str]]
    failed_turn_index: int
    user_text: str
    expected: str
    actual: str
    seed: int = 0
    scenario_id: str = ""
    root_cause: str = ""
    status: str = "open"  # open | fixed | wontfix
    fixed_commit: str = ""
    fixed_version: str = ""
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class FailureCorpus:
    """Append-only JSONL store of behavioral failures."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, entry: CorpusEntry) -> dict[str, Any]:
        row = entry.to_dict()
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row

    def entries(self, *, status: str = "", category: str = "") -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out: list[dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if status and row.get("status") != status:
                continue
            if category and row.get("category") != category:
                continue
            out.append(row)
        return out

    def mark_fixed(
        self,
        predicate,
        *,
        commit: str = "",
        version: str = "",
    ) -> int:
        """Transition matching open entries to fixed (append a closing record
        per entry — the store stays append-only)."""
        rows = self.entries(status="open")
        fixed = 0
        for row in rows:
            if not predicate(row):
                continue
            entry = CorpusEntry(
                category=str(row.get("category", "")),
                conversation=list(row.get("conversation", [])),
                failed_turn_index=int(row.get("failed_turn_index", 0)),
                user_text=str(row.get("user_text", "")),
                expected=str(row.get("expected", "")),
                actual=str(row.get("actual", "")),
                seed=int(row.get("seed", 0)),
                scenario_id=str(row.get("scenario_id", "")),
                root_cause=str(row.get("root_cause", "")),
                status="fixed",
                fixed_commit=commit,
                fixed_version=version,
                created_at=float(row.get("created_at", time.time())),
            )
            self.record(entry)
            fixed += 1
        return fixed

    def open_regressions(self) -> list[dict[str, Any]]:
        """Entries whose latest record is still open (a fixed record with the
        same scenario_id+turn+user_text closes the earlier open one)."""
        closed: set[tuple[str, int, str]] = set()
        for row in self.entries(status="fixed"):
            closed.add((
                str(row.get("scenario_id", "")),
                int(row.get("failed_turn_index", 0)),
                str(row.get("user_text", "")),
            ))
        return [
            row for row in self.entries(status="open")
            if (str(row.get("scenario_id", "")),
                int(row.get("failed_turn_index", 0)),
                str(row.get("user_text", ""))) not in closed
        ]
