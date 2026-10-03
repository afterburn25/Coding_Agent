"""Creator identity — reserved-name gating + passcode verification.

The name ``John Hamburn`` (identity.NEXUS_CREATOR) is reserved: any
profile creation using it — in any casing/whitespace — must pass Creator
passcode verification, and may never become an ordinary profile.

Credential model:
- Bootstrap: a PBKDF2-HMAC-SHA256 verifier (salt+hash, no cleartext)
  ships in this module. The cleartext passcode is never in source,
  config, logs, APIs, or diagnostics.
- Enrollment: the first successful verification re-hashes the supplied
  passcode under a fresh random salt into ``data/creator_credential.json``
  (local credential storage), and the file becomes authoritative.
- Verification is always against the stored verifier — never name-only.
- Rate limiting: exponential backoff with a bounded persisted delay and
  attempt counter; success resets it. No permanent lockout; no oracle —
  every failure returns the same neutral error.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from pathlib import Path
from typing import Any

from ..fsutil import atomic_write_text
from ..identity import NEXUS_CREATOR
from .model import normalize_name

RESERVED_NAME = normalize_name(NEXUS_CREATOR)

KDF = "pbkdf2-sha256"
ITERATIONS = 600_000
MAX_BACKOFF_S = 30.0

# Bootstrap verifier: PBKDF2-HMAC-SHA256(passcode, salt, 600k). Only the
# digest ships — never the passcode itself.
_BOOTSTRAP = {
    "salt": "2100e39ac6c7c71be5d00da99eb29b37",
    "hash": "fac48b4efe3178c0132b34c1273aa37cea1a2ca5fdb58a15ace4e67843f50b1c",
}


def is_reserved_name(first_name: str, last_name: str) -> bool:
    """Normalized full-name match — 'john  hamburn', 'JOHN HAMBURN',
    whitespace/unicode tricks all collapse to the reserved name."""
    return normalize_name(f"{first_name} {last_name}") == RESERVED_NAME


def _pbkdf2(passcode: str, salt_hex: str, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha256", passcode.encode("utf-8"), bytes.fromhex(salt_hex),
        iterations)


class CreatorAuth:
    """Verifies the Creator passcode and records protected identity.

    State lives in ``data/creator_credential.json``: the verifier plus
    bounded rate-limit counters. The passcode itself is never stored,
    logged, or returned.
    """

    def __init__(self, root: Path) -> None:
        self.path = Path(root) / "creator_credential.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # -- verifier storage -------------------------------------------------

    def _load(self) -> dict[str, Any]:
        try:
            import json
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and raw.get("kdf") == KDF:
                return raw
        except Exception:
            pass
        # No enrolled credential yet — fall back to the packaged verifier.
        return {"kdf": KDF, "iterations": ITERATIONS,
                "salt": _BOOTSTRAP["salt"], "hash": _BOOTSTRAP["hash"],
                "bootstrap": True, "failed_attempts": 0, "locked_until": 0.0}

    def _save(self, state: dict[str, Any]) -> None:
        try:
            atomic_write_text(self.path, _json_dumps(state))
        except OSError:
            pass

    # -- rate limiting ------------------------------------------------------

    def locked_until(self) -> float:
        return float(self._load().get("locked_until") or 0.0)

    def verify(self, passcode: str, *, now: float | None = None) -> dict:
        """Attempt verification. Returns {ok, retry_after_s?, reason?} —
        failure reasons are neutral and identical for wrong passcodes."""
        now = now or time.time()
        state = self._load()
        locked_until = float(state.get("locked_until") or 0.0)
        if now < locked_until:
            return {"ok": False,
                    "retry_after_s": round(locked_until - now, 1),
                    "reason": "verification temporarily unavailable"}
        candidate = str(passcode or "")
        digest = _pbkdf2(candidate, str(state["salt"]),
                         int(state["iterations"]))
        ok = hmac.compare_digest(
            digest.hex(), str(state.get("hash") or ""))
        if ok:
            # Bootstrap → enroll: re-hash under a fresh salt into the
            # local credential file so the packaged verifier is retired.
            if state.get("bootstrap"):
                salt = secrets.token_bytes(16).hex()
                state = {
                    "kdf": KDF, "iterations": ITERATIONS,
                    "salt": salt,
                    "hash": _pbkdf2(candidate, salt, ITERATIONS).hex(),
                    "enrolled_at": now,
                }
            state["failed_attempts"] = 0
            state["locked_until"] = 0.0
            self._save(state)
            return {"ok": True}
        fails = int(state.get("failed_attempts") or 0) + 1
        state["failed_attempts"] = fails
        delay = min(2.0 ** fails, MAX_BACKOFF_S)
        state["locked_until"] = now + delay
        # Persist even in bootstrap mode — otherwise a restart resets
        # the rate limiter and allows unlimited pre-enrollment guessing.
        self._save(state)
        return {"ok": False, "retry_after_s": delay,
                "reason": "incorrect passcode"}


def _json_dumps(obj: Any) -> str:
    import json
    return json.dumps(obj, indent=2, sort_keys=True)


def creator_fields(passcode_ok: bool) -> dict[str, Any]:
    """The only place protected identity fields are produced — the caller
    must already have a successful verification."""
    if not passcode_ok:
        raise PermissionError("creator identity requires verification")
    return {"is_creator": True, "creator_role": "nexus_creator",
            "creator_address": "Father"}
