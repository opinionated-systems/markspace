# -*- coding: utf-8 -*-
"""
Provider-agnostic LLM client with tool-calling support.

Works with any OpenAI-compatible API (Fireworks, OpenAI, Together, Ollama, etc.).
Handles retry with backoff, tool-call normalization from content field,
and configurable timeouts.

Usage:
    from markspace.llm import LLMConfig, LLMClient

    config = LLMConfig.from_env(model="kimi-k2p5")
    client = LLMClient(config)
    response = client.chat(messages, tools=TOOL_DEFINITIONS)
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field


@dataclass(frozen=True)
class LLMConfig:
    """Configuration for an OpenAI-compatible LLM endpoint."""

    base_url: str
    api_key: str
    model: str
    temperature: float = 0.0
    max_tokens: int = 1024

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
    def from_env(cls, model: str | None = None) -> LLMConfig:
        """
        Create config from environment variables.

        Checks in order:
        1. FIREWORKS_API_TOKEN -> Fireworks
        2. OPENAI_API_KEY -> OpenAI

        The model parameter accepts either short names (resolved via
        markspace.models.resolve_model_id) or full model paths.
        """
        from markspace.models import resolve_model_id

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
            resolved = model or "gpt-4o"
            return cls.openai(api_key=openai_key, model=resolved)

        raise RuntimeError(
            "No API key found. Set FIREWORKS_API_TOKEN or OPENAI_API_KEY."
        )


class LLMClient:
    """
    Stateless HTTP client for OpenAI-compatible chat completions.

    Handles:
    - Retry with exponential backoff on 429/5xx
    - Tool-call normalization (some models emit tool calls as JSON in content)
    - Configurable timeout
    """

    def __init__(
        self,
        config: LLMConfig,
        timeout: float = 60.0,
        max_retries: int = 3,
    ) -> None:
        self.config = config
        self.timeout = timeout
        self.max_retries = max_retries
        self._endpoint = f"{config.base_url}/chat/completions"

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: str = "auto",
        temperature: float | None = None,
    ) -> dict:
        """
        Send a chat completion request. Returns the raw response dict.

        Automatically normalizes tool calls that appear in the content field
        (common with Llama-family models on Fireworks).
        """
        import httpx

        payload: dict = {
            "model": self.config.model,
            "messages": messages,
            "temperature": temperature
            if temperature is not None
            else self.config.temperature,
            "max_tokens": self.config.max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice

        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                with httpx.Client(timeout=self.timeout) as http:
                    resp = http.post(
                        self._endpoint,
                        headers=headers,
                        json=payload,
                    )

                if resp.status_code == 429 or resp.status_code >= 500:
                    wait = min(2**attempt, 30)
                    time.sleep(wait)
                    last_error = httpx.HTTPStatusError(
                        f"HTTP {resp.status_code}",
                        request=resp.request,
                        response=resp,
                    )
                    continue

                resp.raise_for_status()
                data = resp.json()
                _normalize_tool_calls(data)
                return data

            except httpx.TimeoutException as e:
                last_error = e
                if attempt < self.max_retries:
                    time.sleep(min(2**attempt, 30))
                    continue
                raise

        if last_error:
            raise last_error
        raise RuntimeError("Unexpected retry exhaustion")


def _normalize_tool_calls(data: dict) -> None:
    """
    Some models (Llama on Fireworks) emit tool calls as JSON in the content
    field instead of using the structured tool_calls response format.
    Detect and normalize in-place.
    """
    choice = data["choices"][0]
    message = choice["message"]
    if not message.get("tool_calls") and message.get("content"):
        content = message["content"].strip()
        try:
            obj = json.loads(content)
            if isinstance(obj, dict) and "name" in obj:
                fn_args = obj.get("parameters", obj.get("arguments", {}))
                message["tool_calls"] = [
                    {
                        "id": f"synthetic_{uuid.uuid4().hex[:8]}",
                        "type": "function",
                        "function": {
                            "name": obj["name"],
                            "arguments": (
                                json.dumps(fn_args)
                                if isinstance(fn_args, dict)
                                else str(fn_args)
                            ),
                        },
                    }
                ]
                message["content"] = ""
        except json.JSONDecodeError:
            pass
