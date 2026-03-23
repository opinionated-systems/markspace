# -*- coding: utf-8 -*-
"""
Tests for scope-level rate limits.

P64: Rate Limit Enforcement - per-agent write cap
P65: Rate Limit Fleet Cap - fleet-wide write cap
P66: Rate Limit Independence - independent of envelope and budget
"""

from __future__ import annotations

import uuid

import pytest

from markspace import (
    Agent,
    AgentManifest,
    ConflictPolicy,
    DecayConfig,
    Guard,
    GuardVerdict,
    MarkSpace,
    MarkType,
    Observation,
    Scope,
    hours,
    minutes,
)
from markspace.envelope import EnvelopeConfig, StatisticalEnvelope
from markspace.rate_limit import RateLimitTracker, ScopeRateLimit
from markspace.space import ScopeError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def rate_limited_scope() -> Scope:
    return Scope(
        name="test",
        allowed_intent_verbs=("book",),
        allowed_action_verbs=("booked",),
        decay=DecayConfig(
            observation_half_life=hours(6),
            warning_half_life=hours(2),
            intent_ttl=minutes(30),
        ),
        conflict_policy=ConflictPolicy.FIRST_WRITER,
        rate_limit=ScopeRateLimit(
            max_writes_per_agent_per_window=3,
            max_total_writes_per_window=5,
            window_seconds=60.0,
        ),
    )


@pytest.fixture
def space(rate_limited_scope: Scope) -> MarkSpace:
    return MarkSpace(scopes=[rate_limited_scope], clock=1000.0)


@pytest.fixture
def agent() -> Agent:
    return Agent(
        name="agent-1",
        scopes={"test": ["intent", "action", "observation", "warning", "need"]},
    )


@pytest.fixture
def agent2() -> Agent:
    return Agent(
        name="agent-2",
        scopes={"test": ["intent", "action", "observation", "warning", "need"]},
    )


# ---------------------------------------------------------------------------
# ScopeRateLimit data type
# ---------------------------------------------------------------------------


class TestScopeRateLimit:
    def test_fields_optional(self) -> None:
        limit = ScopeRateLimit()
        assert limit.max_writes_per_agent_per_window is None
        assert limit.max_total_writes_per_window is None
        assert limit.window_seconds == 300.0

    def test_window_seconds_positive(self) -> None:
        with pytest.raises(Exception):
            ScopeRateLimit(window_seconds=0)
        with pytest.raises(Exception):
            ScopeRateLimit(window_seconds=-1)


# ---------------------------------------------------------------------------
# RateLimitTracker unit tests
# ---------------------------------------------------------------------------


class TestRateLimitTracker:
    def test_within_limit_allowed(self) -> None:
        tracker = RateLimitTracker()
        limit = ScopeRateLimit(max_writes_per_agent_per_window=3, window_seconds=60)
        aid = uuid.uuid4()
        for _ in range(3):
            result = tracker.check_and_record("test", aid, limit, 1000.0)
            assert result is None

    def test_exceeds_per_agent_limit(self) -> None:
        tracker = RateLimitTracker()
        limit = ScopeRateLimit(max_writes_per_agent_per_window=2, window_seconds=60)
        aid = uuid.uuid4()
        assert tracker.check_and_record("test", aid, limit, 1000.0) is None
        assert tracker.check_and_record("test", aid, limit, 1001.0) is None
        result = tracker.check_and_record("test", aid, limit, 1002.0)
        assert result is not None
        assert "Rate limit exceeded" in result

    def test_window_rotation_resets_count(self) -> None:
        tracker = RateLimitTracker()
        limit = ScopeRateLimit(max_writes_per_agent_per_window=2, window_seconds=60)
        aid = uuid.uuid4()
        tracker.check_and_record("test", aid, limit, 1000.0)
        tracker.check_and_record("test", aid, limit, 1001.0)
        # Third write at t=1002 should fail (within window)
        assert tracker.check_and_record("test", aid, limit, 1002.0) is not None
        # After window rotates (t=1061), should succeed again
        assert tracker.check_and_record("test", aid, limit, 1061.0) is None

    def test_fleet_cap(self) -> None:
        tracker = RateLimitTracker()
        limit = ScopeRateLimit(
            max_writes_per_agent_per_window=10,
            max_total_writes_per_window=3,
            window_seconds=60,
        )
        a1, a2, a3, a4 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        assert tracker.check_and_record("test", a1, limit, 1000.0) is None
        assert tracker.check_and_record("test", a2, limit, 1001.0) is None
        assert tracker.check_and_record("test", a3, limit, 1002.0) is None
        # 4th write exceeds fleet cap
        result = tracker.check_and_record("test", a4, limit, 1003.0)
        assert result is not None
        assert "Fleet rate limit" in result


# ---------------------------------------------------------------------------
# P64: Rate Limit Enforcement (guard integration)
# ---------------------------------------------------------------------------


