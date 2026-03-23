# -*- coding: utf-8 -*-
"""
Markspace Coordination Protocol - Scope Rate Limits

Per-scope write rate limits enforced by the guard. Each scope can
optionally define per-agent and fleet-wide write caps within a
sliding time window.

Rate limits operate independently of the statistical envelope and
token budgets (P66). An agent within its rate limit but flagged by
the envelope is still subject to envelope restrictions; an agent
within its envelope baseline but exceeding its rate limit is still
rejected.

Spec Section 9.12.
"""

from __future__ import annotations

import uuid
from collections import deque
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field


class ScopeRateLimit(BaseModel):
    """Rate limit configuration for a scope.

    All limit fields are optional. Omitted fields mean no limit.
    window_seconds defines the sliding window duration.

    Spec Section 9.12.
    """

    model_config = ConfigDict(frozen=True)

    max_writes_per_agent_per_window: int | None = None
    max_total_writes_per_window: int | None = None
    window_seconds: float = Field(default=300.0, gt=0)


@dataclass
class RateLimitTracker:
    """Tracks write timestamps for rate limit enforcement.

    Uses sliding windows with timestamp deques rather than fixed-boundary
    counters to avoid boundary-crossing edge cases.

    Thread safety: the guard holds its own lock when calling these methods.
    """

    # (scope, agent_id) -> timestamps of writes in current window
    _per_agent: dict[tuple[str, uuid.UUID], deque[float]] = field(default_factory=dict)
    # scope -> timestamps of all writes in current window
    _per_scope: dict[str, deque[float]] = field(default_factory=dict)

    def check_and_record(
        self,
        scope: str,
        agent_id: uuid.UUID,
        limit: ScopeRateLimit,
        now: float,
    ) -> str | None:
        """Check rate limits and record a write if allowed.

        Returns a rejection reason string if the write exceeds a limit,
        or None if the write is allowed.

        P64: per-agent limit enforcement.
        P65: fleet-wide cap enforcement.
        """
        window_start = now - limit.window_seconds

        # Check per-agent limit
        if limit.max_writes_per_agent_per_window is not None:
            agent_key = (scope, agent_id)
            agent_ts = self._per_agent.get(agent_key)
            if agent_ts is None:
                agent_ts = deque()
                self._per_agent[agent_key] = agent_ts
            self._prune_window(agent_ts, window_start)
            if len(agent_ts) >= limit.max_writes_per_agent_per_window:
                return (
                    f"Rate limit exceeded: agent has {len(agent_ts)} writes "
                    f"in scope '{scope}' within {limit.window_seconds}s window "
                    f"(limit: {limit.max_writes_per_agent_per_window})"
                )

        # Check fleet-wide limit
        if limit.max_total_writes_per_window is not None:
            scope_ts = self._per_scope.get(scope)
            if scope_ts is None:
                scope_ts = deque()
                self._per_scope[scope] = scope_ts
            self._prune_window(scope_ts, window_start)
            if len(scope_ts) >= limit.max_total_writes_per_window:
                return (
                    f"Fleet rate limit exceeded: {len(scope_ts)} total writes "
                    f"in scope '{scope}' within {limit.window_seconds}s window "
                    f"(limit: {limit.max_total_writes_per_window})"
                )

        # Record the write. This happens before the caller's space.write(),
        # so if space.write() later fails (e.g., scope validation rejects an
        # invalid verb), the rate limit slot is consumed for a write that
        # never stored. This is conservative - the agent loses a slot in the
        # window but is never incorrectly allowed. Splitting check from record
        # would require the caller to call back on success, adding complexity
        # for a case that only matters when agents send malformed marks.
        if limit.max_writes_per_agent_per_window is not None:
            agent_key = (scope, agent_id)
            self._per_agent.setdefault(agent_key, deque()).append(now)
        if limit.max_total_writes_per_window is not None:
            self._per_scope.setdefault(scope, deque()).append(now)

        return None

    @staticmethod
    def _prune_window(timestamps: deque[float], window_start: float) -> None:
        """Remove timestamps older than the window start."""
        while timestamps and timestamps[0] < window_start:
            timestamps.popleft()
