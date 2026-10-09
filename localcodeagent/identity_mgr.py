"""Nexus Identity Manager — one authoritative subsystem for Nexus's
persistent online identity.

Distinct from ``identity.py`` (the canonical persona/self-model — who
Nexus *is*). This subsystem tracks the accounts Nexus *holds*: email,
service accounts, usernames/handles, verification state, recovery
ownership, OAuth grants, and per-account health.

Invariants:

- **No secrets here.** ``credential_ref`` is the SecretVault key *name*
  (e.g. ``"github_token"``), never a value. Credentials are retrieved
  just-in-time by the service that owns them and stay Vault-only.
- **The user owns recovery.** ``recovery_owner`` is always the human
  owner; Nexus records recovery references but never claims ownership
  of recovery channels.
- **Verified, not claimed.** An account is ``active`` only after a
  successful verification/login probe — ``record_account(..., state=
  "active")`` is rejected unless the caller passes ``verified=True``
  with evidence.
- **Human gates stay human.** CAPTCHA, phone verification, Terms/legal
  acceptance, and account-security challenges park the creation
  workflow at ``awaiting_human`` — never bypassed.

State is durable in ``data/identity.json``; live rows are merged with
service probes (GitHubAccountService, connector account records) so the
answer to "what accounts do I have?" reflects reality, not stale rows.
"""
from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .fsutil import atomic_write_text

# Account lifecycle states.
ACCOUNT_STATES = (
    "creating",                # signup flow in progress
    "awaiting_verification",   # registered; email/code verification pending
    "awaiting_human",          # CAPTCHA / phone / ToS / security challenge
    "verifying_login",         # registration done; proving the login works
    "active",                  # verified and usable
    "degraded",                # usable but impaired (rate limit, partial)
    "suspended",               # the service suspended/locked the account
    "disabled",                # deliberately turned off
    "failed",                  # creation/verification failed
)

# Creation workflow steps in order. ``awaiting_human`` is a hold state,
# not a step — the workflow resumes into whichever step was next.
CREATION_STEPS = (
    "plan",            # service + handle chosen, permission checked
    "signup",          # registration submitted
    "verification",    # email/phone confirmation handled
    "verify_login",    # first authenticated use proven
)

# Human-only challenges — the automation must stop, not work around.
HUMAN_CHALLENGES = (
    "captcha", "phone_verification", "terms_acceptance",
    "legal_consent", "security_challenge", "recovery_setup",
    "identity_verification", "payment_verification",
)

_AUDIT_LIMIT = 400