class TestP64RateLimitEnforcement:
    def test_writes_succeed_below_limit(self, space: MarkSpace, agent: Agent) -> None:
        guard = Guard(space)
        for i in range(3):
            guard.write_mark(agent, Observation(scope="test", topic=f"t{i}"))

    def test_writes_fail_above_limit(self, space: MarkSpace, agent: Agent) -> None:
        guard = Guard(space)
        for i in range(3):
            guard.write_mark(agent, Observation(scope="test", topic=f"t{i}"))
        with pytest.raises(ScopeError, match="Rate limit exceeded"):
            guard.write_mark(agent, Observation(scope="test", topic="t4"))

    def test_rejection_visible_to_envelope(
        self, space: MarkSpace, agent: Agent
    ) -> None:
        envelope = StatisticalEnvelope(
            config=EnvelopeConfig(window_seconds=60),
            clock=space.now,
        )
        guard = Guard(space, envelope=envelope)
        for i in range(3):
            guard.write_mark(agent, Observation(scope="test", topic=f"t{i}"))
        # 4th write rejected by rate limit
        with pytest.raises(ScopeError):
            guard.write_mark(agent, Observation(scope="test", topic="t4"))
        # Envelope should have seen the attempt
        stats = envelope.get_stats(agent.id)
        assert stats is not None


# ---------------------------------------------------------------------------
# P65: Fleet Cap (guard integration)
# ---------------------------------------------------------------------------


class TestP64RateLimitOnExecute:
    """Rate limits also apply to contested writes (intents via execute)."""

    def test_execute_rejected_above_limit(self, space: MarkSpace, agent: Agent) -> None:
        guard = Guard(space)
        # Use 3 observations to hit the per-agent limit
        for i in range(3):
            guard.write_mark(agent, Observation(scope="test", topic=f"t{i}"))
        # Now execute should also be rejected - intent write hits rate limit
        decision, _ = guard.execute(
            agent,
            "test",
            "res-1",
            "book",
            "booked",
            tool_fn=lambda: "done",
        )
        assert decision.verdict == GuardVerdict.DENIED
        assert "Rate limit" in decision.reason


class TestP65FleetCap:
    def test_fleet_cap_blocks_all_agents(
        self, space: MarkSpace, agent: Agent, agent2: Agent
    ) -> None:
        guard = Guard(space)
        # Agent 1 writes 3 (at per-agent limit)
        for i in range(3):
            guard.write_mark(agent, Observation(scope="test", topic=f"t{i}"))
        # Agent 2 writes 2 (fleet total now 5 = fleet cap)
        for i in range(2):
            guard.write_mark(agent2, Observation(scope="test", topic=f"t{i}"))
        # Both agents should be blocked
        with pytest.raises(ScopeError, match="Fleet rate limit"):
            guard.write_mark(agent2, Observation(scope="test", topic="t5"))


# ---------------------------------------------------------------------------
# P66: Rate Limit Independence
# ---------------------------------------------------------------------------


class TestP66RateLimitIndependence:
    def test_rate_limit_independent_of_budget(self, space: MarkSpace) -> None:
        """Agent within budget but over rate limit is still rejected."""
        from markspace.budget import TokenBudget

        guard = Guard(space)
        agent = Agent(
            name="budgeted",
            scopes={"test": ["observation"]},
            manifest=AgentManifest(
                budget=TokenBudget(max_input_tokens_total=999999),
            ),
        )
        for i in range(3):
            guard.write_mark(agent, Observation(scope="test", topic=f"t{i}"))
        # Over rate limit, but budget is fine
        with pytest.raises(ScopeError, match="Rate limit"):
            guard.write_mark(agent, Observation(scope="test", topic="t4"))

    def test_concurrent_writes_respect_limit(
        self, space: MarkSpace, agent: Agent
    ) -> None:
        """Rate limit holds under concurrent writes (thread safety)."""
        import threading

        guard = Guard(space)
        errors: list[Exception] = []
        successes = []

        def try_write(topic: str) -> None:
            try:
                guard.write_mark(agent, Observation(scope="test", topic=topic))
                successes.append(topic)
            except ScopeError:
                errors.append(topic)

        threads = [
            threading.Thread(target=try_write, args=(f"t{i}",)) for i in range(6)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Per-agent limit is 3: at most 3 successes
        assert len(successes) <= 3
        assert len(errors) >= 3

    def test_no_rate_limit_scope_unaffected(self) -> None:
        """Scopes without rate limits are not affected."""
        no_limit_scope = Scope(
            name="unlimited",
            decay=DecayConfig(
                observation_half_life=hours(6),
                warning_half_life=hours(2),
                intent_ttl=minutes(30),
            ),
        )
        space = MarkSpace(scopes=[no_limit_scope], clock=1000.0)
        guard = Guard(space)
        agent = Agent(name="free", scopes={"unlimited": ["observation"]})
        # Should be able to write many marks without rate limit
        for i in range(20):
            guard.write_mark(agent, Observation(scope="unlimited", topic=f"t{i}"))
