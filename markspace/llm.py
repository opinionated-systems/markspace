# -*- coding: utf-8 -*-
"""
Provider-agnostic LLM client with tool-calling support.

Works with any OpenAI-compatible API (Fireworks, OpenAI, Together, Ollama, etc.)
and natively with the Anthropic Messages API. Handles retry with backoff,
tool-call normalization from content field, and configurable timeouts.

Usage:
    from markspace.llm import LLMConfig, LLMClient

    config = LLMConfig.from_env(model="kimi-k2p5")
    with LLMClient(config) as client:
        response = client.chat(messages, tools=TOOL_DEFINITIONS)
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
import uuid
import warnings
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _load_dotenv_once() -> None:
    """Load .env file at most once per process."""
    from dotenv import load_dotenv

    load_dotenv()


if TYPE_CHECKING:
    import httpx


@dataclass(frozen=True)
class LLMConfig:
    """Configuration for an LLM endpoint (OpenAI-compatible or Anthropic)."""

    base_url: str
    api_key: str
    model: str
    temperature: float = 0.0
    max_tokens: int = 10_000

    @property
    def is_anthropic(self) -> bool:
        # Match api.anthropic.com but not e.g. not-anthropic.com
        from urllib.parse import urlparse

        host = urlparse(self.base_url).hostname or ""
        return host == "api.anthropic.com" or host.endswith(".anthropic.com")

    @classmethod
    def fireworks(
        cls,
        api_key: str,
        model: str = "accounts/fireworks/models/kimi-k2p5",
    ) -> LLMConfig:
        return cls(
            base_url="https://api.fireworks.ai/inference/v1",
            api_key=api_key,
            model=model,
        )

    @classmethod
    def openai(
        cls,
        api_key: str,
        model: str = "gpt-4o",
    ) -> LLMConfig:
        return cls(
            base_url="https://api.openai.com/v1",
            api_key=api_key,
            model=model,
        )

    @classmethod
    def anthropic(
        cls,
        api_key: str,
        model: str = "claude-sonnet-4-6",
    ) -> LLMConfig:
        return cls(
            base_url="https://api.anthropic.com/v1",
            api_key=api_key,
            model=model,
        )

    @classmethod
    def from_env(cls, model: str | None = None) -> LLMConfig:
        """
        Create config from environment variables.

        If the model is a known entry in EXTERNAL_MODELS, uses that entry's
        base_url and api_key_env directly (regardless of which env vars are set).

        Otherwise checks in order:
        1. FIREWORKS_API_TOKEN -> Fireworks
        2. OPENAI_API_KEY -> OpenAI
        3. GEMINI_API_KEY -> Google Gemini (OpenAI-compatible)
        4. ANTHROPIC_API_KEY -> Anthropic Claude (native Messages API)

        The model parameter accepts either short names (resolved via
        markspace.models.resolve_model_id) or full model paths.
        """
        from markspace.models import EXTERNAL_MODELS, resolve_model_id

        _load_dotenv_once()

        # If model is a known external model, use its registry entry directly.
        # This ensures mercury-2 goes to Inception Labs, claude-* to Anthropic, etc.
        if model and model in EXTERNAL_MODELS:
            entry = EXTERNAL_MODELS[model]
            api_key = os.environ.get(entry.api_key_env)
            if not api_key:
                raise RuntimeError(
                    f"Model '{model}' requires {entry.api_key_env} to be set."
                )
            return cls(
                base_url=entry.base_url,
                api_key=api_key,
                model=entry.model_id,
            )

        fireworks_key = os.environ.get("FIREWORKS_API_TOKEN")
        if fireworks_key:
            resolved = (
                resolve_model_id(model)
                if model
                else "accounts/fireworks/models/kimi-k2p5"
            )
            base_url = os.environ.get(
                "FIREWORKS_INFERENCE_ENDPOINT",
                "https://api.fireworks.ai/inference/v1/chat/completions",
            )
            # Normalize: strip /chat/completions if present (we append it in chat())
            if base_url.endswith("/chat/completions"):
                base_url = base_url[: -len("/chat/completions")]
            return cls(base_url=base_url, api_key=fireworks_key, model=resolved)

        openai_key = os.environ.get("OPENAI_API_KEY")
        if openai_key:
            resolved = resolve_model_id(model) if model else "gpt-4o"
            return cls.openai(api_key=openai_key, model=resolved)

        gemini_key = os.environ.get("GEMINI_API_KEY")
        if gemini_key:
            resolved = resolve_model_id(model) if model else "gemini-2.5-flash"
            return cls(
                base_url="https://generativelanguage.googleapis.com/v1beta/openai",
                api_key=gemini_key,
                model=resolved,
            )

        anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
        if anthropic_key:
            resolved = resolve_model_id(model) if model else "claude-sonnet-4-6"
            return cls.anthropic(api_key=anthropic_key, model=resolved)

        raise RuntimeError(
            "No API key found. Set FIREWORKS_API_TOKEN, OPENAI_API_KEY,"
            " GEMINI_API_KEY, or ANTHROPIC_API_KEY."
            " See .env.example for details."
        )


class LLMClient:
    """
    LLM client supporting OpenAI-compatible and Anthropic APIs.

    Handles:
    - Retry with exponential backoff on 429/5xx
    - Circuit breaker: after consecutive_failure_threshold failures, raises
      immediately without attempting the request for circuit_breaker_timeout
      seconds. Prevents accumulating long waits when the API is down.
    - Tool-call normalization (some models emit tool calls as JSON in content)
    - Automatic format translation for Anthropic's Messages API
    - Configurable timeout
    """

    def __init__(
        self,
        config: LLMConfig,
        timeout: float = 60.0,
        max_retries: int = 5,
        consecutive_failure_threshold: int = 3,
        circuit_breaker_timeout: float = 60.0,
    ) -> None:
        self.config = config
        self.timeout = timeout
        self.max_retries = max_retries
        if config.is_anthropic:
            self._endpoint = f"{config.base_url}/messages"
        else:
            self._endpoint = f"{config.base_url}/chat/completions"
        self._http: httpx.Client | None = None
        # Circuit breaker state
        self._consecutive_failures: int = 0
        self._consecutive_failure_threshold: int = consecutive_failure_threshold
        self._circuit_open_until: float = 0.0
        self._circuit_breaker_timeout: float = circuit_breaker_timeout

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: str = "auto",
        temperature: float | None = None,
    ) -> dict:
        """
        Send a chat completion request. Returns an OpenAI-format response dict.

        For Anthropic backends, automatically translates request and response
        formats. Callers always work with OpenAI-format messages and responses.

        Automatically normalizes tool calls that appear in the content field
        (common with Llama-family models on Fireworks).
        """
        import httpx

        # Circuit breaker: fail fast if the circuit is open
        now = time.time()
        if self._consecutive_failures >= self._consecutive_failure_threshold:
            if now < self._circuit_open_until:
                raise RuntimeError(
                    f"Circuit breaker open: {self._consecutive_failures} consecutive "
                    f"failures. Will retry after {self._circuit_open_until - now:.0f}s."
                )
            # Half-open: reset to 0 so a single failure re-opens with fresh timeout
            self._consecutive_failures = 0

        if self.config.is_anthropic:
            payload, headers = self._build_anthropic_request(
                messages, tools, tool_choice, temperature
            )
        else:
            payload, headers = self._build_openai_request(
                messages, tools, tool_choice, temperature
            )

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                http = self._get_http()
                resp = http.post(
                    self._endpoint,
                    headers=headers,
                    json=payload,
                )

                if resp.status_code == 429 or resp.status_code >= 500:
                    self._record_failure()
                    wait = min(2**attempt, 30) * (0.5 + random.random())
                    time.sleep(wait)
                    last_error = httpx.HTTPStatusError(
                        f"HTTP {resp.status_code}",
                        request=resp.request,
                        response=resp,
                    )
                    continue

                if resp.status_code == 400:
                    # 400 is usually a client error that won't resolve on
                    # retry. Log the body for debugging and raise immediately.
                    body = resp.text[:500]
                    logger.warning("HTTP 400 from %s: %s", self._endpoint, body)
                    raise httpx.HTTPStatusError(
                        f"HTTP {resp.status_code}",
                        request=resp.request,
                        response=resp,
                    )

                resp.raise_for_status()
                data = resp.json()

                # Success - reset circuit breaker
                self._consecutive_failures = 0

                if self.config.is_anthropic:
                    data = _convert_anthropic_response(data)
                else:
                    _normalize_tool_calls(data)

                return data

            except httpx.TimeoutException as e:
                self._record_failure()
                last_error = e
                if attempt < self.max_retries:
                    wait = min(2**attempt, 30) * (0.5 + random.random())
                    time.sleep(wait)
                    continue
                raise

        if last_error:
            raise last_error
        raise RuntimeError("Unexpected retry exhaustion")

    def _record_failure(self) -> None:
        """Record a failure for circuit breaker tracking."""
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._consecutive_failure_threshold:
            self._circuit_open_until = time.time() + self._circuit_breaker_timeout

    def _build_openai_request(
        self,
        messages: list[dict],
        tools: list[dict] | None,
        tool_choice: str,
        temperature: float | None,
    ) -> tuple[dict, dict]:
        """Build payload and headers for OpenAI-compatible APIs."""
        payload: dict = {
            "model": self.config.model,
            "messages": messages,
            "temperature": (
                temperature if temperature is not None else self.config.temperature
            ),
            "max_tokens": self.config.max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice

        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        return payload, headers

    def _build_anthropic_request(
        self,
        messages: list[dict],
        tools: list[dict] | None,
        tool_choice: str,
        temperature: float | None,
    ) -> tuple[dict, dict]:
        """Build payload and headers for Anthropic Messages API."""
        system, converted_messages = _convert_messages_to_anthropic(messages)

        payload: dict = {
            "model": self.config.model,
            "messages": converted_messages,
            "max_tokens": self.config.max_tokens,
            "temperature": (
                temperature if temperature is not None else self.config.temperature
            ),
        }
        if system:
            payload["system"] = system
        if tools and tool_choice != "none":
            payload["tools"] = _convert_tools_to_anthropic(tools)
            if tool_choice == "auto":
                payload["tool_choice"] = {"type": "auto"}
            elif tool_choice == "required":
                payload["tool_choice"] = {"type": "any"}
            else:
                payload["tool_choice"] = {"type": "tool", "name": tool_choice}

        headers = {
            "x-api-key": self.config.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        return payload, headers

    def _get_http(self) -> httpx.Client:
        """Return a reusable httpx client (connection pooling)."""
        import httpx as _httpx

        if self._http is None or self._http.is_closed:
            self._http = _httpx.Client(timeout=self.timeout)
        return self._http

    def close(self) -> None:
        """Close the underlying HTTP client."""
        if self._http is not None and not self._http.is_closed:
            self._http.close()
            self._http = None

    def __enter__(self) -> LLMClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            if self._http is not None and not self._http.is_closed:
                warnings.warn(
                    "LLMClient was not closed. Use 'with LLMClient(config) as client:' "
                    "or call client.close() explicitly.",
                    ResourceWarning,
                    stacklevel=1,
                )
        except Exception:
            # Guard against AttributeError during interpreter shutdown
            # when httpx may already be garbage-collected.
            pass


# ---------------------------------------------------------------------------
# Anthropic format conversion
# ---------------------------------------------------------------------------


def _convert_tools_to_anthropic(tools: list[dict]) -> list[dict]:
    """Convert OpenAI tool definitions to Anthropic format."""
    result = []
    for tool in tools:
        fn = tool.get("function", tool)
        result.append(
            {
                "name": fn["name"],
                "description": fn.get("description", ""),
                "input_schema": fn.get(
                    "parameters", {"type": "object", "properties": {}}
                ),
            }
        )
    return result


def _convert_messages_to_anthropic(
    messages: list[dict],
) -> tuple[str, list[dict]]:
    """
    Convert OpenAI-format messages to Anthropic format.

    Extracts system messages to a separate string (Anthropic uses a top-level
    system parameter). Converts tool results and assistant tool_calls to
    Anthropic's content block format.

    Returns (system_text, converted_messages).
    """
    system_parts: list[str] = []
    converted: list[dict] = []

    for msg in messages:
        role = msg.get("role", "")

        if role == "system":
            system_parts.append(str(msg.get("content", "")))

        elif role == "tool":
            # Anthropic: tool results are user messages with tool_result blocks
            converted.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": msg["tool_call_id"],
                            "content": str(msg.get("content", "")),
                        }
                    ],
                }
            )

        elif role == "assistant":
            tool_calls = msg.get("tool_calls", [])
            if tool_calls:
                content: list[dict] = []
                text = msg.get("content")
                if text:
                    content.append({"type": "text", "text": str(text)})
                for tc in tool_calls:
                    fn = tc["function"]
                    try:
                        input_data = json.loads(fn.get("arguments", "{}"))
                    except (json.JSONDecodeError, TypeError):
                        input_data = {}
                    content.append(
                        {
                            "type": "tool_use",
                            "id": tc["id"],
                            "name": fn["name"],
                            "input": input_data,
                        }
                    )
                converted.append({"role": "assistant", "content": content})
            else:
                converted.append(
                    {
                        "role": "assistant",
                        "content": str(msg.get("content", "")),
                    }
                )

        elif role == "user":
            converted.append(
                {
                    "role": "user",
                    "content": str(msg.get("content", "")),
                }
            )

    # Anthropic requires alternating user/assistant messages.
    # Merge consecutive same-role messages.
    merged = _merge_consecutive_roles(converted)

    return "\n\n".join(system_parts), merged


def _merge_consecutive_roles(messages: list[dict]) -> list[dict]:
    """
    Merge consecutive messages with the same role.

    Anthropic requires strictly alternating user/assistant roles.
    Multiple consecutive tool_result messages (from parallel tool calls)
    get merged into a single user message with multiple content blocks.
    """
    if not messages:
        return []

    merged: list[dict] = [dict(messages[0])]
    for msg in messages[1:]:
        if msg["role"] == merged[-1]["role"]:
            prev = merged[-1]
            # Normalize both to list-of-blocks format.
            # Copy prev_content to a new list to avoid mutating the
            # original message's content when it's already a list.
            prev_content = prev["content"]
            if isinstance(prev_content, str):
                prev_content = [{"type": "text", "text": prev_content}]
            else:
                prev_content = list(prev_content)

            new_content = msg["content"]
            if isinstance(new_content, str):
                new_content = [{"type": "text", "text": new_content}]

            prev["content"] = prev_content + new_content
        else:
            merged.append(msg)

    return merged


def _convert_anthropic_response(resp: dict) -> dict:
    """
    Convert Anthropic Messages API response to OpenAI chat completion format.

    Translates content blocks (text, tool_use) into OpenAI's message format
    with tool_calls, and maps usage/stop_reason fields.
    """
    content_blocks = resp.get("content", [])
    text_parts: list[str] = []
    tool_calls: list[dict] = []

    for block in content_blocks:
        if block["type"] == "text":
            text_parts.append(block["text"])
        elif block["type"] == "tool_use":
            tool_calls.append(
                {
                    "id": block["id"],
                    "type": "function",
                    "function": {
                        "name": block["name"],
                        "arguments": json.dumps(block.get("input", {})),
                    },
                }
            )

    message: dict = {
        "role": "assistant",
        "content": "\n".join(text_parts) if text_parts else "",
    }
    if tool_calls:
        message["tool_calls"] = tool_calls

    stop_reason = resp.get("stop_reason", "end_turn")
    finish_reason = "tool_calls" if stop_reason == "tool_use" else "stop"

    usage = resp.get("usage", {})

    return {
        "choices": [
            {
                "message": message,
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
        },
    }


# ---------------------------------------------------------------------------
# OpenAI tool-call normalization
# ---------------------------------------------------------------------------


def _parse_single_tool_call(obj: dict) -> dict | None:
    """Parse a single tool-call dict (must have a 'name' key). Returns None if invalid."""
    if not isinstance(obj, dict) or "name" not in obj:
        return None
    fn_args = obj.get("parameters", obj.get("arguments", {}))
    return {
        "id": f"synthetic_{uuid.uuid4().hex[:8]}",
        "type": "function",
        "function": {
            "name": obj["name"],
            "arguments": (
                json.dumps(fn_args) if isinstance(fn_args, dict) else str(fn_args)
            ),
        },
    }


def _normalize_tool_calls(data: dict) -> None:
    """
    Some models (Llama on Fireworks) emit tool calls as JSON in the content
    field instead of using the structured tool_calls response format.
    Detect and normalize in-place.

    Handles both single tool call objects ({"name": ...}) and arrays
    of tool call objects ([{"name": ...}, ...]).
    """
    choices = data.get("choices")
    if not choices:
        return
    choice = choices[0]
    message = choice.get("message")
    if not message:
        return
    if not message.get("tool_calls") and message.get("content"):
        content = message["content"].strip()
        try:
            obj = json.loads(content)
        except json.JSONDecodeError:
            return

        calls: list[dict] = []
        if isinstance(obj, dict) and "name" in obj:
            parsed = _parse_single_tool_call(obj)
            if parsed:
                calls.append(parsed)
        elif isinstance(obj, list):
            for item in obj:
                parsed = _parse_single_tool_call(item)
                if parsed:
                    calls.append(parsed)

        if calls:
            message["tool_calls"] = calls
            message["content"] = ""
