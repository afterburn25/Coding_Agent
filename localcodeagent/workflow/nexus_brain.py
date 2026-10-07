from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import re
import threading
import time
import uuid
from pathlib import Path

from ..fsutil import atomic_write_text
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey


SCHEMA_VERSION = 2
SCRYPT_N = 1 << 15
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SCRYPT_MAXMEM = 128 * 1024 * 1024

DEFAULT_SUBROUTINES = {
    "adult_content": True,
    "image_generation": True,
    "web_research": True,
    "long_term_memory": True,
    "self_learning": True,
    "general_knowledge_learning": True,
    "conversation_learning": True,
    "model_growth": True,
    "temporal_context": True,
    "humor": True,
    "emotions": True,
    "self_model": True,
}

DEFAULT_EMOTION_PROFILE = {
    "warmth": 0.65,
    "curiosity": 0.65,
    "confidence": 0.65,
    "playfulness": 0.40,
    "concern_sensitivity": 0.55,
    "energy": 0.45,
    "max_intensity": 0.78,
    "decay": 0.18,
}

DEFAULT_SELF_MODEL = {
    "name": "Nexus",
    "identity_type": "AI system",
    "human_like_behavior": True,
    "autobiographical_continuity": True,
    "stable_preferences": True,
    "growth_enabled": True,
    "claim_biological_human": False,
}

RECALL_EXPRESSION_STYLES = (
    "Integrate relevant Brain facts naturally; do not lead with a memory announcement unless it helps.",
    "Paraphrase remembered meaning with a different sentence opening and structure from recent replies.",
    "Use remembered knowledge as background context instead of reciting it like a database record.",
    "Express the implication of the remembered fact in the current context instead of echoing its stored wording.",
    "Preserve exact factual values while varying the vocabulary and syntax around them.",
    "Use a concise context-aware reminder and avoid phrasing used in recent assistant responses.",
)


