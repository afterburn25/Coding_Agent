from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from .provider import ProviderResponse
from ..config import ModelProfile

import re

_CONTEXT_OVERFLOW_RE = re.compile(
    r"exceeds the (available context|maximum context|context size)|"
    r"context (window|length|size).*exceed|prompt.*too (long|large)|"
    r"request.*too many tokens",
    re.IGNORECASE,
)
_OVERFLOW_NUMBERS_RE = re.compile(r"\((\d+)\s*tokens\).*?\((\d+)\s*tokens\)", re.DOTALL)


class ModelHTTPError(RuntimeError):
    """HTTP failure from the model endpoint with the server's own reason.

    Distinct from a connection failure — the endpoint was reached and
    rejected the request, so ``server_message`` carries the model's
    diagnostic (e.g. context overflow details).
    """

    def __init__(self, url: str, status: int, server_message: str, raw_body: str = "") -> None:
        super().__init__(
            f"Model endpoint {url} rejected the request (HTTP {status}): {server_message or raw_body[:300]}"
        )
        self.url = url
        self.status = status
        self.server_message = server_message
        self.raw_body = raw_body


def _http_error_detail(exc: urllib.error.HTTPError) -> tuple[str, str]:
    """Extract (server_message, raw_body) from an HTTPError."""
    try:
        raw_body = exc.read().decode("utf-8", errors="replace")
    except Exception:
        raw_body = ""
    server_message = ""
    try:
        parsed = json.loads(raw_body)
        err = parsed.get("error") if isinstance(parsed, dict) else None
        if isinstance(err, dict):
            server_message = str(err.get("message") or "")
        elif isinstance(err, str):
            server_message = err
        if not server_message and isinstance(parsed, dict):
            server_message = str(parsed.get("detail") or parsed.get("message") or "")
    except ValueError:
        server_message = raw_body[:300]
    return server_message, raw_body


