"""Profile schema, validation, age derivation, immutable-field policy.

The birthdate is authoritative — age is always derived with full
month/day accounting, never ``current_year - birth_year``. Identity
fields (name, sex, birthdate) are immutable after creation; the backend
rejects edits to them rather than relying on disabled form fields.
"""
from __future__ import annotations

import re
import time
import unicodedata
import uuid
from datetime import date
from typing import Any


class ProfileError(ValueError):
    """Validation/authorization failure — message is safe to show users."""


# --- field sets -----------------------------------------------------------

IDENTITY_FIELDS = ("first_name", "last_name", "sex", "birth_date")
CONTACT_FIELDS = ("email", "phone")
LOCATION_FIELDS = ("street_address", "city", "state", "zip_code")
REQUIRED_FIELDS = (IDENTITY_FIELDS + CONTACT_FIELDS + LOCATION_FIELDS)

# Never editable after creation — name/sex/birthdate are legal identity,
# the protected fields are established only through Creator verification.
IMMUTABLE_FIELDS = frozenset(
    IDENTITY_FIELDS
    + ("profile_id", "created_at", "is_creator", "creator_role"))

# is_creator / creator_address / creator_title_* are protected: they can
# only be set by the creator-auth path, never via profile PATCH.
PROTECTED_FIELDS = frozenset(
    ("is_creator", "creator_role", "creator_address",
     "creator_title_greetings", "creator_title_conversation",
     "creator_title_notifications"))

EDITABLE_FIELDS = frozenset(
    CONTACT_FIELDS + LOCATION_FIELDS
    + ("avatar_path", "settings", "greeting_preference"))

SEXES = ("male", "female", "other")

_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[A-Za-z]{2,}$")
_PHONE_RE = re.compile(r"^\+?[0-9 ().\-x]{7,25}$")
_ZIP_RE = re.compile(r"^\d{5}(-\d{4})?$")
_NAME_RE = re.compile(r"^[^\d<>/\\]{1,60}$")


def normalize_name(text: str) -> str:
    """Trim + Unicode NFKC + casefold + collapse whitespace — the only
    way names are compared, so 'john  hamburn'/'JOHN HAMBURN' can't
    bypass the reserved Creator name."""
    t = unicodedata.normalize("NFKC", str(text or ""))
    t = " ".join(t.split())
    return t.casefold()


def compute_age(birth: date, today: date | None = None) -> int:
    """Age in full years — accounts for whether this year's birthday has
    occurred."""
    today = today or date.today()
    age = today.year - birth.year
    if (today.month, today.day) < (birth.month, birth.day):
        age -= 1
    return age


def is_adult_birthdate(birth: date, today: date | None = None) -> bool:
    return compute_age(birth, today) >= 18


def parse_birth_date(raw: Any) -> date:
    """Accept ISO 'YYYY-MM-DD' or {'year','month','day'} → date."""
    if isinstance(raw, dict):
        try:
            return date(int(raw["year"]), int(raw["month"]),
                        int(raw["day"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ProfileError("invalid birthdate") from exc
    try:
        return date.fromisoformat(str(raw))
    except (TypeError, ValueError) as exc:
        raise ProfileError("invalid birthdate") from exc


def validate_profile(data: dict[str, Any], *,
                     require_all: bool = True) -> dict[str, str]:
    """Validate create/patch input. Returns errors keyed by field —
    never throws for missing input when require_all is False (PATCH)."""
    errors: dict[str, str] = {}

    def need(f: str) -> bool:
        return require_all or f in data

    if need("first_name"):
        v = str(data.get("first_name") or "").strip()
        if not v or not _NAME_RE.match(v):
            errors["first_name"] = "required (letters, ≤60 chars)"
    if need("last_name"):
        v = str(data.get("last_name") or "").strip()
        if not v or not _NAME_RE.match(v):
            errors["last_name"] = "required (letters, ≤60 chars)"
    if need("sex"):
        if str(data.get("sex") or "").lower() not in SEXES:
            errors["sex"] = "required (male | female | other)"
    if need("birth_date"):
        try:
            b = parse_birth_date(data.get("birth_date"))
            if compute_age(b) < 0 or compute_age(b) > 130:
                errors["birth_date"] = "out of range"
        except ProfileError:
            errors["birth_date"] = "required (valid calendar date)"
    if need("email"):
        if not _EMAIL_RE.match(str(data.get("email") or "").strip()):
            errors["email"] = "valid email required"
    if need("phone"):
        if not _PHONE_RE.match(str(data.get("phone") or "").strip()):
            errors["phone"] = "valid phone required"
    if need("street_address"):
        if not str(data.get("street_address") or "").strip():
            errors["street_address"] = "required"
    if need("city"):
        if not str(data.get("city") or "").strip():
            errors["city"] = "required"
    if need("state"):
        v = str(data.get("state") or "").strip().upper()
        if not re.match(r"^[A-Z]{2}$", v):
            errors["state"] = "2-letter state code required"
    if need("zip_code"):
        if not _ZIP_RE.match(str(data.get("zip_code") or "").strip()):
            errors["zip_code"] = "5-digit ZIP required"
    return errors


def new_profile(fields: dict[str, Any]) -> dict[str, Any]:
    """Construct a validated profile record. Identity is normalized
    (trimmed, not mangled); birth_date stored as ISO. is_creator etc.
    start False — they can only be set by the Creator verify path."""
    errors = validate_profile(fields, require_all=True)
    if errors:
        raise ProfileError("; ".join(f"{k}: {v}" for k, v in errors.items()))
    now = time.time()
    return {
        "profile_id": uuid.uuid4().hex,
        "first_name": str(fields["first_name"]).strip(),
        "last_name": str(fields["last_name"]).strip(),
        "sex": str(fields["sex"]).strip().lower(),
        "birth_date": parse_birth_date(fields["birth_date"]).isoformat(),
        "email": str(fields["email"]).strip(),
        "phone": str(fields["phone"]).strip(),
        "street_address": str(fields["street_address"]).strip(),
        "city": str(fields["city"]).strip(),
        "state": str(fields["state"]).strip().upper(),
        "zip_code": str(fields["zip_code"]).strip(),
        "avatar_path": "",
        "created_at": now,
        "updated_at": now,
        "has_completed_intro": False,
        "is_creator": False,
        "creator_role": "",
        "creator_address": "",
        "creator_title_greetings": True,
        "creator_title_conversation": True,
        "creator_title_notifications": False,
        "settings": {},
    }


def public_profile(p: dict[str, Any]) -> dict[str, Any]:
    """API-facing view — adds derived fields, never exposes internal-only
    data. (The record itself holds no secrets.)"""
    out = dict(p)
    try:
        birth = date.fromisoformat(str(p.get("birth_date") or ""))
        out["age"] = compute_age(birth)
        out["is_adult"] = is_adult_birthdate(birth)
    except (TypeError, ValueError):
        out["age"] = None
        out["is_adult"] = False
    out["display_name"] = f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
    if not out.get("is_creator"):
        # Creator-only fields are invisible to ordinary profiles.
        out.pop("creator_address", None)
        out.pop("creator_title_greetings", None)
        out.pop("creator_title_conversation", None)
        out.pop("creator_title_notifications", None)
        out.pop("creator_role", None)
    return out
