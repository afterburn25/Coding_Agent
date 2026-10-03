"""User profiles — durable identity, onboarding lock, Creator identity.

A profile is the human's identity: immutable legal identity fields
(first/last/sex/birthdate), editable contact + location, an avatar, and
per-profile personality/voice/settings/memory. Profiles live under
``data/profiles/<uuid>/`` and never key data by name.

``ProfileManager`` owns creation, switching, and the onboarding gate;
``creator.py`` owns the reserved-name passcode flow; ``postal.py`` owns
offline US state/city/ZIP lookup behind a replaceable provider.
"""
from .manager import ProfileManager
from .model import (
    IMMUTABLE_FIELDS, ProfileError, compute_age, is_adult_birthdate,
    new_profile, normalize_name, validate_profile,
)

__all__ = [
    "ProfileManager", "ProfileError", "IMMUTABLE_FIELDS", "new_profile",
    "normalize_name", "validate_profile", "compute_age",
    "is_adult_birthdate",
]
