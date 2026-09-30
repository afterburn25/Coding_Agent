from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
import uuid
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
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
        self.enabled = bool(enabled)
        self.max_records = max(100, int(max_records))
        self._lock = threading.RLock()
        self._session_key: bytes | None = None
        self._unlocked = False
        self._verified_for_session = False
        self._tampered = False
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
        return bool(self._unlocked and self._session_key)

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
    def _derive(passcode: str, salt: bytes) -> bytes:
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

    def _digest(self, key: bytes) -> str:
        return hmac.new(key, self._canonical(self._unsigned_payload()), hashlib.sha256).hexdigest()

    def _save_auth(self, auth: dict[str, Any]) -> None:
        tmp = self.auth_path.with_suffix(self.auth_path.suffix + ".tmp")
        tmp.write_text(json.dumps(auth, indent=2), encoding="utf-8")
        tmp.replace(self.auth_path)

    def _save_signed(self) -> None:
        self._require_unlocked()
        self._data["schema_version"] = SCHEMA_VERSION
        self._data["updated_at"] = time.time()
        self._data["signature"] = self._digest(self._session_key or b"")
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

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
            auth_salt = secrets.token_bytes(16)
            integrity_salt = secrets.token_bytes(16)
            auth = {
                "version": 1,
                "creator_name": creator,
                "auth_salt": self._b64(auth_salt),
                "auth_hash": self._b64(self._derive(secret, auth_salt)),
                "integrity_salt": self._b64(integrity_salt),
                "kdf": {"name": "scrypt", "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P, "dklen": SCRYPT_DKLEN},
                "created_at": time.time(),
            }
            self._save_auth(auth)
            self._session_key = self._derive(secret, integrity_salt)
            self._unlocked = True
            self._verified_for_session = True
            self._tampered = False
            self._save_signed()
            return self.summary()

    def unlock(self, creator_name: str, passcode: str) -> dict[str, Any]:
        if not self.initialized:
            raise RuntimeError("Nexus Brain creator lock has not been initialized")
        auth = self._auth()
        creator = self._clean(creator_name, 120)
        if creator.casefold() != str(auth.get("creator_name") or "").casefold():
            raise PermissionError("Creator authentication failed")
        try:
            auth_salt = self._unb64(str(auth.get("auth_salt") or ""))
            expected = self._unb64(str(auth.get("auth_hash") or ""))
            integrity_salt = self._unb64(str(auth.get("integrity_salt") or ""))
        except Exception as exc:
            raise RuntimeError("Nexus Brain creator metadata is invalid") from exc
        if not hmac.compare_digest(self._derive(str(passcode or ""), auth_salt), expected):
            raise PermissionError("Creator authentication failed")
        key = self._derive(str(passcode or ""), integrity_salt)
        with self._lock:
            signature = str(self._data.get("signature") or "")
            if signature and not hmac.compare_digest(signature, self._digest(key)):
                self._tampered = True
                self._session_key = None
                self._unlocked = False
                self._verified_for_session = False
                raise PermissionError("Nexus Brain integrity verification failed; protected data appears to have been modified")
            self._session_key = key
            self._unlocked = True
            self._verified_for_session = True
            self._tampered = False
            if not signature:
                self._save_signed()
            return self.summary()

    def lock(self) -> dict[str, Any]:
        with self._lock:
            self._session_key = None
            self._unlocked = False
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

    def prompt_context(self, *, project_id: str = "", conversation_id: str = "") -> str:
        if not self.enabled or not self.initialized or not self.verified_for_session or not self.subroutine("long_term_memory", True):
            return ""
        with self._lock:
            durable = [
                copy.deepcopy(row) for row in self._data.get("records", [])
                if isinstance(row, dict) and row.get("kind") in {"fact", "rule", "autobiographical"}
                and self._applies(row, project_id, conversation_id)
            ][-80:]
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
        self._require_unlocked()
        auth = self._auth()
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
                "creator_lock": copy.deepcopy(auth),
            }

    def install_locked_export(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.initialized:
            raise RuntimeError("Install locked Brain export only into an uninitialized Nexus Brain")
        if str(payload.get("format") or "") != "chat-nexus-brain-locked":
            raise ValueError("Unsupported Nexus Brain export format")
        brain = payload.get("brain")
        auth = payload.get("creator_lock")
        if not isinstance(brain, dict) or not isinstance(auth, dict):
            raise ValueError("Locked Brain export is incomplete")
        # It remains locked/unverified until the original creator supplies the passcode.
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(brain, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)
        self._save_auth(auth)
        self._session_key = None
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
        return self.summary()

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
                "brain_id": str(self._data.get("brain_id") or ""), "schema_version": int(self._data.get("schema_version", SCHEMA_VERSION)),
                "integrity": "tampered" if self._tampered else "verified" if self.verified_for_session else "locked_unverified" if self.initialized else "uninitialized",
                "records": len(records), "counts": counts, "subroutines": self.subroutines(),
                "emotion_profile": self.emotion_profile(), "self_model": self.self_model(),
                "updated_at": float(self._data.get("updated_at") or 0),
            }
