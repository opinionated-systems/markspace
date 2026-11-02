# -*- coding: utf-8 -*-
"""
Request pacer for the stress test, for providers with adaptive rate limits.

Fireworks serverless limits are tokens per minute, per account and per model,
and other users of the account share them. A limit grows 25% when usage stays
above 50% of it, at most once every 15 minutes, and "If your traffic ramps up
too quickly, you will get 429s" (docs.fireworks.ai/serverless/rate-limits,
read 2026-11-01).

AdaptivePacer implements the markspace.llm.RequestPacer hook. Every HTTP
attempt, retries included, calls acquire() and waits in one line, in arrival
order. Only the caller at the head of the line waits on time. The head starts
when the clock reaches both the next start (the previous start plus 1/rate)
and the Retry-After floor; then the next caller becomes the head. So starts
are at least 1/rate apart, no start comes before the floor, and a rate signal
never re-orders the line: it only moves the time at which the head may start.

Cuts. A 429, or a response with the over-limit header, from a request that
started in the current generation halves the rate (not below min_rps), starts
a new generation, moves the next start to one interval at the new rate after
the response, and holds off steps for cooldown_seconds. Signals from requests
that started before the cut do not cut again. Every Retry-After the client
reports (it reads one from a 429 only) raises the floor to the response time
plus the Retry-After, whatever the request's generation.

Steps. When a caller starts step_seconds or more after the last step check,
the step window closes. The rate rises by step_rps (to at most max_rps) only
if, in that window, at least one response was a 200; no response was a 429, a
5xx or over the limit, and no attempt timed out; callers started at least
0.9 x rate x window seconds; the cooldown has passed; and the last minute's
generated tokens stay below target_fraction of the limit that the provider
reports in its header. A 5xx or a timeout blocks the step but does not cut
the rate. A limit header that is not a finite positive number is logged and
ignored; the previous limit stays.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from typing import Any

logger = logging.getLogger(__name__)

# Fireworks response headers. The limit is tokens per minute; over-limit is
# "yes" while the account is over its current limit (read by the backend's
# rate limiter too; not described in the rate-limit docs).
GENERATED_LIMIT_HEADER = "x-ratelimit-limit-tokens-generated"
OVER_LIMIT_HEADER = "x-ratelimit-over-limit"


class AdaptivePacer:
    """Thread-safe pacer shared by every request to one model on one account."""

    def __init__(
        self,
        start_rps: float,
        step_rps: float,
        step_seconds: float,
        max_rps: float,
        min_rps: float,
        target_fraction: float,
        cooldown_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        wait: Callable[
            [threading.Condition, float | None], object
        ] = threading.Condition.wait,
        on_change: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        if not 0 < min_rps <= start_rps <= max_rps:
            raise ValueError("need 0 < min_rps <= start_rps <= max_rps")
        if step_rps <= 0 or step_seconds <= 0 or cooldown_seconds < 0:
            raise ValueError("need step_rps > 0, step_seconds > 0, cooldown >= 0")
        if not 0 < target_fraction <= 1:
            raise ValueError("need 0 < target_fraction <= 1")
        self._rate = start_rps
        self._step_rps = step_rps
        self._step_seconds = step_seconds
        self._max_rps = max_rps
        self._min_rps = min_rps
        self._target_fraction = target_fraction
        self._cooldown_seconds = cooldown_seconds
        self._clock = clock
        # wait(condition, timeout) releases the condition's lock while it
        # waits, like threading.Condition.wait; tests inject a fake clock's.
        self._wait = wait
        self._on_change = on_change
        self._cond = threading.Condition(threading.Lock())
        # Callers waiting to start, in arrival order; the head is _line[0].
        self._line: deque[object] = deque()
        now = clock()
        self._next_start = now  # previous start + 1/rate
        self._retry_until = now  # Retry-After floor: no start before it
        self._last_step = now
        self._hold_until = now  # no step before this time
        self._generation = 0  # rises by one at each cut
        self._limited_since_step = False
        self._succeeded_since_step = False
        self._started_since_step = 0
        # (time, generated tokens) per response, kept for the last minute
        self._generated: deque[tuple[float, int]] = deque()
        # Unknown until a response carries the header: the rate holds until then.
        self._generated_limit: float | None = None

    @property
    def rate(self) -> float:
        with self._cond:
            return self._rate

    def acquire(self) -> int:
        """
        Wait in line until this caller may start. Returns the generation the
        start belongs to, which the caller passes back to on_response.
        """
        with self._cond:
            me = object()
            self._line.append(me)
            try:
                now = self._wait_for_turn(me)
            except BaseException:
                # A caller that leaves unstarted must not block the line.
                self._line.remove(me)
                self._cond.notify_all()
                raise
            self._line.popleft()
            try:
                self._maybe_step(now)
            finally:
                # Whatever happens, the next caller becomes the head.
                self._next_start = now + 1.0 / self._rate
                self._started_since_step += 1
                self._cond.notify_all()
            return self._generation

    def _wait_for_turn(self, me: object) -> float:
        """
        Block until *me* heads the line and the clock reaches its start time;
        return that time. Caller holds the lock.
        """
        while True:
            if self._line[0] is not me:
                self._wait(self._cond, None)
                continue
            now = self._clock()
            # A cut or a Retry-After only moves these times later; a head
            # that wakes at an older start time computes its wait again.
            start_at = max(self._next_start, self._retry_until)
            if now >= start_at:
                return now
            self._wait(self._cond, start_at - now)

    def on_response(
        self,
        status: int,
        headers: Mapping[str, str],
        usage: Mapping[str, Any] | None,
        generation: int,
        retry_after: float | None,
    ) -> None:
        """
        Record a response to a request that started in *generation*: its
        limit header, its generated tokens, and any rate signal.
        """
        with self._cond:
            now = self._clock()
            self._read_limit(headers)
            if usage is not None and isinstance(usage.get("completion_tokens"), int):
                self._generated.append((now, usage["completion_tokens"]))
            over_limit = headers.get(OVER_LIMIT_HEADER) == "yes"
            if retry_after is not None:
                self._retry_until = max(self._retry_until, now + retry_after)
            if (status == 429 or over_limit) and generation == self._generation:
                # One cut per generation. The head also waits for the floor.
                self._rate = max(self._min_rps, self._rate / 2)
                self._generation += 1
                self._next_start = now + 1.0 / self._rate
                self._report("rate_limited" if status == 429 else "over_limit", now)
            if status == 429 or over_limit:
                self._hold_until = now + self._cooldown_seconds
            if status == 429 or over_limit or status >= 500:
                self._limited_since_step = True
            if status == 200:
                self._succeeded_since_step = True

    def on_failure(self) -> None:
        """Record an attempt that timed out: its step window does not step."""
        with self._cond:
            self._limited_since_step = True

    def _read_limit(self, headers: Mapping[str, str]) -> None:
        """Keep the header's limit if it is a finite positive number. Caller holds the lock."""
        raw = headers.get(GENERATED_LIMIT_HEADER)
        if raw is None:
            return
        try:
            limit = float(raw)  # "inf" and "1e309" give infinity, not an error
        except ValueError:
            limit = math.nan
        if math.isfinite(limit) and limit > 0:
            self._generated_limit = limit
        else:
            logger.warning(
                "Unusable %s %r; keeping the previous limit",
                GENERATED_LIMIT_HEADER,
                raw,
            )

    def _generated_last_minute(self, now: float) -> int:
        while self._generated and self._generated[0][0] < now - 60.0:
            self._generated.popleft()
        return sum(tokens for _, tokens in self._generated)

    def _maybe_step(self, now: float) -> None:
        """Raise the rate one step if the last step window allows it. Caller holds the lock."""
        elapsed = now - self._last_step
        if elapsed < self._step_seconds:
            return
        self._last_step = now
        limited, succeeded = self._limited_since_step, self._succeeded_since_step
        started = self._started_since_step
        self._limited_since_step = False
        self._succeeded_since_step = False
        self._started_since_step = 0
        # A step needs callers that used about the whole current rate:
        # otherwise the rate is not what limits the traffic, and raising it
        # only stores a burst.
        saturated = started >= 0.9 * self._rate * elapsed
        if limited or not succeeded or not saturated or now < self._hold_until:
            return
        if self._generated_limit is None or self._rate >= self._max_rps:
            return
        target = self._target_fraction * self._generated_limit
        if self._generated_last_minute(now) >= target:
            return
        self._rate = min(self._max_rps, self._rate + self._step_rps)
        self._report("step", now)

    def _report(self, event: str, now: float) -> None:
        if self._on_change is None:
            return
        # A failing listener (a full disk under the event log) must neither
        # stop the line nor lose a paid response: log it and go on.
        try:
            self._on_change(
                {
                    "event": event,
                    "time": now,
                    "rate": self._rate,
                    "generated_last_minute": self._generated_last_minute(now),
                    "generated_limit_per_minute": self._generated_limit,
                }
            )
        except Exception:
            logger.exception("Pacer on_change failed for %r; pacing goes on", event)