def _shrink_messages_for_context(messages: list[dict[str, Any]], overage_tokens: int) -> list[dict[str, Any]]:
    """Trim a request to fit the server's reported context.

    System messages are always kept. Oldest non-system bodies are stubbed
    first (roles/tool_call pairing preserved); if still over budget the
    oldest whole messages are dropped after the system block.
    """
    # ~3 chars/token: convert the token overage into chars to remove, with
    # a 25% safety margin since our estimate is approximate.
    chars_to_remove = int(overage_tokens * 3 * 1.25)
    shrunk = [dict(m) for m in messages]
    head = 0
    while head < len(shrunk) and shrunk[head].get("role") == "system":
        head += 1
    stub = "[elided to fit context window]"
    removed = 0
    for m in shrunk[head:]:
        if removed >= chars_to_remove:
            break
        content = str(m.get("content") or "")
        if len(content) > 400:
            removed += len(content) - len(stub)
            m["content"] = stub
    if removed < chars_to_remove:
        # Still over: drop the oldest non-system messages entirely.
        keep = shrunk[:head]
        tail = shrunk[head:]
        while tail and removed < chars_to_remove:
            dropped = tail.pop(0)
            removed += len(str(dropped.get("content") or "")) + 40
        shrunk = keep + tail
    return shrunk


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
        repaired = 0
        tools_dropped = False
        while True:
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = json.loads(resp.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                server_message, raw_body = _http_error_detail(exc)
                # Self-repair: the server tells us exactly how oversized the
                # request was — shrink the prompt to fit and retry instead
                # of failing the task.
                if exc.code == 400 and _CONTEXT_OVERFLOW_RE.search(server_message) and repaired < 2:
                    repaired += 1
                    nums = _OVERFLOW_NUMBERS_RE.search(server_message)
                    overage = max(256, int(nums.group(1)) - int(nums.group(2)) + 512) if nums else 4096
                    payload["messages"] = _shrink_messages_for_context(messages, overage)
                    payload["max_tokens"] = min(int(payload["max_tokens"]), 2048)
                    messages = payload["messages"]
                    data = json.dumps(payload).encode("utf-8")
                    req = urllib.request.Request(
                        url, data=data, method="POST",
                        headers={"Content-Type": "application/json",
                                 "Authorization": f"Bearer {self.profile.api_key or 'local'}"})
                    continue
                if (exc.code == 400 and _CONTEXT_OVERFLOW_RE.search(server_message)
                        and not tools_dropped and payload.get("tools")):
                    # Last resort: system prompt + tool schemas alone can
                    # exceed the window, leaving message shrinking unable to
                    # converge. Drop the tools so the turn degrades to a plain
                    # answer instead of a hard failure.
                    tools_dropped = True
                    payload.pop("tools", None)
                    payload.pop("tool_choice", None)
                    payload["messages"] = _shrink_messages_for_context(messages, 2048)
                    payload["max_tokens"] = min(int(payload["max_tokens"]), 2048)
                    messages = payload["messages"]
                    data = json.dumps(payload).encode("utf-8")
                    req = urllib.request.Request(
                        url, data=data, method="POST",
                        headers={"Content-Type": "application/json",
                                 "Authorization": f"Bearer {self.profile.api_key or 'local'}"})
                    continue
                raise ModelHTTPError(url, exc.code, server_message, raw_body) from exc
            except urllib.error.URLError as exc:
                raise RuntimeError(
                    f"Could not reach model endpoint {url}. Start your local inference server or update config.json. Details: {exc}"
                ) from exc
        if repaired:
            raw.setdefault("auto_repaired", {"context_shrink": repaired,
                                             "tools_dropped": tools_dropped})
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
        repaired = 0
        tools_dropped = False
        while True:
            try:
                resp = urllib.request.urlopen(req, timeout=self.timeout)
                break
            except urllib.error.HTTPError as exc:
                server_message, raw_body = _http_error_detail(exc)
                if exc.code == 400 and _CONTEXT_OVERFLOW_RE.search(server_message) and repaired < 2:
                    repaired += 1
                    nums = _OVERFLOW_NUMBERS_RE.search(server_message)
                    overage = max(256, int(nums.group(1)) - int(nums.group(2)) + 512) if nums else 4096
                    payload["messages"] = _shrink_messages_for_context(messages, overage)
                    payload["max_tokens"] = min(int(payload["max_tokens"]), 2048)
                    messages = payload["messages"]
                    data = json.dumps(payload).encode("utf-8")
                    req = urllib.request.Request(
                        url, data=data, method="POST",
                        headers={"Content-Type": "application/json",
                                 "Authorization": f"Bearer {self.profile.api_key or 'local'}",
                                 "Accept": "text/event-stream"})
                    continue
                if (exc.code == 400 and _CONTEXT_OVERFLOW_RE.search(server_message)
                        and not tools_dropped and payload.get("tools")):
                    # Last resort: system prompt + tool schemas alone can
                    # exceed the window; drop tool schemas and retry so the
                    # turn still answers instead of hard-failing.
                    tools_dropped = True
                    payload.pop("tools", None)
                    payload.pop("tool_choice", None)
                    payload["messages"] = _shrink_messages_for_context(messages, 2048)
                    payload["max_tokens"] = min(int(payload["max_tokens"]), 2048)
                    messages = payload["messages"]
                    data = json.dumps(payload).encode("utf-8")
                    req = urllib.request.Request(
                        url, data=data, method="POST",
                        headers={"Content-Type": "application/json",
                                 "Authorization": f"Bearer {self.profile.api_key or 'local'}",
                                 "Accept": "text/event-stream"})
                    continue
                raise ModelHTTPError(url, exc.code, server_message, raw_body) from exc
            except urllib.error.URLError as exc:
                raise RuntimeError(
                    f"Could not reach model endpoint {url}. Start your local inference server or update config.json. Details: {exc}"
                ) from exc
        try:
            with resp:
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
            # Mid-stream connection drops still surface as reachability errors.
            raise RuntimeError(
                f"Lost connection to model endpoint {url} mid-stream. Details: {exc}"
            ) from exc

        if chunk_count == 0:
            return self.complete(messages=messages, tools=tools)
        message: dict[str, Any] = {"role": role, "content": "".join(content_parts)}
        if tool_calls:
            message["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
        raw: dict[str, Any] = {
            "stream": True,
            "finish_reason": finish_reason,
            **({"auto_repaired": {"context_shrink": repaired,
                                  "tools_dropped": tools_dropped}} if repaired else {}),
            "chunks": chunk_count,
            "elapsed_seconds": round(time.monotonic() - started_at, 3),
            "time_to_first_token_ms": round((first_token_at - started_at) * 1000, 1) if first_token_at else None,
        }
        if usage:
            raw["usage"] = usage
        if timings:
            raw["timings"] = timings
        return ProviderResponse(message=message, raw=raw)