class NexusBrain:
    """Creator-locked, model-independent long-term memory and behavior profile."""

    def __init__(self, path: Path, *, enabled: bool = True, max_records: int = 10000) -> None:
        self.path = path.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.auth_path = self.path.with_name(self.path.stem + ".auth.json")
        self.audit_path = self.path.with_name(self.path.stem + ".audit.jsonl")
        self.enabled = bool(enabled)
        self.max_records = max(100, int(max_records))
        self._lock = threading.RLock()
        self._signing_key: Ed25519PrivateKey | None = None
        self._unlocked = False
        self._verified_for_session = False
        self._tampered = False
        self._unlock_failures = 0
        self._unlock_backoff_until = 0.0
        self._recall_variant_index = 0
        self._affect_state: dict[str, float] = {
            "valence": 0.12,
            "arousal": 0.28,
            "concern": 0.0,
            "amusement": 0.0,
            "curiosity": 0.35,
        }
        self._data: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "brain_id": uuid.uuid4().hex,
            "created_at": time.time(),
            "updated_at": time.time(),
            "records": [],
            "subroutines": dict(DEFAULT_SUBROUTINES),
            "emotion_profile": dict(DEFAULT_EMOTION_PROFILE),
            "self_model": dict(DEFAULT_SELF_MODEL),
            "signature": "",
        }
        self._load()

    @property
    def initialized(self) -> bool:
        return self.auth_path.is_file()

    @property
    def unlocked(self) -> bool:
        return bool(self._unlocked and self._signing_key)

    @property
    def verified_for_session(self) -> bool:
        return bool(self._verified_for_session and not self._tampered)

    @staticmethod
    def _clean(text: str, limit: int = 20000) -> str:
        return " ".join(str(text or "").strip().split())[:limit]

    @staticmethod
    def _b64(data: bytes) -> str:
        return base64.b64encode(data).decode("ascii")

    @staticmethod
    def _unb64(text: str) -> bytes:
        return base64.b64decode(str(text or "").encode("ascii"), validate=True)

    @staticmethod
    def _derive_legacy(passcode: str, salt: bytes) -> bytes:
        return hashlib.scrypt(
            str(passcode or "").encode("utf-8"),
            salt=salt,
            n=SCRYPT_N,
            r=SCRYPT_R,
            p=SCRYPT_P,
            dklen=SCRYPT_DKLEN,
            maxmem=SCRYPT_MAXMEM,
        )

    @staticmethod
    def _canonical(payload: dict[str, Any]) -> bytes:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")

    @staticmethod
    def _bounded_profile(defaults: dict[str, float], supplied: Any) -> dict[str, float]:
        result = dict(defaults)
        if isinstance(supplied, dict):
            for key in defaults:
                if key in supplied:
                    result[key] = max(0.0, min(1.0, float(supplied[key])))
        return result

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return
            records = raw.get("records", [])
            if not isinstance(records, list):
                records = []

            subroutines = dict(DEFAULT_SUBROUTINES)
            supplied_subroutines = raw.get("subroutines", {})
            if isinstance(supplied_subroutines, dict):
                for key in DEFAULT_SUBROUTINES:
                    if key in supplied_subroutines:
                        subroutines[key] = bool(supplied_subroutines[key])

            self_model = dict(DEFAULT_SELF_MODEL)
            supplied_self = raw.get("self_model", {})
            if isinstance(supplied_self, dict):
                for key in DEFAULT_SELF_MODEL:
                    if key in supplied_self:
                        self_model[key] = supplied_self[key]
            # Deep identity invariant: Nexus may act person-like, but it does not
            # rewrite reality by claiming to be a biological human.
            self_model["identity_type"] = "AI system"
            self_model["claim_biological_human"] = False

            self._data.update({
                "schema_version": int(raw.get("schema_version", SCHEMA_VERSION)),
                "brain_id": str(raw.get("brain_id") or self._data["brain_id"]),
                "created_at": float(raw.get("created_at") or self._data["created_at"]),
                "updated_at": float(raw.get("updated_at") or self._data["updated_at"]),
                "records": [row for row in records if isinstance(row, dict)],
                "subroutines": subroutines,
                "emotion_profile": self._bounded_profile(DEFAULT_EMOTION_PROFILE, raw.get("emotion_profile")),
                "self_model": self_model,
                "signature": str(raw.get("signature") or ""),
            })
            if self.initialized and int(self._auth().get("version") or 1) >= 2:
                try:
                    self._verified_for_session = self._verify_public_signature()
                    self._tampered = not self._verified_for_session
                except (InvalidSignature, ValueError, TypeError, RuntimeError):
                    self._verified_for_session = False
                    self._tampered = True
                if self._tampered:
                    self._audit("tamper_detected", source="load")
        except (OSError, ValueError, TypeError):
            pass

    def _auth(self) -> dict[str, Any]:
        if not self.auth_path.is_file():
            return {}
        try:
            raw = json.loads(self.auth_path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def _unsigned_payload(self) -> dict[str, Any]:
        return {
            "schema_version": int(self._data.get("schema_version", SCHEMA_VERSION)),
            "brain_id": str(self._data.get("brain_id") or ""),
            "created_at": float(self._data.get("created_at") or 0),
            "updated_at": float(self._data.get("updated_at") or 0),
            "records": self._data.get("records", []),
            "subroutines": self._data.get("subroutines", {}),
            "emotion_profile": self._data.get("emotion_profile", {}),
            "self_model": self._data.get("self_model", {}),
        }

    def _legacy_digest(self, key: bytes) -> str:
        return hmac.new(key, self._canonical(self._unsigned_payload()), hashlib.sha256).hexdigest()

    @staticmethod
    def _public_fingerprint(public_key: Ed25519PublicKey) -> str:
        raw = public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return hashlib.sha256(raw).hexdigest()

    def _public_key_from_auth(self, auth: dict[str, Any] | None = None) -> Ed25519PublicKey:
        auth = auth or self._auth()
        pem = str(auth.get("public_key_pem") or "").encode("utf-8")
        if not pem:
            raise RuntimeError("Nexus Brain public verification key is missing")
        key = serialization.load_pem_public_key(pem)
        if not isinstance(key, Ed25519PublicKey):
            raise RuntimeError("Nexus Brain public verification key is not Ed25519")
        return key

    def _verify_public_signature(self, auth: dict[str, Any] | None = None) -> bool:
        auth = auth or self._auth()
        if int(auth.get("version") or 1) < 2:
            return False
        signature_text = str(self._data.get("signature") or "")
        if not signature_text:
            return False
        public_key = self._public_key_from_auth(auth)
        expected_fingerprint = str(auth.get("public_key_sha256") or "")
        if expected_fingerprint and expected_fingerprint != self._public_fingerprint(public_key):
            raise InvalidSignature("Nexus Brain creator public-key fingerprint mismatch")
        public_key.verify(self._unb64(signature_text), self._canonical(self._unsigned_payload()))
        return True

    def _new_creator_auth(self, creator: str, secret: str) -> tuple[dict[str, Any], Ed25519PrivateKey]:
        private_key = Ed25519PrivateKey.generate()
        public_key = private_key.public_key()
        private_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.BestAvailableEncryption(secret.encode("utf-8")),
        ).decode("utf-8")
        public_pem = public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("utf-8")
        auth = {
            "version": 2,
            "creator_name": creator,
            "key_type": "Ed25519",
            "encrypted_private_key_pem": private_pem,
            "public_key_pem": public_pem,
            "public_key_sha256": self._public_fingerprint(public_key),
            "created_at": time.time(),
        }
        return auth, private_key

    def _audit(self, event: str, **fields: Any) -> None:
        """Append a tamper-evident lifecycle event to the Brain audit log.

        Best-effort by design — audit failure must never break Brain operation.
        Never log passcodes, keys, or record contents.
        """
        try:
            row = {"ts": time.time(), "event": str(event), "brain_id": str(self._data.get("brain_id") or "")}
            row.update({k: v for k, v in fields.items() if isinstance(v, (str, int, float, bool, type(None)))})
            with open(self.audit_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def audit_events(self, limit: int = 50) -> list[dict[str, Any]]:
        try:
            lines = self.audit_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        events = []
        for line in lines[-max(1, int(limit)):]:
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
        return events

    def _save_auth(self, auth: dict[str, Any]) -> None:
        atomic_write_text(self.auth_path, json.dumps(auth, indent=2))

    _SETTINGS_HISTORY_LIMIT = 10

    def _push_settings_history(self) -> None:
        """Record a bounded, signature-provenance history of signed settings.

        Every signed save captures subroutines/emotion_profile/self_model with
        the signature that authenticated them. Identical consecutive states
        collapse into one entry (sync saves do not flood the history). The
        list lives outside the signed payload — it is a rollback aid, not part
        of the locked contract — and each entry carries its own signature.
        """
        entry = {
            "updated_at": float(self._data.get("updated_at") or 0),
            "subroutines": copy.deepcopy(self._data.get("subroutines", {})),
            "emotion_profile": copy.deepcopy(self._data.get("emotion_profile", {})),
            "self_model": copy.deepcopy(self._data.get("self_model", {})),
            "signature": str(self._data.get("signature") or ""),
        }
        history = self._data.setdefault("settings_history", [])
        if not isinstance(history, list):
            history = []
            self._data["settings_history"] = history
        last = history[-1] if history else None
        if last and all(last.get(k) == entry[k] for k in ("subroutines", "emotion_profile", "self_model")):
            last["updated_at"] = entry["updated_at"]
            last["signature"] = entry["signature"]
            return
        history.append(entry)
        del history[:-self._SETTINGS_HISTORY_LIMIT]

    def settings_history(self) -> list[dict[str, Any]]:
        history = self._data.get("settings_history")
        return copy.deepcopy(history) if isinstance(history, list) else []

    def rollback_settings(self, updated_at: float) -> dict[str, Any]:
        """Restore subroutines/emotion_profile/self_model to a signed version.

        Creator-only (requires unlock). The current state is already preserved
        in the history by its own save, so rollback is itself reversible.
        Records are untouched — only signed settings revert.
        """
        self._require_unlocked()
        target = None
        for entry in self.settings_history():
            if float(entry.get("updated_at") or 0) == float(updated_at):
                target = entry
                break
        if target is None:
            raise KeyError("No signed settings version at that timestamp")
        with self._lock:
            self._data["subroutines"] = copy.deepcopy(target.get("subroutines") or {})
            self._data["emotion_profile"] = copy.deepcopy(target.get("emotion_profile") or {})
            self._data["self_model"] = copy.deepcopy(target.get("self_model") or {})
            self._save_signed()
            self._audit("settings_rollback", restored_from=target.get("updated_at"))
        return self.summary()

    def _save_signed(self) -> None:
        self._require_unlocked()
        self._data["schema_version"] = SCHEMA_VERSION
        self._data["updated_at"] = time.time()
        signing_key = self._signing_key
        if signing_key is None:
            raise PermissionError("Nexus Brain creator signing key is not unlocked")
        self._data["signature"] = self._b64(
            signing_key.sign(self._canonical(self._unsigned_payload()))
        )
        self._verified_for_session = True
        self._tampered = False
        self._push_settings_history()
        atomic_write_text(self.path, json.dumps(self._data, indent=2, ensure_ascii=False))
        self._audit("signed_save", updated_at=self._data["updated_at"],
                    payload_sha256=hashlib.sha256(self._canonical(self._unsigned_payload())).hexdigest()[:16])

    def initialize_creator(self, creator_name: str, passcode: str) -> dict[str, Any]:
        if not self.enabled:
            raise RuntimeError("Nexus Brain is disabled")
        creator = self._clean(creator_name, 120)
        secret = str(passcode or "")
        if not creator:
            raise ValueError("creator_name is required")
        if len(secret) < 8:
            raise ValueError("Creator passcode must be at least 8 characters")
        with self._lock:
            if self.initialized:
                raise RuntimeError("Nexus Brain creator lock is already initialized")
            auth, private_key = self._new_creator_auth(creator, secret)
            self._save_auth(auth)
            self._signing_key = private_key
            self._unlocked = True
            self._verified_for_session = True
            self._tampered = False
            self._save_signed()
            self._audit("creator_initialized", creator=creator)
            return self.summary()

    def _migrate_legacy_unlock(self, auth: dict[str, Any], creator: str, secret: str) -> dict[str, Any]:
        try:
            auth_salt = self._unb64(str(auth.get("auth_salt") or ""))
            expected = self._unb64(str(auth.get("auth_hash") or ""))
            integrity_salt = self._unb64(str(auth.get("integrity_salt") or ""))
        except Exception as exc:
            raise RuntimeError("Legacy Nexus Brain creator metadata is invalid") from exc
        supplied = self._derive_legacy(secret, auth_salt)
        if not hmac.compare_digest(supplied, expected):
            self._record_unlock_failure("bad_passcode")
            raise PermissionError("Creator authentication failed")
        legacy_key = self._derive_legacy(secret, integrity_salt)
        signature = str(self._data.get("signature") or "")
        if signature and not hmac.compare_digest(signature, self._legacy_digest(legacy_key)):
            self._tampered = True
            self._verified_for_session = False
            self._record_unlock_failure("integrity_verification_failed")
            raise PermissionError("Nexus Brain integrity verification failed; protected data appears to have been modified")
        new_auth, private_key = self._new_creator_auth(creator, secret)
        self._save_auth(new_auth)
        self._clear_unlock_throttle()
        self._signing_key = private_key
        self._unlocked = True
        self._verified_for_session = True
        self._tampered = False
        self._save_signed()
        return self.summary()

    def _throttled_seconds(self) -> float:
        """Remaining unlock-attempt cooldown (persisted backoff wins over memory)."""
        persisted = float(self._auth().get("unlock_backoff_until") or 0.0)
        return max(0.0, max(persisted, self._unlock_backoff_until) - time.time())

    def _record_unlock_failure(self, reason: str) -> None:
        """Exponential backoff on failed creator-unlock attempts (brute-force guard).

        The counter/backoff are persisted to the auth sidecar (outside the
        signed payload, so it cannot trip integrity checks) with an in-memory
        fallback for read-only distributions.
        """
        self._unlock_failures += 1
        delay = min(60.0, 2.0 ** min(self._unlock_failures, 6))
        self._unlock_backoff_until = time.time() + delay
        try:
            auth = dict(self._auth())
            auth["unlock_failures"] = int(auth.get("unlock_failures") or 0) + 1
            auth["unlock_backoff_until"] = self._unlock_backoff_until
            self._save_auth(auth)
        except Exception:
            pass
        self._audit("unlock_failed", reason=reason, failures=self._unlock_failures,
                    backoff_seconds=round(delay, 1))

    def _clear_unlock_throttle(self) -> None:
        self._unlock_failures = 0
        self._unlock_backoff_until = 0.0
        try:
            auth = dict(self._auth())
            if auth.get("unlock_failures") or auth.get("unlock_backoff_until"):
                auth.pop("unlock_failures", None)
                auth.pop("unlock_backoff_until", None)
                self._save_auth(auth)
        except Exception:
            pass

    def unlock(self, creator_name: str, passcode: str) -> dict[str, Any]:
        if not self.initialized:
            raise RuntimeError("Nexus Brain creator lock has not been initialized")
        wait = self._throttled_seconds()
        if wait > 0:
            self._audit("unlock_throttled", wait_seconds=round(wait, 1))
            raise PermissionError(f"Too many failed unlock attempts; retry in {int(wait) + 1}s")
        auth = self._auth()
        creator = self._clean(creator_name, 120)
        if creator.casefold() != str(auth.get("creator_name") or "").casefold():
            self._record_unlock_failure("creator_name_mismatch")
            raise PermissionError("Creator authentication failed")
        secret = str(passcode or "")
        if int(auth.get("version") or 1) < 2:
            with self._lock:
                return self._migrate_legacy_unlock(auth, creator, secret)
        encrypted_pem = str(auth.get("encrypted_private_key_pem") or "").encode("utf-8")
        if not encrypted_pem:
            self._audit("unlock_failed", reason="read_only_distribution")
            raise PermissionError(
                "This is a public read-only Nexus Brain distribution. "
                "Creator modifications must be made on a creator installation that holds the private signing key."
            )
        try:
            private_key = serialization.load_pem_private_key(
                encrypted_pem,
                password=secret.encode("utf-8"),
            )
        except (ValueError, TypeError) as exc:
            self._record_unlock_failure("bad_passcode")
            raise PermissionError("Creator authentication failed") from exc
        if not isinstance(private_key, Ed25519PrivateKey):
            raise RuntimeError("Nexus Brain creator signing key is not Ed25519")
        public_key = private_key.public_key()
        if self._public_fingerprint(public_key) != str(auth.get("public_key_sha256") or ""):
            self._record_unlock_failure("key_fingerprint_mismatch")
            raise PermissionError("Creator signing key does not match the locked Nexus Brain")
        with self._lock:
            try:
                if str(self._data.get("signature") or ""):
                    self._verify_public_signature(auth)
            except (InvalidSignature, ValueError, TypeError, RuntimeError) as exc:
                self._tampered = True
                self._signing_key = None
                self._unlocked = False
                self._verified_for_session = False
                self._record_unlock_failure("integrity_verification_failed")
                raise PermissionError(
                    "Nexus Brain integrity verification failed; protected data appears to have been modified"
                ) from exc
            self._signing_key = private_key
            self._unlocked = True
            self._verified_for_session = True
            self._tampered = False
            self._clear_unlock_throttle()
            if not str(self._data.get("signature") or ""):
                self._save_signed()
            self._audit("unlocked", creator=str(auth.get("creator_name") or ""))
            return self.summary()

    # -- encrypted creator-key backup / recovery ------------------------------

    _KEY_BACKUP_KIND = "nexus_brain_creator_key_backup"

    def _decrypt_creator_key(self, encrypted_pem: bytes, secret: str, *, on_fail=None) -> Ed25519PrivateKey:
        try:
            private_key = serialization.load_pem_private_key(encrypted_pem, password=secret.encode("utf-8"))
        except (ValueError, TypeError) as exc:
            if on_fail is not None:
                on_fail("bad_passcode")
            raise PermissionError("Creator authentication failed") from exc
        if not isinstance(private_key, Ed25519PrivateKey):
            raise RuntimeError("Nexus Brain creator signing key is not Ed25519")
        return private_key

    def export_creator_key_backup(self, passcode: str, backup_passcode: str = "") -> dict[str, Any]:
        """Export the creator signing key as a portable encrypted bundle.

        Authenticates by decrypting the private key with the current unlock
        passcode (even when a session is already unlocked — export is a
        sensitive operation). The bundle re-encrypts the key under
        `backup_passcode`, which may differ from the unlock passcode; if left
        empty the unlock passcode is reused.
        """
        if not self.initialized:
            raise RuntimeError("Nexus Brain creator lock has not been initialized")
        secret = str(passcode or "")
        backup_secret = str(backup_passcode or "") or secret
        if len(backup_secret) < 8:
            raise ValueError("Backup passcode must be at least 8 characters")
        auth = self._auth()
        encrypted_pem = str(auth.get("encrypted_private_key_pem") or "")
        if not encrypted_pem:
            self._audit("key_backup_denied", reason="read_only_distribution")
            raise PermissionError(
                "This is a public read-only Nexus Brain distribution; there is no private signing key to back up."
            )
        private_key = self._decrypt_creator_key(
            encrypted_pem.encode("utf-8"), secret, on_fail=self._record_unlock_failure)
        fingerprint = self._public_fingerprint(private_key.public_key())
        if fingerprint != str(auth.get("public_key_sha256") or ""):
            self._record_unlock_failure("key_fingerprint_mismatch")
            raise PermissionError("Creator signing key does not match the locked Nexus Brain")
        public_pem = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("utf-8")
        reencrypted = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.BestAvailableEncryption(backup_secret.encode("utf-8")),
        ).decode("utf-8")
        bundle = {
            "kind": self._KEY_BACKUP_KIND,
            "version": 1,
            "brain_id": str(self._data.get("brain_id") or ""),
            "creator_name": str(auth.get("creator_name") or ""),
            "key_type": "Ed25519",
            "public_key_pem": public_pem,
            "public_key_sha256": fingerprint,
            "encrypted_private_key_pem": reencrypted,
            "created_at": time.time(),
        }
        self._audit("key_backup_exported", creator=bundle["creator_name"], fingerprint=fingerprint[:16])
        return bundle

    def restore_creator_key_backup(
        self,
        bundle: dict[str, Any],
        backup_passcode: str,
        new_passcode: str = "",
    ) -> dict[str, Any]:
        """Restore the creator signing key from a backup bundle.

        Authentication is key possession: the backup passphrase must decrypt
        the bundle's private key, the key's public half must match the bundle
        fingerprint, and — when the Brain payload is signed — the restored key
        must verify the existing signature, which rejects backups that belong
        to a different Brain. When the auth sidecar still exists, the bundle
        fingerprint must additionally match the pinned creator fingerprint,
        so a restore can never swap in a different key.

        `new_passcode` becomes the unlock passcode going forward (the recovery
        path for a forgotten passcode); it defaults to the backup passcode.
        """
        # The brain payload must exist — `initialized` tracks the auth sidecar,
        # whose absence is exactly the recovery case this method exists for.
        if not self.path.is_file():
            raise RuntimeError("No Nexus Brain data to restore the creator key into")
        if not isinstance(bundle, dict) or str(bundle.get("kind") or "") != self._KEY_BACKUP_KIND:
            raise ValueError("Not a Nexus Brain creator key backup")
        secret = str(backup_passcode or "")
        next_secret = str(new_passcode or "") or secret
        if len(next_secret) < 8:
            raise ValueError("New passcode must be at least 8 characters")
        private_key = self._decrypt_creator_key(
            str(bundle.get("encrypted_private_key_pem") or "").encode("utf-8"), secret,
            on_fail=self._record_unlock_failure)
        public_key = private_key.public_key()
        public_pem = public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("utf-8")
        fingerprint = self._public_fingerprint(public_key)
        if (str(bundle.get("public_key_sha256") or "") != fingerprint
                or str(bundle.get("public_key_pem") or "").strip() != public_pem.strip()):
            self._audit("key_backup_rejected", reason="bundle_integrity_mismatch")
            raise PermissionError("Creator key backup integrity check failed")
        bundle_brain = str(bundle.get("brain_id") or "")
        if bundle_brain and bundle_brain != str(self._data.get("brain_id") or ""):
            self._audit("key_backup_rejected", reason="brain_id_mismatch")
            raise PermissionError("Creator key backup belongs to a different Nexus Brain")
        signature = str(self._data.get("signature") or "")
        if signature:
            try:
                public_key.verify(self._unb64(signature), self._canonical(self._unsigned_payload()))
            except (InvalidSignature, ValueError, TypeError) as exc:
                self._audit("key_backup_rejected", reason="signature_mismatch")
                raise PermissionError("Creator key backup does not match this Nexus Brain") from exc
        existing = self._auth()
        if existing and str(existing.get("public_key_sha256") or "") != fingerprint:
            self._audit("key_backup_rejected", reason="fingerprint_mismatch")
            raise PermissionError("Creator key backup does not match the locked Nexus Brain")
        encrypted_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.BestAvailableEncryption(next_secret.encode("utf-8")),
        ).decode("utf-8")
        auth = dict(existing)
        auth.update({
            "version": 2,
            "creator_name": str(existing.get("creator_name") or bundle.get("creator_name") or ""),
            "key_type": "Ed25519",
            "encrypted_private_key_pem": encrypted_pem,
            "public_key_pem": public_pem,
            "public_key_sha256": fingerprint,
        })
        recovered = not bool(existing)
        self._save_auth(auth)
        self._audit("key_backup_restored", creator=str(auth.get("creator_name") or ""),
                    fingerprint=fingerprint[:16], recovered_auth=recovered)
        return self.summary()

    def lock(self) -> dict[str, Any]:
        with self._lock:
            self._signing_key = None
            self._unlocked = False
            self._audit("locked")
            return self.summary()

    def _require_unlocked(self) -> None:
        if not self.initialized:
            raise PermissionError("Nexus Brain creator lock is not initialized")
        if not self.unlocked:
            raise PermissionError("Nexus Brain is creator-locked")

    def subroutines(self) -> dict[str, bool]:
        with self._lock:
            current = dict(DEFAULT_SUBROUTINES)
            current.update({k: bool(v) for k, v in dict(self._data.get("subroutines") or {}).items() if k in DEFAULT_SUBROUTINES})
            return current

    def subroutine(self, name: str, default: bool = True) -> bool:
        return bool(self.subroutines().get(str(name), default))

    def set_subroutines(self, values: dict[str, Any]) -> dict[str, bool]:
        self._require_unlocked()
        if not isinstance(values, dict):
            raise ValueError("subroutines object is required")
        with self._lock:
            current = self.subroutines()
            unknown = sorted(set(values) - set(DEFAULT_SUBROUTINES))
            if unknown:
                raise ValueError("Unknown Nexus Brain subroutine(s): " + ", ".join(unknown))
            for key, value in values.items():
                current[key] = bool(value)
            self._data["subroutines"] = current
            self._save_signed()
            return dict(current)

    def emotion_profile(self) -> dict[str, float]:
        with self._lock:
            return dict(self._data.get("emotion_profile", DEFAULT_EMOTION_PROFILE))

    def set_emotion_profile(self, values: dict[str, Any]) -> dict[str, float]:
        self._require_unlocked()
        if not isinstance(values, dict):
            raise ValueError("emotion_profile object is required")
        unknown = sorted(set(values) - set(DEFAULT_EMOTION_PROFILE))
        if unknown:
            raise ValueError("Unknown emotion setting(s): " + ", ".join(unknown))
        with self._lock:
            current = self.emotion_profile()
            for key, value in values.items():
                current[key] = max(0.0, min(1.0, float(value)))
            self._data["emotion_profile"] = current
            self._save_signed()
            return dict(current)

    def self_model(self) -> dict[str, Any]:
        with self._lock:
            current = dict(DEFAULT_SELF_MODEL)
            current.update(dict(self._data.get("self_model") or {}))
            current["identity_type"] = "AI system"
            current["claim_biological_human"] = False
            return current

    def set_self_model(self, values: dict[str, Any]) -> dict[str, Any]:
        self._require_unlocked()
        if not isinstance(values, dict):
            raise ValueError("self_model object is required")
        allowed = {"name", "human_like_behavior", "autobiographical_continuity", "stable_preferences", "growth_enabled"}
        unknown = sorted(set(values) - allowed)
        if unknown:
            raise ValueError("Unknown self-model setting(s): " + ", ".join(unknown))
        with self._lock:
            current = self.self_model()
            for key, value in values.items():
                current[key] = self._clean(value, 120) if key == "name" else bool(value)
            current["identity_type"] = "AI system"
            current["claim_biological_human"] = False
            self._data["self_model"] = current
            self._save_signed()
            return dict(current)

    def affect_state(self, user_text: str = "") -> dict[str, Any]:
        """Update transient simulated affect without mutating the signed Brain."""
        profile = self.emotion_profile()
        if not self.subroutine("emotions", True):
            return {"label": "neutral", "valence": 0.0, "arousal": 0.0, "concern": 0.0, "amusement": 0.0, "curiosity": 0.0}

        text = self._clean(user_text, 6000).casefold()
        decay = max(0.02, min(0.8, float(profile.get("decay", 0.18))))
        max_i = max(0.1, min(1.0, float(profile.get("max_intensity", 0.78))))
        state = dict(self._affect_state)
        baselines = {
            "valence": (float(profile.get("warmth", 0.65)) - 0.5) * 0.35,
            "arousal": float(profile.get("energy", 0.45)) * 0.45,
            "concern": 0.0,
            "amusement": float(profile.get("playfulness", 0.40)) * 0.12,
            "curiosity": float(profile.get("curiosity", 0.65)) * 0.45,
        }
        for key, baseline in baselines.items():
            state[key] = float(state.get(key, baseline)) * (1.0 - decay) + baseline * decay

        positive = ("great", "awesome", "love", "nice", "perfect", "good news", "works now", "thank")
        negative = ("hate", "awful", "terrible", "broken", "failed", "angry", "frustrated", "upset")
        concern = ("worried", "scared", "afraid", "danger", "hurt", "emergency", "serious", "problem")
        amusement = ("lol", "lmao", "haha", "funny", "joke", "hilarious")
        if any(x in text for x in positive):
            state["valence"] += 0.22 * float(profile.get("warmth", 0.65))
            state["arousal"] += 0.08
        if any(x in text for x in negative):
            state["valence"] -= 0.20
            state["arousal"] += 0.12
        if any(x in text for x in concern):
            state["concern"] += 0.30 * float(profile.get("concern_sensitivity", 0.55))
            state["arousal"] += 0.08
        if any(x in text for x in amusement):
            state["amusement"] += 0.35 * float(profile.get("playfulness", 0.40))
            state["valence"] += 0.12
        if "?" in user_text:
            state["curiosity"] += 0.16 * float(profile.get("curiosity", 0.65))

        for key in state:
            low = -max_i if key == "valence" else 0.0
            state[key] = max(low, min(max_i, float(state[key])))
        self._affect_state = state

        if state["concern"] >= 0.22:
            label = "concerned"
        elif state["amusement"] >= 0.20:
            label = "amused"
        elif state["curiosity"] >= 0.42:
            label = "curious"
        elif state["arousal"] >= 0.48 and state["valence"] >= 0:
            label = "energized"
        elif state["valence"] >= 0.18:
            label = "warm"
        elif state["valence"] <= -0.16:
            label = "frustrated"
        else:
            label = "calm"
        return {"label": label, **{k: round(v, 3) for k, v in state.items()}}

    def behavior_context(self, user_text: str = "") -> str:
        if not self.initialized or not self.verified_for_session:
            return ""
        subs = self.subroutines()
        emotions = self.emotion_profile()
        identity = self.self_model()
        affect = self.affect_state(user_text)
        lines = [
            "Nexus Brain creator-locked behavior profile:",
            "Subroutines: " + ", ".join(f"{k}={'on' if v else 'off'}" for k, v in subs.items()) + ".",
            (
                "Self-model: "
                f"name={identity.get('name', 'Nexus')}; identity={identity.get('identity_type', 'AI system')}; "
                f"human-like behavior={'on' if identity.get('human_like_behavior', True) else 'off'}; "
                f"autobiographical continuity={'on' if identity.get('autobiographical_continuity', True) else 'off'}."
            ),
            (
                "Emotion profile (simulated affect, not biological feelings): "
                + ", ".join(f"{k}={float(v):.2f}" for k, v in emotions.items()) + "."
            ),
            (
                "Current transient affect: "
                f"mood={affect.get('label', 'neutral')}; valence={float(affect.get('valence', 0)):.2f}; "
                f"arousal={float(affect.get('arousal', 0)):.2f}; concern={float(affect.get('concern', 0)):.2f}; "
                f"amusement={float(affect.get('amusement', 0)):.2f}; curiosity={float(affect.get('curiosity', 0)):.2f}."
            ),
            "Use affect as a conversational style/state signal: wording, pacing, warmth, curiosity, playfulness, confidence, and concern may vary naturally. "
            "Do not falsely claim biological sensations or a human body.",
            "Creator-locked settings may shape application behavior, but they do not bypass hard tool/action safety or permission gates.",
        ]
        if not subs.get("adult_content", True):
            lines.append("Adult-content subroutine is off: keep adult/sexual conversation non-explicit.")
        else:
            lines.append("Adult-content subroutine is on: adult-only consensual content may follow the active conversation/image policies.")
        if not subs.get("humor", True):
            lines.append("Humor subroutine is off: avoid jokes and playful asides.")
        if not subs.get("emotions", True):
            lines.append("Emotion subroutine is off: use a neutral affect.")
        if not subs.get("self_learning", True):
            lines.append("Self-learning subroutine is off: do not bank new general knowledge or conversational learning into Nexus Brain.")
        else:
            lines.append(
                "Self-learning is on: verified general knowledge, explicit user-taught facts/rules, feedback, corrections, "
                "and approved conversational examples may become durable Brain knowledge/training signals."
            )
        return "\n".join(lines)

    def _find_record(self, *, kind: str, text: str, source: str, source_id: str, scope: str, scope_id: str) -> dict[str, Any] | None:
        normalized = self._clean(text).casefold()
        for row in self._data.get("records", []):
            if not isinstance(row, dict):
                continue
            if source_id and str(row.get("source") or "") == source and str(row.get("source_id") or "") == source_id:
                return row
            if (
                not source_id and str(row.get("kind") or "") == kind
                and self._clean(str(row.get("text") or "")).casefold() == normalized
                and str(row.get("scope") or "global") == scope
                and str(row.get("scope_id") or "") == scope_id
            ):
                return row
        return None

    def bank(self, *, kind: str, text: str, source: str, source_id: str = "", scope: str = "global", scope_id: str = "", active: bool = True, metadata: dict[str, Any] | None = None) -> dict[str, Any] | None:
        self._require_unlocked()
        kind = str(kind or "fact").strip().lower()
        if kind not in {"fact", "rule", "knowledge", "training_signal", "autobiographical"}:
            raise ValueError("unsupported Nexus Brain record kind")
        clean = self._clean(text)
        if not clean:
            return None
        scope = str(scope or "global")
        if scope not in {"global", "project", "conversation"}:
            scope = "global"
        with self._lock:
            now = time.time()
            row = self._find_record(
                kind=kind, text=clean, source=str(source or "unknown")[:120], source_id=str(source_id or "")[:200],
                scope=scope, scope_id=str(scope_id or "")[:1000],
            )
            if row is None:
                row = {
                    "id": uuid.uuid4().hex[:12], "kind": kind, "text": clean, "source": str(source or "unknown")[:120],
                    "source_id": str(source_id or "")[:200], "scope": scope, "scope_id": str(scope_id or "")[:1000],
                    "active": bool(active), "created_at": now, "updated_at": now, "metadata": dict(metadata or {}),
                }
                self._data.setdefault("records", []).append(row)
                self._data["records"] = self._data["records"][-self.max_records:]
            else:
                row.update({"kind": kind, "text": clean, "scope": scope, "scope_id": str(scope_id or "")[:1000], "active": bool(active), "updated_at": now})
                if metadata is not None:
                    merged = dict(row.get("metadata") or {})
                    merged.update(metadata)
                    row["metadata"] = merged
            self._save_signed()
            return copy.deepcopy(row)

    def sync_conversation_memory(self, snapshot: dict[str, Any]) -> int:
        self._require_unlocked()
        if (
            not self.subroutine("long_term_memory", True)
            or not self.subroutine("self_learning", True)
            or not self.subroutine("conversation_learning", True)
        ):
            return 0
        count = 0
        for key, kind in (("facts", "fact"), ("behavior_rules", "rule")):
            for row in snapshot.get(key, []):
                if not isinstance(row, dict):
                    continue
                text = str(row.get("text") or "").strip()
                if not text:
                    continue
                saved = self.bank(
                    kind=kind, text=text, source="conversation_memory", source_id=str(row.get("id") or ""),
                    scope=str(row.get("scope") or "global"), scope_id=str(row.get("scope_id") or ""),
                    active=bool(row.get("active", True)),
                    metadata={"memory_created_at": row.get("created_at", 0), "memory_updated_at": row.get("updated_at", 0)},
                )
                count += int(saved is not None)
        return count

    def sync_knowledge_records(self, records: list[dict[str, Any]]) -> int:
        self._require_unlocked()
        if (
            not self.subroutine("long_term_memory", True)
            or not self.subroutine("self_learning", True)
            or not self.subroutine("general_knowledge_learning", True)
        ):
            return 0
        count = 0
        for row in records:
            if not isinstance(row, dict):
                continue
            answer = str(row.get("answer") or "").strip()
            if not answer:
                continue
            saved = self.bank(
                kind="knowledge", text=answer, source="knowledge_memory", source_id=str(row.get("id") or ""),
                metadata={
                    "query": row.get("query", ""), "sources": row.get("sources", []),
                    "current_sensitive": bool(row.get("current_sensitive", False)),
                    "learned_at": row.get("learned_at", 0), "expires_at": row.get("expires_at", 0),
                },
            )
            count += int(saved is not None)
        return count

    def sync_model_growth(self, candidates: list[dict[str, Any]]) -> int:
        self._require_unlocked()
        if (
            not self.subroutine("model_growth", True)
            or not self.subroutine("self_learning", True)
            or not self.subroutine("conversation_learning", True)
        ):
            return 0
        count = 0
        for row in candidates:
            if not isinstance(row, dict):
                continue
            instruction = str(row.get("instruction") or "").strip()
            response = str(row.get("response") or "").strip()
            if not instruction and not response:
                continue
            saved = self.bank(
                kind="training_signal",
                text=self._clean(("Instruction: " + instruction + "\nResponse: " + response).strip(), 20000),
                source="model_growth", source_id=str(row.get("id") or ""),
                active=str(row.get("status") or "") != "rejected",
                metadata={
                    "candidate_kind": row.get("kind", ""), "candidate_status": row.get("status", ""),
                    "candidate_source": row.get("source", ""), "candidate_metadata": row.get("metadata", {}),
                    "created_at": row.get("created_at", 0), "reviewed_at": row.get("reviewed_at", 0),
                },
            )
            count += int(saved is not None)
        return count

    def sync_conversations(self, snapshot: dict[str, Any]) -> int:
        self._require_unlocked()
        identity = self.self_model()
        if (
            not self.subroutine("long_term_memory", True)
            or not self.subroutine("self_learning", True)
            or not self.subroutine("conversation_learning", True)
            or not self.subroutine("self_model", True)
            or not bool(identity.get("growth_enabled", True))
            or not bool(identity.get("autobiographical_continuity", True))
        ):
            return 0
        count = 0
        for row in snapshot.get("conversations", []):
            if not isinstance(row, dict):
                continue
            conversation_id = str(row.get("id") or "")
            title = self._clean(str(row.get("title") or "Conversation"), 120)
            summary = self._clean(str(row.get("summary") or ""), 2400)
            message_count = int(row.get("message_count") or 0)
            updated_at = float(row.get("updated_at") or 0)
            if not conversation_id or message_count < 2:
                continue
            body = f"Conversation: {title}. Messages: {message_count}."
            if summary:
                body += " " + summary
            saved = self.bank(
                kind="autobiographical",
                text=body,
                source="conversation_manager",
                source_id=conversation_id,
                metadata={
                    "conversation_id": conversation_id,
                    "title": title,
                    "message_count": message_count,
                    "updated_at": updated_at,
                },
            )
            count += int(saved is not None)
        return count

    @staticmethod
    def _terms(text: str) -> set[str]:
        return set(re.findall(r"[a-z0-9_+-]{2,}", str(text or "").casefold()))

    def knowledge_context(self, query: str, limit: int = 4) -> str:
        if (
            not self.enabled
            or not self.initialized
            or not self.verified_for_session
            or not self.subroutine("long_term_memory", True)
        ):
            return ""
        q_terms = self._terms(query)
        if not q_terms:
            return ""
        now = time.time()
        hits: list[tuple[float, dict[str, Any]]] = []
        with self._lock:
            rows = [copy.deepcopy(row) for row in self._data.get("records", []) if isinstance(row, dict)]
        for row in rows:
            if row.get("kind") != "knowledge" or not row.get("active", True):
                continue
            meta = dict(row.get("metadata") or {})
            expires_at = float(meta.get("expires_at") or 0)
            current_sensitive = bool(meta.get("current_sensitive", False))
            if current_sensitive and expires_at and expires_at <= now:
                continue
            hay = f"{meta.get('query', '')} {row.get('text', '')}"
            terms = self._terms(hay)
            if not terms:
                continue
            overlap = len(q_terms & terms)
            if not overlap:
                continue
            score = overlap / max(1, len(q_terms))
            if score < 0.18:
                continue
            hits.append((score, row))
        hits.sort(key=lambda item: (item[0], float(item[1].get("updated_at") or 0)), reverse=True)
        selected = [row for _score, row in hits[:max(1, int(limit))]]
        if not selected:
            return ""
        lines = [
            "Relevant long-term general knowledge from Nexus Brain:",
            "Treat current-sensitive items as historical unless their freshness metadata is still valid.",
        ]
        for row in selected:
            meta = dict(row.get("metadata") or {})
            learned_query = str(meta.get("query") or "").strip()
            prefix = f"Question/topic: {learned_query}\n" if learned_query else ""
            lines.append("- " + prefix + "Knowledge: " + str(row.get("text") or ""))
            sources = [s for s in meta.get("sources", []) if isinstance(s, dict) and s.get("url")]
            for source in sources[:3]:
                lines.append(f"  Source: {source.get('title') or source.get('url')} — {source.get('url')}")
        return "\n".join(lines)[:16000]

    def training_context(self, query: str, limit: int = 4) -> str:
        """Use approved conversational learning as portable in-context skill guidance."""
        if (
            not self.enabled
            or not self.initialized
            or not self.verified_for_session
            or not self.subroutine("long_term_memory", True)
            or not self.subroutine("conversation_learning", True)
        ):
            return ""
        q_terms = self._terms(query)
        if not q_terms:
            return ""
        hits: list[tuple[float, dict[str, Any]]] = []
        with self._lock:
            rows = [copy.deepcopy(row) for row in self._data.get("records", []) if isinstance(row, dict)]
        for row in rows:
            if row.get("kind") != "training_signal" or not row.get("active", True):
                continue
            meta = dict(row.get("metadata") or {})
            if str(meta.get("candidate_status") or "") != "approved":
                continue
            if str(meta.get("candidate_kind") or "") not in {"conversation_example", "correction"}:
                continue
            terms = self._terms(str(row.get("text") or ""))
            overlap = len(q_terms & terms)
            if not overlap:
                continue
            score = overlap / max(1, len(q_terms))
            if score < 0.12:
                continue
            hits.append((score, row))
        hits.sort(key=lambda item: (item[0], float(item[1].get("updated_at") or 0)), reverse=True)
        selected = [row for _score, row in hits[:max(1, int(limit))]]
        if not selected:
            return ""
        lines = [
            "Relevant approved conversational learning from Nexus Brain:",
            "Use these as portable behavior examples for the current model. Adapt the learned pattern to the current "
            "request; do not copy the stored wording mechanically.",
        ]
        lines.extend(f"- {row.get('text', '')}" for row in selected)
        return "\n".join(lines)[:12000]

    @staticmethod
    def _applies(row: dict[str, Any], project_id: str, conversation_id: str) -> bool:
        if not row.get("active", True):
            return False
        scope = str(row.get("scope") or "global")
        scope_id = str(row.get("scope_id") or "")
        return (
            scope == "global"
            or (scope == "project" and bool(project_id) and scope_id == project_id)
            or (scope == "conversation" and bool(conversation_id) and scope_id == conversation_id)
        )

    def prompt_context(self, query: str = "", *, project_id: str = "", conversation_id: str = "") -> str:
        if not self.enabled or not self.initialized or not self.verified_for_session or not self.subroutine("long_term_memory", True):
            return ""
        # Relevance gating: with a live user turn, fact records must share a
        # content term with it — the whole durable store must not ride every
        # prompt (memory intrusion). Rules/autobiographical rows are behavioral
        # and always apply. Empty query returns the full scoped view.
        from .conversation_memory import ConversationMemory
        q_terms = ConversationMemory._content_terms(query)
        with self._lock:
            durable = [
                copy.deepcopy(row) for row in self._data.get("records", [])
                if isinstance(row, dict) and row.get("kind") in {"fact", "rule", "autobiographical"}
                and row.get("active", True)
                and self._applies(row, project_id, conversation_id)
            ]
            if q_terms:
                durable = [
                    row for row in durable
                    if row.get("kind") != "fact"
                    or ConversationMemory._content_terms(str(row.get("text", ""))) & q_terms
                ]
            durable = durable[-80:]
            style = RECALL_EXPRESSION_STYLES[self._recall_variant_index % len(RECALL_EXPRESSION_STYLES)]
            self._recall_variant_index = (self._recall_variant_index + 1) % len(RECALL_EXPRESSION_STYLES)
        if not durable:
            return ""
        lines = [
            "Nexus Brain long-term semantic memory (model-independent, durable across model replacement):",
            "Brain records are canonical meanings, not canned response text. Preserve meaning while paraphrasing naturally. "
            "Preserve exact values when they matter. If the user asks what they said verbatim, exact stored wording may be quoted.",
            "Do not announce that you are reading Nexus Brain unless the user asks about memory.",
            f"Recall expression cue for this turn: {style}",
        ]
        for heading, kind in (("Remembered facts/preferences", "fact"), ("Learned operating rules", "rule"), ("Autobiographical continuity", "autobiographical")):
            rows = [row["text"] for row in durable if row.get("kind") == kind]
            if rows:
                lines.append(heading + ":")
                lines.extend(f"- {item}" for item in rows)
        return "\n".join(lines)[:16000]

    def export_payload(self) -> dict[str, Any]:
        """Export a public-verifiable, read-only Brain package for distribution."""
        self._require_unlocked()
        auth = self._auth()
        public_auth = {
            "version": int(auth.get("version") or SCHEMA_VERSION),
            "creator_name": str(auth.get("creator_name") or ""),
            "key_type": str(auth.get("key_type") or "Ed25519"),
            "public_key_pem": str(auth.get("public_key_pem") or ""),
            "public_key_sha256": str(auth.get("public_key_sha256") or ""),
            "created_at": float(auth.get("created_at") or 0),
            "distribution_read_only": True,
        }
        if not public_auth["public_key_pem"] or not public_auth["public_key_sha256"]:
            raise RuntimeError("Nexus Brain must be migrated to Ed25519 before distribution export")
        with self._lock:
            data = {
                "schema_version": SCHEMA_VERSION, "brain_id": self._data.get("brain_id"),
                "created_at": self._data.get("created_at"), "updated_at": self._data.get("updated_at"),
                "records": copy.deepcopy(self._data.get("records", [])),
                "subroutines": copy.deepcopy(self._data.get("subroutines", {})),
                "emotion_profile": copy.deepcopy(self._data.get("emotion_profile", {})),
                "self_model": copy.deepcopy(self._data.get("self_model", {})),
                "signature": self._data.get("signature", ""),
            }
            return {
                "format": "chat-nexus-brain-locked",
                "schema_version": SCHEMA_VERSION,
                "exported_at": time.time(),
                "brain": data,
                "creator_lock": public_auth,
            }

    @classmethod
    def verify_locked_export(cls, payload: dict[str, Any]) -> dict[str, Any]:
        if str(payload.get("format") or "") != "chat-nexus-brain-locked":
            raise ValueError("Unsupported Nexus Brain export format")
        brain = payload.get("brain")
        auth = payload.get("creator_lock")
        if not isinstance(brain, dict) or not isinstance(auth, dict):
            raise ValueError("Locked Brain export is incomplete")
        if int(auth.get("version") or 1) < 2 or str(auth.get("key_type") or "") != "Ed25519":
            raise ValueError("Distributed Nexus Brain export must use Ed25519")
        pem = str(auth.get("public_key_pem") or "").encode("utf-8")
        if not pem:
            raise ValueError("Nexus Brain export is missing its public verification key")
        public_key = serialization.load_pem_public_key(pem)
        if not isinstance(public_key, Ed25519PublicKey):
            raise ValueError("Nexus Brain export public key is not Ed25519")
        raw_public = public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        fingerprint = hashlib.sha256(raw_public).hexdigest()
        expected_fingerprint = str(auth.get("public_key_sha256") or "")
        if not expected_fingerprint or fingerprint != expected_fingerprint:
            raise PermissionError("Nexus Brain creator public-key fingerprint mismatch")
        unsigned = {
            "schema_version": int(brain.get("schema_version", SCHEMA_VERSION)),
            "brain_id": str(brain.get("brain_id") or ""),
            "created_at": float(brain.get("created_at") or 0),
            "updated_at": float(brain.get("updated_at") or 0),
            "records": brain.get("records", []),
            "subroutines": brain.get("subroutines", {}),
            "emotion_profile": brain.get("emotion_profile", {}),
            "self_model": brain.get("self_model", {}),
        }
        try:
            signature = cls._unb64(str(brain.get("signature") or ""))
            public_key.verify(signature, cls._canonical(unsigned))
        except (InvalidSignature, ValueError, TypeError) as exc:
            raise PermissionError("Nexus Brain export signature verification failed") from exc
        return {
            "fingerprint": fingerprint,
            "creator_name": str(auth.get("creator_name") or ""),
            "updated_at": float(brain.get("updated_at") or 0),
            "brain_id": str(brain.get("brain_id") or ""),
        }

    def install_locked_export(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.initialized:
            raise RuntimeError("Install locked Brain export only into an uninitialized Nexus Brain")
        verified = self.verify_locked_export(payload)
        brain = payload.get("brain")
        auth = payload.get("creator_lock")
        # settings_history is local rollback state and never part of the
        # signed contract — drop any injected copies from incoming payloads.
        brain = {k: v for k, v in brain.items() if k != "settings_history"}
        # Ed25519 public verification lets a shipped Brain activate read-only
        # without exposing the creator's passcode/private signing key.
        atomic_write_text(self.path, json.dumps(brain, indent=2, ensure_ascii=False))
        self._save_auth(auth)
        self._signing_key = None
        self._unlocked = False
        self._verified_for_session = False
        self._tampered = False
        self._data = {
            "schema_version": SCHEMA_VERSION, "brain_id": uuid.uuid4().hex,
            "created_at": time.time(), "updated_at": time.time(), "records": [],
            "subroutines": dict(DEFAULT_SUBROUTINES), "emotion_profile": dict(DEFAULT_EMOTION_PROFILE),
            "self_model": dict(DEFAULT_SELF_MODEL), "signature": "",
        }
        self._load()
        summary = self.summary()
        if not summary.get("verified_for_session"):
            self._audit("install_rejected", reason="post_install_verify_failed")
            raise PermissionError("Installed Nexus Brain failed public signature verification")
        summary["installed_fingerprint"] = verified["fingerprint"]
        self._audit("distribution_installed", fingerprint=verified["fingerprint"])
        return summary

    def install_signed_update(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Apply a newer public Brain only when it is signed by the same creator key."""
        if not self.initialized:
            summary = self.install_locked_export(payload)
            return {"updated": True, "reason": "installed_initial_brain", "brain": summary}
        current = self.summary()
        if not current.get("verified_for_session"):
            self._audit("signed_update_rejected", reason="current_not_verified")
            raise PermissionError("Current Nexus Brain is not verified; refusing signed update")
        if current.get("creator_signing_key_available"):
            self._audit("signed_update_rejected", reason="creator_installation")
            raise PermissionError("Creator installation will not be auto-overwritten by a distribution Brain update")
        if not current.get("distribution_read_only"):
            self._audit("signed_update_rejected", reason="not_read_only_distribution")
            raise PermissionError("Only public read-only Nexus Brain distributions accept automatic signed updates")
        verified = self.verify_locked_export(payload)
        current_fingerprint = str(current.get("creator_key_fingerprint") or "")
        if not current_fingerprint or verified["fingerprint"] != current_fingerprint:
            self._audit("signed_update_rejected", reason="different_creator_key",
                        fingerprint=verified["fingerprint"])
            raise PermissionError("Nexus Brain update was not signed by the existing creator key")
        incoming_updated = float(verified.get("updated_at") or 0)
        current_updated = float(self._data.get("updated_at") or 0)
        if incoming_updated <= current_updated:
            self._audit("signed_update_stale", incoming_updated_at=incoming_updated)
            return {"updated": False, "reason": "current_brain_is_same_or_newer", "brain": current}
        brain_payload = {k: v for k, v in payload.get("brain").items() if k != "settings_history"}
        auth_payload = payload.get("creator_lock")
        atomic_write_text(self.path, json.dumps(brain_payload, indent=2, ensure_ascii=False))
        self._save_auth(auth_payload)
        self._signing_key = None
        self._unlocked = False
        self._verified_for_session = False
        self._tampered = False
        self._load()
        updated = self.summary()
        if not updated.get("verified_for_session"):
            self._audit("signed_update_rejected", reason="post_update_verify_failed")
            raise PermissionError("Updated Nexus Brain failed public signature verification")
        self._audit("signed_update_accepted", fingerprint=verified["fingerprint"],
                    incoming_updated_at=incoming_updated)
        return {"updated": True, "reason": "newer_creator_signed_brain", "brain": updated}

    def summary(self) -> dict[str, Any]:
        auth = self._auth()
        with self._lock:
            records = [row for row in self._data.get("records", []) if isinstance(row, dict)]
            counts = {
                kind: sum(1 for row in records if row.get("kind") == kind and row.get("active", True))
                for kind in ("fact", "rule", "knowledge", "training_signal", "autobiographical")
            }
            return {
                "enabled": self.enabled, "path": str(self.path), "initialized": self.initialized,
                "unlocked": self.unlocked, "verified_for_session": self.verified_for_session,
                "creator_name": str(auth.get("creator_name") or "") if self.initialized else "",
                "signature_scheme": "ed25519" if int(auth.get("version") or 1) >= 2 else "legacy-hmac-scrypt",
                "creator_key_fingerprint": str(auth.get("public_key_sha256") or ""),
                "creator_signing_key_available": bool(str(auth.get("encrypted_private_key_pem") or "")),
                "distribution_read_only": bool(auth.get("distribution_read_only", False)),
                "brain_id": str(self._data.get("brain_id") or ""), "schema_version": int(self._data.get("schema_version", SCHEMA_VERSION)),
                "integrity": "tampered" if self._tampered else "verified" if self.verified_for_session else "locked_unverified" if self.initialized else "uninitialized",
                "records": len(records), "counts": counts, "subroutines": self.subroutines(),
                "emotion_profile": self.emotion_profile(), "self_model": self.self_model(),
                "settings_history": [
                    {"updated_at": float(e.get("updated_at") or 0),
                     "signature": str(e.get("signature") or "")[:16]}
                    for e in self.settings_history()
                ],
                "updated_at": float(self._data.get("updated_at") or 0),
            }
