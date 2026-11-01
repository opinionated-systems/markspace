# -*- coding: utf-8 -*-
"""
LLM client: reasoning_effort in the request and cached prompt tokens from the
response, the two inputs a trial's cost depends on; and the RequestPacer hook:
every attempt, retries included, waits for the pacer, every response and
timeout is reported to it, and a 429's Retry-After is honoured.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx
import pytest

import markspace.llm as llm_module
from markspace.llm import (
    LLMClient,
    LLMConfig,
    _record_cached_prompt_tokens,
    _retry_wait,
)


def _payload(config: LLMConfig) -> dict:
    payload, _headers = LLMClient(config)._build_openai_request(
        [{"role": "user", "content": "hi"}], None, "auto", None
    )
    return payload


def test_reasoning_effort_sent_only_when_set() -> None:
    base = LLMConfig(base_url="https://example.invalid/v1", api_key="k", model="m")
    assert "reasoning_effort" not in _payload(base)
    capped = LLMConfig(
        base_url="https://example.invalid/v1",
        api_key="k",
        model="m",
        reasoning_effort=2048,
    )
    assert _payload(capped)["reasoning_effort"] == 2048


def test_cached_tokens_header_wins_over_body() -> None:
    data = {
        "usage": {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 10}}
    }
    _record_cached_prompt_tokens(data, {"fireworks-cached-prompt-tokens": "64"})
    assert data["usage"]["cached_prompt_tokens"] == 64


def test_cached_tokens_from_body_without_header() -> None:
    data = {
        "usage": {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 10}}
    }
    _record_cached_prompt_tokens(data, {})
    assert data["usage"]["cached_prompt_tokens"] == 10


def test_cached_tokens_unknown_writes_no_key() -> None:
    data = {"usage": {"prompt_tokens": 100}}
    _record_cached_prompt_tokens(data, {})
    assert "cached_prompt_tokens" not in data["usage"]


def test_malformed_cached_header_keeps_the_body_count() -> None:
    data = {
        "usage": {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 10}}
    }
    _record_cached_prompt_tokens(data, {"fireworks-cached-prompt-tokens": "n/a"})
    assert data["usage"]["cached_prompt_tokens"] == 10


class RecordingPacer:
    """A RequestPacer that records what the client reports to it."""

    def __init__(self) -> None:
        self.acquired = 0
        self.failures = 0
        self.responses: list[tuple[int, float | None]] = []
        self.usages: list[Any] = []

    def acquire(self) -> int:
        self.acquired += 1
        return 0

    def on_response(
        self,
        status: int,
        headers: Mapping[str, str],
        usage: Mapping[str, Any] | None,
        generation: int,
        retry_after: float | None,
    ) -> None:
        self.responses.append((status, retry_after))
        self.usages.append(usage)

    def on_failure(self) -> None:
        self.failures += 1


OK_REPLY = {
    "choices": [{"message": {"role": "assistant", "content": "ok"}}],
    "usage": {"prompt_tokens": 5, "completion_tokens": 1},
}


def _client(
    replies: list[httpx.Response | None], pacer: RecordingPacer | None
) -> LLMClient:
    """A client whose transport answers with *replies* in order; None is a read timeout."""

    def answer(request: httpx.Request) -> httpx.Response:
        reply = replies.pop(0)
        if reply is None:
            raise httpx.ReadTimeout("timed out", request=request)
        return reply

    client = LLMClient(
        LLMConfig(base_url="https://example.invalid/v1", api_key="k", model="m"),
        consecutive_failure_threshold=1,
        pacer=pacer,
    )
    client._http = httpx.Client(transport=httpx.MockTransport(answer))
    return client


def _chat(client: LLMClient) -> dict:
    return client.chat([{"role": "user", "content": "hi"}])


def test_client_paces_every_attempt_and_honours_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waits: list[float] = []
    monkeypatch.setattr(llm_module.time, "sleep", waits.append)
    pacer = RecordingPacer()
    client = _client(
        [
            httpx.Response(429, headers={"retry-after": "7"}),
            httpx.Response(200, json=OK_REPLY),
        ],
        pacer,
    )
    resp = _chat(client)
    assert resp["choices"][0]["message"]["content"] == "ok"
    assert pacer.acquired == 2
    assert pacer.responses == [(429, 7.0), (200, None)]
    assert waits and waits[0] >= 7.0
    # With a pacer, the 429 did not count toward the circuit breaker.
    assert client._circuit_open_until == 0.0


def test_client_caps_the_retry_after_it_passes_to_the_pacer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(llm_module.time, "sleep", lambda seconds: None)
    pacer = RecordingPacer()
    client = _client([httpx.Response(429, headers={"retry-after": "3600"})], pacer)
    with pytest.raises(httpx.HTTPStatusError):
        _chat(client)
    assert pacer.responses == [(429, 120.0)]


def test_client_without_pacer_waits_at_least_backoff_and_counts_the_429(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waits: list[float] = []
    monkeypatch.setattr(llm_module.time, "sleep", waits.append)
    client = _client(
        [
            httpx.Response(429, headers={"retry-after": "0"}),
            httpx.Response(200, json=OK_REPLY),
        ],
        None,
    )
    _chat(client)
    assert waits and waits[0] >= 0.5  # backoff floor for attempt 0
    # Without a pacer, a 429 still counts toward the circuit breaker.
    assert client._circuit_open_until > 0.0


def test_client_fails_when_retry_after_exceeds_its_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waits: list[float] = []
    monkeypatch.setattr(llm_module.time, "sleep", waits.append)
    client = _client([httpx.Response(429, headers={"retry-after": "900"})], None)
    with pytest.raises(httpx.HTTPStatusError):
        _chat(client)
    assert waits == []


def test_client_passes_200_usage_to_the_pacer() -> None:
    pacer = RecordingPacer()
    client = _client([httpx.Response(200, json=OK_REPLY)], pacer)
    _chat(client)
    assert pacer.usages == [OK_REPLY["usage"]]
    assert pacer.responses == [(200, None)]


def test_chat_returns_cached_tokens_from_the_header() -> None:
    client = _client(
        [
            httpx.Response(
                200, headers={"fireworks-cached-prompt-tokens": "4"}, json=OK_REPLY
            )
        ],
        None,
    )
    resp = _chat(client)
    assert resp["usage"]["cached_prompt_tokens"] == 4


def test_client_reports_a_5xx_with_its_headers_to_the_pacer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(llm_module.time, "sleep", lambda seconds: None)
    pacer = RecordingPacer()
    client = _client(
        [
            httpx.Response(503, headers={"x-ratelimit-over-limit": "yes"}),
            httpx.Response(200, json=OK_REPLY),
        ],
        pacer,
    )
    _chat(client)
    assert pacer.acquired == 2
    assert pacer.responses == [(503, None), (200, None)]


def test_client_reports_a_timeout_to_the_pacer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(llm_module.time, "sleep", lambda seconds: None)
    pacer = RecordingPacer()
    client = _client([None, httpx.Response(200, json=OK_REPLY)], pacer)
    _chat(client)
    assert pacer.acquired == 2  # the retry after the timeout waited too
    assert pacer.failures == 1
    assert pacer.responses == [(200, None)]


def test_429_without_retry_after_reaches_the_pacer_as_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(llm_module.time, "sleep", lambda seconds: None)
    pacer = RecordingPacer()
    client = _client([httpx.Response(429), httpx.Response(200, json=OK_REPLY)], pacer)
    _chat(client)
    assert pacer.responses == [(429, None), (200, None)]


def test_negative_retry_after_reaches_the_pacer_as_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(llm_module.time, "sleep", lambda seconds: None)
    pacer = RecordingPacer()
    client = _client(
        [
            httpx.Response(429, headers={"retry-after": "-5"}),
            httpx.Response(200, json=OK_REPLY),
        ],
        pacer,
    )
    _chat(client)
    assert pacer.responses == [(429, 0.0), (200, None)]


def test_retry_after_as_a_date_is_not_passed_to_the_pacer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waits: list[float] = []
    monkeypatch.setattr(llm_module.time, "sleep", waits.append)
    pacer = RecordingPacer()
    client = _client(
        [
            httpx.Response(
                429, headers={"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}
            ),
            httpx.Response(200, json=OK_REPLY),
        ],
        pacer,
    )
    _chat(client)
    assert pacer.responses == [(429, None), (200, None)]
    assert len(waits) == 1 and 0.5 <= waits[0] <= 1.5  # backoff for attempt 0


def test_retry_wait_is_capped_backoff_and_never_below_retry_after() -> None:
    for _ in range(20):
        assert 15.0 <= _retry_wait(None, 10) <= 45.0  # 30 s cap, jitter 0.5-1.5
        assert _retry_wait(60.0, 0) == 60.0
