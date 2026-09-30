from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .provider import ProviderResponse
from ..config import ModelProfile


class OpenAICompatibleProvider:
    def __init__(self, profile: ModelProfile, timeout: int = 300, endpoint: str | None = None) -> None:
        self.profile = profile
        self.timeout = timeout
        self.endpoint = (endpoint or profile.endpoint).rstrip("/")

    def complete(self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> ProviderResponse:
        if not self.endpoint:
            raise RuntimeError(f"No endpoint is available for model profile '{self.profile.id}'.")
        url = self.endpoint + "/chat/completions"
        payload: dict[str, Any] = {
            "model": self.profile.model,
            "messages": messages,
            "temperature": 0.2,
            "stream": False,
        }
        if tools and self.profile.tool_calling:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.profile.api_key or 'local'}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"Could not reach model endpoint {url}. Start your local inference server or update config.json. Details: {exc}"
            ) from exc
        choices = raw.get("choices") or []
        if not choices:
            raise RuntimeError(f"Model endpoint returned no choices: {raw}")
        return ProviderResponse(message=choices[0].get("message", {}), raw=raw)