class IdentityManager:
    """Durable account/identity registry + creation workflow.

    All external state is injected (connectors, github_account, vault)
    so the manager stays testable and never stores secret material.
    """

    def __init__(self, data_dir: Path, *, vault: Any = None,
                 connectors: Any = None, github_account: Any = None,
                 permission_check: Callable[[str], str] | None = None,
                 emit: Callable[[str, dict], None] | None = None) -> None:
        self._dir = Path(data_dir)
        self._path = self._dir / "identity.json"
        self._vault = vault
        self._connectors = connectors
        self._github = github_account
        self._perm = permission_check or (lambda _p: "allow")
        self._emit = emit or (lambda _e, _d: None)
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {
            "version": 1,
            "canonical_name": "Nexus",
            "primary_email": "",          # the Nexus-owned address
            "email_account": "",          # account id backing it
            "recovery_owner": "user",     # the human owner — never Nexus
            "accounts": {},               # account_id -> record
            "audit": [],
        }
        self._load()

    # -- persistence --------------------------------------------------------

    def _load(self) -> None:
        try:
            import json
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for k in ("canonical_name", "primary_email",
                          "email_account", "recovery_owner"):
                    if raw.get(k):
                        self._data[k] = raw[k]
                if isinstance(raw.get("accounts"), dict):
                    self._data["accounts"] = raw["accounts"]
                if isinstance(raw.get("audit"), list):
                    self._data["audit"] = raw["audit"][-_AUDIT_LIMIT:]
        except FileNotFoundError:
            pass
        except Exception:
            pass

    def _save(self) -> None:
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            import json
            atomic_write_text(self._path, json.dumps(self._data, indent=2))
        except Exception:
            pass

    def _audit(self, action: str, detail: dict[str, Any]) -> None:
        """Append an audit entry. ``detail`` must never carry secret
        values — callers pass credential_ref names, not material."""
        self._data["audit"].append({
            "ts": time.time(), "action": action,
            "detail": {k: v for k, v in detail.items()
                       if k not in ("secret", "token", "password",
                                    "credential", "value")},
        })
        self._data["audit"] = self._data["audit"][-_AUDIT_LIMIT:]
        self._emit("identity", {"action": action,
                                "service": detail.get("service", "")})

    # -- account records ----------------------------------------------------

    def _find(self, service: str) -> tuple[str, dict[str, Any]]:
        """Locate the account row for a service (by service key)."""
        service = str(service or "").strip().lower()
        for aid, rec in self._data["accounts"].items():
            if rec.get("service") == service:
                return aid, rec
        return "", {}

    def record_account(self, service: str, *, handle: str = "",
                       auth_method: str = "", credential_ref: str = "",
                       state: str = "active", verified: bool = False,
                       scopes: list[str] | None = None,
                       provenance: str = "", detail: str = "",
                       oauth: bool = False) -> dict[str, Any]:
        """Create/update the durable account row for a service.

        ``state="active"`` requires ``verified=True`` — the caller is
        asserting a login/verification probe succeeded. Anything else is
        recorded honestly as unverified.
        """
        service = str(service or "").strip().lower()
        if not service:
            return {"ok": False, "error": "service is required"}
        if state not in ACCOUNT_STATES:
            return {"ok": False, "error": f"unknown account state '{state}'"}
        if state == "active" and not verified:
            state = "awaiting_verification"
            detail = (detail + " " if detail else "") + \
                "recorded unverified — no login proof supplied"
        with self._lock:
            aid, rec = self._find(service)
            if not aid:
                aid = f"acct-{uuid.uuid4().hex[:8]}"
                rec = {"id": aid, "service": service,
                       "created_at": time.time(),
                       "provenance": provenance or "recorded"}
                self._data["accounts"][aid] = rec
            now = time.time()
            if handle:
                rec["handle"] = handle
            if auth_method:
                rec["auth_method"] = auth_method
            if credential_ref:
                rec["credential_ref"] = credential_ref
            if scopes is not None:
                rec["scopes"] = list(scopes)
            if oauth:
                rec["oauth"] = True
            rec["state"] = state
            rec["detail"] = detail
            rec["updated_at"] = now
            if state == "active" and verified:
                rec["verified_at"] = now
                rec["last_success_at"] = now
            if provenance:
                rec["provenance"] = provenance
            self._audit("account_record", {"service": service,
                                           "state": state,
                                           "account": aid})
            self._save()
            return {"ok": True, "account_id": aid, "state": state}

    def store_credential(self, service: str, vault_name: str,
                         value: str) -> dict[str, Any]:
        """Vault a secret for a service and record only the key name."""
        if self._vault is None:
            return {"ok": False, "error": "no secret vault configured"}
        service = str(service or "").strip().lower()
        vault_name = str(vault_name or "").strip()
        if not service or not vault_name or not value:
            return {"ok": False, "error": "service, vault_name and value required"}
        try:
            self._vault.set(vault_name, value,
                            description=f"{service} account credential")
        except Exception as exc:
            return {"ok": False, "error": f"vault store failed: {exc}"}
        with self._lock:
            aid, rec = self._find(service)
            if not aid:
                out = self.record_account(service, state="awaiting_verification",
                                          auth_method="credential")
                if not out.get("ok"):
                    return out
                aid, rec = self._find(service)
            rec["credential_ref"] = vault_name
            rec["updated_at"] = time.time()
            self._audit("credential_stored", {"service": service,
                                              "credential_ref": vault_name})
            self._save()
            return {"ok": True, "account_id": aid,
                    "credential_ref": vault_name}

    def mark_used(self, service: str, *, ok: bool = True,
                  detail: str = "") -> None:
        """A real use succeeded/failed — health + last_success bookkeeping."""
        with self._lock:
            _aid, rec = self._find(service)
            if not rec:
                return
            if ok:
                rec["last_success_at"] = time.time()
                rec["health"] = "healthy"
                if rec.get("state") == "degraded":
                    rec["state"] = "active"
            else:
                rec["health"] = "unhealthy"
                if rec.get("state") == "active":
                    rec["state"] = "degraded"
                    rec["detail"] = detail or "last use failed"
            rec["updated_at"] = time.time()
            self._save()

    def set_state(self, service: str, state: str, *,
                  detail: str = "") -> dict[str, Any]:
        if state not in ACCOUNT_STATES:
            return {"ok": False, "error": f"unknown account state '{state}'"}
        if state == "active":
            return {"ok": False,
                    "error": "use verify_login() — 'active' requires proof"}
        with self._lock:
            aid, rec = self._find(service)
            if not rec:
                return {"ok": False, "error": f"no account for '{service}'"}
            rec["state"] = state
            rec["detail"] = detail
            rec["updated_at"] = time.time()
            self._audit("account_state", {"service": service, "state": state})
            self._save()
            return {"ok": True, "account_id": aid}

    def verify_login(self, service: str,
                     probe: Callable[[], bool] | None = None) -> dict[str, Any]:
        """Prove the account works — flip to ``active`` only on success.

        ``probe`` performs a real authenticated check (supplied by the
        owning service/connector). No probe → stays unverified.
        """
        with self._lock:
            aid, rec = self._find(service)
            if not rec:
                return {"ok": False, "error": f"no account for '{service}'"}
            rec["state"] = "verifying_login"
            rec["updated_at"] = time.time()
        ok = False
        try:
            ok = bool(probe()) if probe is not None else False
        except Exception:
            ok = False
        with self._lock:
            if ok:
                rec["state"] = "active"
                rec["verified_at"] = time.time()
                rec["last_success_at"] = time.time()
                rec["health"] = "healthy"
                rec["detail"] = "login verified"
            else:
                rec["state"] = "awaiting_verification"
                rec["detail"] = ("verification probe failed" if probe
                                 else "no verification probe available")
                rec["health"] = "unhealthy" if probe else ""
            rec["updated_at"] = time.time()
            self._audit("verify_login", {"service": service,
                                         "verified": ok})
            self._save()
            return {"ok": ok, "account_id": aid,
                    "state": rec["state"]}

    def remove_account(self, service: str, *, delete_credential: bool = False) -> dict[str, Any]:
        """Forget an account row. Credential deletion is explicit and
        separate — a dropped row never silently orphans or kills a vault
        entry another service might share."""
        service = str(service or "").strip().lower()
        with self._lock:
            aid, rec = self._find(service)
            if not rec:
                return {"ok": False, "error": f"no account for '{service}'"}
            ref = str(rec.get("credential_ref") or "")
            del self._data["accounts"][aid]
            if (self._data.get("email_account") or "") == aid:
                self._data["email_account"] = ""
                self._data["primary_email"] = ""
            if delete_credential and ref and self._vault is not None:
                try:
                    self._vault.delete(ref)
                except Exception:
                    pass
            self._audit("account_removed", {"service": service,
                                            "credential_ref": ref,
                                            "credential_deleted":
                                            bool(delete_credential)})
            self._save()
            return {"ok": True, "account_id": aid}

    # -- creation workflow ----------------------------------------------------

    def begin_account_creation(self, service: str, *,
                               handle: str = "",
                               provenance: str = "user_requested") -> dict[str, Any]:
        """Start a tracked signup. Permission-gated by
        ``identity.account_create`` — creating a third-party account is
        not implied by social.post or browser.control."""
        verdict = self._perm("identity.account_create")
        if verdict != "allow":
            return {"ok": False, "permission": "identity.account_create",
                    "verdict": verdict, "needs_approval": verdict == "ask",
                    "error": f"permission denied: identity.account_create"}
        service = str(service or "").strip().lower()
        if not service:
            return {"ok": False, "error": "service is required"}
        with self._lock:
            aid, rec = self._find(service)
            if rec and rec.get("state") in ("creating", "awaiting_human",
                                            "awaiting_verification",
                                            "verifying_login", "active"):
                return {"ok": False, "error": "creation already in progress "
                        f"or account exists (state: {rec['state']})",
                        "account_id": aid, "state": rec.get("state")}
            if not aid:
                aid = f"acct-{uuid.uuid4().hex[:8]}"
                rec = {"id": aid, "service": service,
                       "created_at": time.time()}
                self._data["accounts"][aid] = rec
            rec.update({
                "state": "creating",
                "handle": handle,
                "auth_method": "",
                "provenance": provenance,
                "workflow": {"step": "signup", "steps": list(CREATION_STEPS),
                             "human_challenge": "", "started_at": time.time()},
                "updated_at": time.time(),
            })
            self._audit("creation_started", {"service": service,
                                             "account": aid})
            self._save()
            return {"ok": True, "account_id": aid, "state": "creating",
                    "step": "signup"}

    def pause_for_human(self, service: str, challenge: str,
                        *, detail: str = "") -> dict[str, Any]:
        """A human-only control appeared — park the workflow. Nexus
        must request human takeover; it never bypasses CAPTCHA/phone/
        legal/security challenges."""
        challenge = str(challenge or "").strip().lower()
        if challenge not in HUMAN_CHALLENGES:
            challenge = "security_challenge"
        with self._lock:
            aid, rec = self._find(service)
            if not rec:
                return {"ok": False, "error": f"no account for '{service}'"}
            wf = rec.get("workflow") or {}
            rec["state"] = "awaiting_human"
            wf["human_challenge"] = challenge
            rec["workflow"] = wf
            rec["detail"] = detail or f"human required: {challenge.replace('_', ' ')}"
            rec["updated_at"] = time.time()
            self._audit("awaiting_human", {"service": service,
                                           "challenge": challenge})
            self._save()
            self._emit("identity_human_gate",
                       {"service": service, "challenge": challenge})
            return {"ok": True, "account_id": aid, "state": "awaiting_human",
                    "challenge": challenge}

    def resume_creation(self, service: str, *,
                        step: str | None = None) -> dict[str, Any]:
        """Human gate cleared (or verification received) — continue."""
        with self._lock:
            aid, rec = self._find(service)
            if not rec:
                return {"ok": False, "error": f"no account for '{service}'"}
            if rec.get("state") not in ("awaiting_human",
                                        "awaiting_verification", "creating"):
                return {"ok": False,
                        "error": f"nothing to resume (state: {rec.get('state')})"}
            wf = rec.get("workflow") or {}
            wf["human_challenge"] = ""
            nxt = step or "verify_login"
            if nxt not in CREATION_STEPS:
                nxt = "verify_login"
            wf["step"] = nxt
            rec["workflow"] = wf
            rec["state"] = ("awaiting_verification" if nxt == "verification"
                            else "creating" if nxt in ("plan", "signup")
                            else "verifying_login")
            rec["updated_at"] = time.time()
            self._audit("creation_resumed", {"service": service,
                                             "step": nxt})
            self._save()
            return {"ok": True, "account_id": aid,
                    "state": rec["state"], "step": nxt}

    # -- identity-level fields ---------------------------------------------

    def set_primary_email(self, service: str, address: str) -> dict[str, Any]:
        """Bind the Nexus-owned email address to its backing account."""
        service = str(service or "").strip().lower()
        address = str(address or "").strip()
        if not service or "@" not in address:
            return {"ok": False, "error": "service and a valid address required"}
        verdict = self._perm("identity.account_modify")
        if verdict != "allow":
            return {"ok": False, "permission": "identity.account_modify",
                    "verdict": verdict, "needs_approval": verdict == "ask",
                    "error": f"permission denied: identity.account_modify"}
        with self._lock:
            aid, rec = self._find(service)
            if not rec:
                out = self.record_account(service, handle=address,
                                          auth_method="email",
                                          state="awaiting_verification",
                                          provenance="email_binding")
                if not out.get("ok"):
                    return out
                aid, rec = self._find(service)
            rec["handle"] = address
            rec["kind"] = "email"
            self._data["primary_email"] = address
            self._data["email_account"] = aid
            self._audit("primary_email_set", {"service": service,
                                              "address_domain":
                                              address.split("@")[-1]})
            self._save()
            return {"ok": True, "account_id": aid}

    # -- merged live view ----------------------------------------------------

    def _live_rows(self) -> dict[str, dict[str, Any]]:
        """Probe owning services for live account truth — merged over
        durable rows so a live probe always wins."""
        live: dict[str, dict[str, Any]] = {}
        if self._github is not None:
            try:
                st = self._github.status() or {}
                state = st.get("state") or "not_configured"
                live["github"] = {
                    "service": "github",
                    "handle": st.get("login") or "",
                    "state": ("active" if state == "connected"
                              else "degraded" if state in
                              ("invalid_token", "unreachable")
                              else "disabled" if state == "disabled"
                              else "awaiting_verification"),
                    "auth_method": "token",
                    "credential_ref": "github_token" if st.get("source") else "",
                    "scopes": st.get("scopes") or [],
                    "detail": st.get("detail") or "",
                    "live": True,
                }
            except Exception:
                pass
        try:
            conns = getattr(self._connectors, "connectors", None) or {}
            for name, rec in conns.items():
                conn = rec.get("conn")
                acct = {}
                try:
                    if hasattr(conn, "account"):
                        acct = conn.account() or {}
                    elif hasattr(conn, "account_state"):
                        acct = {"state": conn.account_state()}
                except Exception:
                    acct = {}
                state = str(acct.get("state") or
                            ("active" if rec.get("authed") else "none"))
                live[name] = {
                    "service": name,
                    "handle": acct.get("handle") or acct.get("name")
                              or acct.get("agent_name") or "",
                    "state": ("active" if state in ("active", "claimed")
                              else "awaiting_verification" if state ==
                              "awaiting_owner_verification"
                              else "disabled" if state == "disabled"
                              else "awaiting_verification" if state not in
                              ("none", "") else "awaiting_verification"),
                    "auth_method": "api_key" if rec.get("authed") else "",
                    "credential_ref": getattr(conn, "secret_name", "") or "",
                    "verified_at": acct.get("verified_at") or 0,
                    "detail": acct.get("detail") or "",
                    "live": True,
                }
        except Exception:
            pass
        return live

    def status(self) -> dict[str, Any]:
        """Public identity view — durable records merged with live
        service probes. Contains no secret material."""
        with self._lock:
            accounts = {aid: dict(rec)
                        for aid, rec in self._data["accounts"].items()}
        live = self._live_rows()
        services_with_live = set(live)
        merged: list[dict[str, Any]] = []
        for aid, rec in accounts.items():
            svc = rec.get("service") or ""
            row = dict(rec)
            if svc in live:
                lrow = live[svc]
                for k in ("handle", "state", "auth_method", "scopes",
                          "credential_ref", "health"):
                    if lrow.get(k):
                        row[k] = lrow[k]
                row["live_detail"] = lrow.get("detail") or ""
                row["live"] = True
                services_with_live.discard(svc)
            merged.append(row)
        for svc in services_with_live:
            row = dict(live[svc])
            row.setdefault("id", f"live-{svc}")
            merged.append(row)
        merged.sort(key=lambda r: (r.get("service") or ""))
        return {
            "canonical_name": self._data["canonical_name"],
            "primary_email": self._data["primary_email"],
            "recovery_owner": self._data["recovery_owner"],
            "accounts": merged,
            "audit_tail": self._data["audit"][-20:],
            "counts": {
                "total": len(merged),
                "active": sum(1 for r in merged
                              if r.get("state") == "active"),
                "awaiting_human": sum(1 for r in merged
                                      if r.get("state") == "awaiting_human"),
            },
        }

    def audit(self, limit: int = 60) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._data["audit"])[-limit:]
