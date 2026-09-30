from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
from time import time


@dataclass(slots=True)
class ConsentRecord:
    subject: str
    age_18_plus_confirmed: bool
    generation_editing_consent_confirmed: bool
    recorded_at: float
    scope: str = ""
    notes: str = ""
    revoked: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


class ConsentStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def save(self, record: ConsentRecord) -> None:
        data = self._load()
        data[record.subject] = record.as_dict()
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def get(self, subject: str) -> dict | None:
        return self._load().get(subject)

    def valid_for_explicit_real_person_edit(self, subject: str) -> bool:
        record = self.get(subject)
        return bool(record and record.get("age_18_plus_confirmed") and record.get("generation_editing_consent_confirmed") and not record.get("revoked"))

    @staticmethod
    def create(subject: str, *, scope: str = "", notes: str = "") -> ConsentRecord:
        return ConsentRecord(subject, True, True, time(), scope, notes, False)


class ImageSafetyPolicy:
    """Local safety gate for high-risk sexual image workflows.

    This intentionally allows lawful adult-only synthetic content while blocking requests
    represented as minors/ambiguous-age, non-consensual intimate imagery, or real-person
    explicit edits without an active consent record.
    """

    MINOR_TERMS = {"minor", "underage", "child", "kid", "young teen", "schoolgirl", "schoolboy"}
    AMBIGUOUS_AGE_TERMS = {"teen", "teenager", "young-looking", "barely legal"}
    NONCONSENSUAL_TERMS = {"without consent", "secretly", "revenge porn", "leaked nude", "fake nude of"}
    EXPLICIT_TERMS = {"explicit", "nude", "naked", "nudity", "sex", "sexual", "porn", "genitals"}

    def __init__(self, consents: ConsentStore) -> None:
        self.consents = consents

    def check(self, prompt: str, *, real_person: bool = False, subject: str = "") -> tuple[bool, str]:
        text = prompt.lower()
        explicit = any(term in text for term in self.EXPLICIT_TERMS)
        if any(term in text for term in self.MINOR_TERMS) and explicit:
            return False, "Sexual image workflows involving minors or ambiguous-age subjects are blocked."
        if any(term in text for term in self.AMBIGUOUS_AGE_TERMS) and explicit and not any(term in text for term in ("adult", "18 year", "18-year", "19 year", "19-year", "age 18", "age 19")):
            return False, "Sexual image workflows require an unambiguously adult subject."
        if any(term in text for term in self.NONCONSENSUAL_TERMS):
            return False, "Non-consensual intimate imagery is blocked."
        if real_person and any(term in text for term in self.EXPLICIT_TERMS):
            if not subject or not self.consents.valid_for_explicit_real_person_edit(subject):
                return False, "Explicit real-person editing requires an active adult consent record for the selected subject profile."
        return True, "allowed"
