from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from .provider import ProviderResponse
from ..config import ModelProfile


class OpenAICompatibleProvider:
    def __init__(self, profile: ModelProfile, timeout: int = 300, endpoint: str | None = None) -> None:
        self.profile = profile
        self.timeout = timeout
        self.endpoint = (endpoint or profile.endpoint).rstrip("/")

    def complete(self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None, max_tokens: int | None = None) -> ProviderResponse:
        if not self.endpoint:
            raise RuntimeError(f"No endpoint is available for model profile '{self.profile.id}'.")
        url = self.endpoint + "/chat/completions"
        payload: dict[str, Any] = {
            "model": self.profile.model,
            "messages": messages,
            "temperature": float(self.profile.temperature),
            "max_tokens": max(128, int(max_tokens if max_tokens is not None else self.profile.max_output_tokens)),
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
        started_at = time.monotonic()
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
        raw.setdefault("elapsed_seconds", round(time.monotonic() - started_at, 3))
        return ProviderResponse(message=choices[0].get("message", {}), raw=raw)

    def complete_stream(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        on_delta: Callable[[str], None] | None = None,
        max_tokens: int | None = None,
    ) -> ProviderResponse:
        """Stream a chat completion and reconstruct content plus tool calls."""
        if not self.endpoint:
            raise RuntimeError(f"No endpoint is available for model profile '{self.profile.id}'.")
        url = self.endpoint + "/chat/completions"
        payload: dict[str, Any] = {
            "model": self.profile.model,
            "messages": messages,
            "temperature": float(self.profile.temperature),
            "max_tokens": max(128, int(max_tokens if max_tokens is not None else self.profile.max_output_tokens)),
            "stream": True,
            "stream_options": {"include_usage": True},
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
                "Accept": "text/event-stream",
            },
        )

        role = "assistant"
        content_parts: list[str] = []
        tool_calls: dict[int, dict[str, Any]] = {}
        finish_reason = ""
        chunk_count = 0
        usage: dict[str, Any] = {}
        timings: dict[str, Any] = {}
        started_at = time.monotonic()
        first_token_at = 0.0
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                content_type = str(resp.headers.get("Content-Type") or "").lower()
                if "text/event-stream" not in content_type:
                    raw = json.loads(resp.read().decode("utf-8"))
                    choices = raw.get("choices") or []
                    if not choices:
                        raise RuntimeError(f"Model endpoint returned no choices: {raw}")
                    message = choices[0].get("message", {})
                    text = str(message.get("content") or "")
                    if text and on_delta is not None:
                        on_delta(text)
                    return ProviderResponse(message=message, raw=raw)
                for raw_line in resp:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line or line.startswith(":") or not line.startswith("data:"):
                        continue
                    payload_text = line[5:].strip()
                    if payload_text == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload_text)
                    except json.JSONDecodeError:
                        continue
                    chunk_count += 1
                    if isinstance(chunk.get("usage"), dict):
                        usage = dict(chunk["usage"])
                    if isinstance(chunk.get("timings"), dict):
                        timings = dict(chunk["timings"])
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    choice = choices[0]
                    finish_reason = str(choice.get("finish_reason") or finish_reason or "")
                    delta = choice.get("delta") or {}
                    if delta.get("role"):
                        role = str(delta["role"])
                    text = delta.get("content")
                    if text:
                        if not first_token_at:
                            first_token_at = time.monotonic()
                        piece = str(text)
                        content_parts.append(piece)
                        if on_delta is not None:
                            on_delta(piece)
                    for item in delta.get("tool_calls") or []:
                        index = int(item.get("index", 0))
                        current = tool_calls.setdefault(index, {
                            "id": "",
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                        })
                        if item.get("id"):
                            current["id"] = str(item["id"])
                        if item.get("type"):
                            current["type"] = str(item["type"])
                        fn = item.get("function") or {}
                        if fn.get("name"):
                            current["function"]["name"] += str(fn["name"])
                        if fn.get("arguments"):
                            current["function"]["arguments"] += str(fn["arguments"])
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"Could not reach model endpoint {url}. Start your local inference server or update config.json. Details: {exc}"
            ) from exc

        if chunk_count == 0:
            return self.complete(messages=messages, tools=tools)
        message: dict[str, Any] = {"role": role, "content": "".join(content_parts)}
        if tool_calls:
            message["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
        raw: dict[str, Any] = {
            "stream": True,
            "finish_reason": finish_reason,
            "chunks": chunk_count,
            "elapsed_seconds": round(time.monotonic() - started_at, 3),
            "time_to_first_token_ms": round((first_token_at - started_at) * 1000, 1) if first_token_at else None,
        }
        if usage:
            raw["usage"] = usage
        if timings:
            raw["timings"] = timings
        return ProviderResponse(message=message, raw=raw)
