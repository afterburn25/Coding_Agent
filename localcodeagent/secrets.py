"""Local encrypted credential vault.

Secrets live in an encrypted store under data/. Tools request credentials by
reference — e.g. ``api_request`` accepts ``{"auth": {"secret": "github.personal"}}``
and receives only that credential's value at call time. Secret values are never
returned by API endpoints or written to logs.
"""
from __future__ import annotations

import base64
import json
import os
import threading
import time
from pathlib import Path

from .fsutil import atomic_write_text
from typing import Any

from cryptography.fernet import Fernet


class SecretVault:
    """Small encrypted key/value store for API keys and tokens.

    A random Fernet key (AES-128-CBC + HMAC) is generated once into
    ``<name>.key`` (best-effort restrictive permissions) and used to encrypt
    the JSON payload at rest. This is application-level protection — it does
    not defend against an administrator who can read both the key file and
    the vault.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.key_path = self.path.with_name(self.path.stem + ".key")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._store: dict[str, dict[str, Any]] = {}
        self._load()

    # -- key + storage ---------------------------------------------------------

    @staticmethod
    def _dpapi_protect(raw: bytes) -> bytes | None:
        """Windows DPAPI: encrypt so only this Windows user can read it.
        Returns None when unavailable (non-Windows or crypt32 missing)."""
        if os.name != "nt":
            return None
        try:
            import ctypes
            from ctypes import wintypes

            class DATA_BLOB(ctypes.Structure):
                _fields_ = [("cbData", wintypes.DWORD),
                            ("pbData", ctypes.POINTER(ctypes.c_byte))]

            def _blob(data: bytes) -> DATA_BLOB:
                buf = (ctypes.c_byte * len(data)).from_buffer_copy(data)
                return DATA_BLOB(len(data), ctypes.cast(
                    buf, ctypes.POINTER(ctypes.c_byte)))

            in_blob = _blob(raw)
            out_blob = DATA_BLOB()
            crypt32 = ctypes.windll.crypt32
            kernel32 = ctypes.windll.kernel32
            if not crypt32.CryptProtectData(
                    ctypes.byref(in_blob), "NexusCoreVault", None,
                    None, None, 0, ctypes.byref(out_blob)):
                return None
            try:
                return ctypes.string_at(out_blob.pbData, out_blob.cbData)
            finally:
                kernel32.LocalFree(out_blob.pbData)
        except Exception:
            return None

    @staticmethod
    def _dpapi_unprotect(raw: bytes) -> bytes | None:
        if os.name != "nt":
            return None
        try:
            import ctypes
            from ctypes import wintypes

            class DATA_BLOB(ctypes.Structure):
                _fields_ = [("cbData", wintypes.DWORD),
                            ("pbData", ctypes.POINTER(ctypes.c_byte))]

            buf = (ctypes.c_byte * len(raw)).from_buffer_copy(raw)
            in_blob = DATA_BLOB(len(raw), ctypes.cast(
                buf, ctypes.POINTER(ctypes.c_byte)))
            out_blob = DATA_BLOB()
            crypt32 = ctypes.windll.crypt32
            kernel32 = ctypes.windll.kernel32
            if not crypt32.CryptUnprotectData(
                    ctypes.byref(in_blob), None, None, None, None, 0,
                    ctypes.byref(out_blob)):
                return None
            try:
                return ctypes.string_at(out_blob.pbData, out_blob.cbData)
            finally:
                kernel32.LocalFree(out_blob.pbData)
        except Exception:
            return None

    def _read_key(self) -> bytes | None:
        try:
            text = self.key_path.read_text().strip()
        except OSError:
            return None
        if text.startswith("dpapi:"):
            raw = base64.b64decode(text[6:])
            key = self._dpapi_unprotect(raw)
            return key
        return text.encode("ascii")

    def _write_key(self, key: bytes) -> None:
        protected = self._dpapi_protect(key)
        if protected is not None:
            self.key_path.write_text(
                "dpapi:" + base64.b64encode(protected).decode("ascii"))
        else:
            self.key_path.write_text(key.decode("ascii"))
        try:
            os.chmod(self.key_path, 0o600)
        except OSError:
            pass

    def _fernet(self) -> Fernet:
        key = self._read_key()
        if key is not None:
            return Fernet(key)
        key = Fernet.generate_key()
        self._write_key(key)
        return Fernet(key)

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            blob = json.loads(self.path.read_text(encoding="utf-8"))
            plaintext = self._fernet().decrypt(str(blob.get("data") or "").encode("ascii"))
            payload = json.loads(plaintext.decode("utf-8"))
            if isinstance(payload, dict):
                self._store = {str(k): dict(v) for k, v in payload.get("secrets", {}).items() if isinstance(v, dict)}
        except (OSError, ValueError, KeyError):
            self._store = {}

    def _save(self) -> None:
        payload = json.dumps({"secrets": self._store}, ensure_ascii=False).encode("utf-8")
        encrypted = self._fernet().encrypt(payload).decode("ascii")
        atomic_write_text(self.path, json.dumps({"version": 1, "data": encrypted}, indent=2))

    # -- API -------------------------------------------------------------------

    @staticmethod
    def _valid_name(name: str) -> str:
        name = str(name or "").strip()
        if not name or len(name) > 80 or not all(c.isalnum() or c in "._-" for c in name):
            raise ValueError("secret name must be 1-80 chars of [a-zA-Z0-9._-]")
        return name

    def set(self, name: str, value: str, *, description: str = "") -> dict[str, Any]:
        name = self._valid_name(name)
        value = str(value or "")
        if not value:
            raise ValueError("secret value is required")
        with self._lock:
            existing = self._store.get(name, {})
            self._store[name] = {
                "value": value,
                "description": str(description or "")[:300],
                "created_at": existing.get("created_at") or time.time(),
                "updated_at": time.time(),
            }
            self._save()
            return {"ok": True, "name": name}

    def get(self, name: str) -> str | None:
        with self._lock:
            entry = self._store.get(str(name))
            return str(entry["value"]) if entry else None

    def has(self, name: str) -> bool:
        return self.get(name) is not None

    def redact(self, text: str) -> str:
        """Mask stored secret values appearing in tool output or logs.

        Values under 6 characters are skipped so short/common strings do not
        over-redact unrelated text.
        """
        if not text:
            return text
        with self._lock:
            values = [str(v.get("value", "")) for v in self._store.values()]
        for value in values:
            if len(value) >= 6:
                text = text.replace(value, "••••••")
        return text

    @staticmethod
    def redact_patterns(text: str) -> str:
        """Pattern-based redaction for secrets not stored in the vault —
        API keys, bearer tokens, private-key blocks appearing in logs,
        diagnostics, or crash reports."""
        if not text:
            return text
        import re
        patterns = [
            r"\bsk-[A-Za-z0-9_\-]{16,}\b",
            r"\bghp_[A-Za-z0-9]{20,}\b",
            r"\bgho_[A-Za-z0-9]{20,}\b",
            r"\bgithub_pat_[A-Za-z0-9_]{20,}\b",
            r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b",
            r"\bAKIA[0-9A-Z]{16}\b",
            r"\bBearer\s+[A-Za-z0-9._\-]{20,}",
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
        ]
        for pat in patterns:
            text = re.sub(pat, "••••••", text)
        return text

    def delete(self, name: str) -> bool:
        with self._lock:
            existed = self._store.pop(str(name), None) is not None
            if existed:
                self._save()
            return existed

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return sorted(
                ({"name": k, "description": v.get("description", ""),
                  "created_at": v.get("created_at"), "updated_at": v.get("updated_at")}
                 for k, v in self._store.items()),
                key=lambda r: r["name"],
            )
