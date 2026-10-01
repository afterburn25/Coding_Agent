from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

from ..secrets import SecretVault
from .base import ToolRegistry, ToolSpec

ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}
MAX_BODY = 100_000


def register_api_tools(registry: ToolRegistry, *, vault: SecretVault | None = None) -> None:

    def api_request(args: dict[str, Any]) -> str:
        method = str(args.get("method", "GET")).upper()
        if method not in ALLOWED_METHODS:
            return json.dumps({"error": f"method must be one of {sorted(ALLOWED_METHODS)}"})
        url = str(args["url"])
        if not url.startswith(("http://", "https://")):
            return json.dumps({"error": "url must start with http:// or https://"})
        headers = {str(k): str(v) for k, v in (args.get("headers") or {}).items()}

        # Credential by reference — the tool resolves the named vault entry and
        # the agent never sees the secret value.
        auth = args.get("auth") or {}
        secret_name = str(auth.get("secret") or "")
        if secret_name:
            if vault is None:
                return json.dumps({"error": "no credential vault configured"})
            value = vault.get(secret_name)
            if value is None:
                return json.dumps({"error": f"unknown secret '{secret_name}'"})
            header = str(auth.get("header") or "Authorization")
            scheme = str(auth.get("scheme") or "Bearer")
            headers[header] = f"{scheme} {value}" if scheme else value

        body_bytes: bytes | None = None
        if "json" in args:
            body_bytes = json.dumps(args["json"]).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
        elif "body" in args:
            body_bytes = str(args["body"]).encode("utf-8")
        timeout = max(1, min(int(args.get("timeout_seconds", 30)), 300))

        req = urllib.request.Request(url, data=body_bytes, headers=headers, method=method)
        started = time.time()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read(MAX_BODY + 1)
                payload = {
                    "status": resp.status,
                    "content_type": resp.headers.get("Content-Type", ""),
                    "body": raw.decode("utf-8", errors="replace")[:MAX_BODY],
                    "truncated": len(raw) > MAX_BODY,
                }
        except urllib.error.HTTPError as exc:
            payload = {
                "status": exc.code,
                "content_type": "",
                "body": exc.read(MAX_BODY).decode("utf-8", errors="replace"),
                "truncated": False,
            }
        except urllib.error.URLError as exc:
            return json.dumps({"error": f"request failed: {exc.reason}"})
        payload["elapsed_seconds"] = round(time.time() - started, 3)
        return json.dumps(payload, ensure_ascii=False)

    def secrets_list(args: dict[str, Any]) -> str:
        if vault is None:
            return json.dumps({"error": "no credential vault configured"})
        return json.dumps({"secrets": vault.list()}, ensure_ascii=False)

    registry.register(ToolSpec(
        "api_request",
        "General HTTP/API client. Supports GET/POST/PUT/PATCH/DELETE/HEAD, headers, JSON or raw body. Attach credentials by vault reference via auth.secret — the secret value is injected at call time and never shown to the model.",
        {
            "type": "object",
            "properties": {
                "method": {"type": "string", "enum": sorted(ALLOWED_METHODS), "default": "GET"},
                "url": {"type": "string"},
                "headers": {"type": "object"},
                "json": {"type": "object"},
                "body": {"type": "string"},
                "timeout_seconds": {"type": "integer", "default": 30},
                "auth": {
                    "type": "object",
                    "properties": {
                        "secret": {"type": "string", "description": "vault secret name, e.g. github.personal"},
                        "header": {"type": "string", "default": "Authorization"},
                        "scheme": {"type": "string", "default": "Bearer"},
                    },
                },
            },
            "required": ["url"],
        },
        "external_api.call",
        api_request,
        category="external_apis",
        capabilities=["api_request", "http_client", "external_api"],
        requires_network=True,
    ))
    if vault is not None:
        registry.register(ToolSpec(
            "secrets_list",
            "List credential vault entry names and metadata. Never returns secret values.",
            {"type": "object", "properties": {}},
            "credentials.use",
            secrets_list,
            category="utilities",
            capabilities=["secrets_list"],
        ))
