# -*- coding: utf-8 -*-
"""
Integration tests for the defense-in-depth stack:
envelope -> barrier -> guard -> space.

Tests the full cycle: anomalous behavior detected by envelope,
barrier applied by guard, agent blocked on subsequent writes.
"""

import uuid

import pytest

from markspace import (
    Agent,
    AgentBarrier,
    DecayConfig,
    Guard,
    GuardVerdict,
    MarkSpace,
    MarkType,
    Observation,
    Need,
    Scope,
    ScopeError,
    Source,
    Warning,
    hours,
    minutes,
)
from markspace.envelope import (
    EnvelopeConfig,
    EnvelopeVerdict,
    StatisticalEnvelope,
    WelfordConfig,
    WelfordDetector,
)
from markspace.probe import DiagnosticProbe, ProbeConfig, ProbeVerdict


def _make_space(clock: float = 0.0) -> MarkSpace:
    return MarkSpace(
        scopes=[
            Scope(
                name="test",
                decay=DecayConfig(
                    observation_half_life=hours(6),
                    warning_half_life=hours(2),
                    intent_ttl=minutes(30),
                ),
                allowed_intent_verbs=("book",),
                allowed_action_verbs=("booked",),
            ),
            Scope(
                name="diagnostics",
                decay=DecayConfig(
                    observation_half_life=hours(6),
                    warning_half_life=hours(2),
                    intent_ttl=minutes(30),
                ),
            ),
        ],
        clock=clock,
    )


def _make_agent(name: str = "agent-1") -> Agent:
    return Agent(
        name=name,
        scopes={
            "test": ["intent", "action", "observation", "warning", "need"],
            "diagnostics": ["observation"],
        },
        read_scopes=frozenset({"test", "diagnostics"}),
    )


def _make_envelope(
    clock_fn, min_samples: int = 3, window: float = 10.0
) -> StatisticalEnvelope:
    welford_cfg = WelfordConfig(k_sigma=2.0, min_samples=min_samples)
    return StatisticalEnvelope(
        config=EnvelopeConfig(
            window_seconds=window,
            detector_factory=lambda _aid: WelfordDetector(welford_cfg),
            concentration_threshold=3,
        ),
        clock=clock_fn,
    )


class TestEnvelopeTriggersBarrier:
    """Envelope detects anomaly -> guard creates barrier -> agent blocked."""

    def test_rate_spike_blocks_agent(self):
        """After baseline, a rate spike triggers RESTRICTED and barrier."""
        t = [0.0]
        clock_fn = lambda: t[0]
        space = _make_space(clock=0.0)
        envelope = _make_envelope(clock_fn, min_samples=3, window=10.0)
        guard = Guard(space, envelope=envelope)
        agent = _make_agent()

        # Build baseline: 3 windows with 2 observations each
        for window_num in range(3):
            t[0] = window_num * 10.0
            space.set_clock(t[0])
            for _ in range(2):
                guard.write_mark(
                    agent,
                    Observation(
                        scope="test",
                        topic="sensor",
                        content={"value": 42},
                        source=Source.FLEET,
                    ),
                )
            # Advance past window boundary to trigger rotation
            t[0] = (window_num + 1) * 10.0 - 0.01

        # Now spike: 20 observations in next window (10x baseline)
        t[0] = 30.0
        space.set_clock(t[0])
        # First write triggers window rotation and feeds baseline into Welford
        guard.write_mark(
            agent,
            Observation(
                scope="test",
                topic="sensor",
                content={"spike": True},
                source=Source.FLEET,
            ),
        )
        for i in range(19):
            try:
                guard.write_mark(
                    agent,
                    Observation(
                        scope="test",
                        topic="sensor",
                        content={"spike": i},
                        source=Source.FLEET,
                    ),
                )
            except ScopeError:
                # Agent got restricted - expected
                break

        # Verify barrier exists
        barrier = guard.get_barrier(agent.id)
        assert barrier is not None
        assert not barrier.is_allowed_checked("test", "observation")

        # Verify agent is blocked on further writes
        with pytest.raises(ScopeError, match="blocked by barrier"):
            guard.write_mark(
                agent,
                Observation(
                    scope="test", topic="sensor", content={}, source=Source.FLEET
                ),
            )

    def test_barrier_blocks_pre_action(self):
        """Barrier also blocks intent/action writes via pre_action."""
        t = [0.0]
        clock_fn = lambda: t[0]
        space = _make_space(clock=0.0)
        envelope = _make_envelope(clock_fn, min_samples=3, window=10.0)
        guard = Guard(space, envelope=envelope)
        agent = _make_agent()

        # Manually set a barrier
        token = guard._principal_token
        barrier = AgentBarrier(agent_id=agent.id, _principal_token=token)
        barrier.narrow("test", MarkType.INTENT.value)
        guard.set_barrier(agent.id, barrier)

        # pre_action should be denied
        decision = guard.pre_action(agent, "test", "room-1", "book", confidence=0.9)
        assert decision.verdict == GuardVerdict.DENIED
        assert "blocked by barrier" in decision.reason


class TestPrincipalRestore:
    """Principal can restore an agent after false positive."""

    def test_restore_clears_barrier(self):
        space = _make_space(clock=0.0)
        guard = Guard(space)
        agent = _make_agent()

        # Apply a barrier
        token = guard._principal_token
        barrier = AgentBarrier(agent_id=agent.id, _principal_token=token)
        barrier.narrow("test", MarkType.OBSERVATION.value)
        guard.set_barrier(agent.id, barrier)

        # Verify blocked
        with pytest.raises(ScopeError):
            guard.write_mark(
                agent,
                Observation(scope="test", topic="x", content={}, source=Source.FLEET),
            )

        # Principal restores
        barrier.restore_all(token)

        # Now allowed
        mid = guard.write_mark(
            agent,
            Observation(scope="test", topic="x", content={}, source=Source.FLEET),
        )
        assert mid is not None


class TestResolvedDeferredWithBarrier:
    """Barrier applied between pre_action and resolve_deferred blocks at resolution."""

    def test_barrier_at_resolution_time(self):
        space = MarkSpace(
            scopes=[
                Scope(
                    name="test",
                    decay=DecayConfig(
                        observation_half_life=hours(6),
                        warning_half_life=hours(2),
                        intent_ttl=minutes(30),
                    ),
                    allowed_intent_verbs=("book",),
                    allowed_action_verbs=("booked",),
                    deferred=True,
                ),
            ],
            clock=0.0,
        )
        guard = Guard(space)
        agent = _make_agent("agent-deferred")

        # pre_action returns BLOCKED (deferred)
        decision = guard.pre_action(agent, "test", "room-1", "book", confidence=0.9)
        assert decision.verdict == GuardVerdict.BLOCKED

        # Now apply barrier before resolution
        token = guard._principal_token
        barrier = AgentBarrier(agent_id=agent.id, _principal_token=token)
        barrier.narrow("test", MarkType.INTENT.value)
        guard.set_barrier(agent.id, barrier)

        # resolve_deferred should deny this agent
        results = guard.resolve_deferred("test", "room-1", agents={agent.id: agent})
        assert agent.id in results
        assert results[agent.id].verdict == GuardVerdict.DENIED
        assert "barrier" in results[agent.id].reason


class TestProbeWithEnvelope:
    """Probe and envelope work together."""

    def test_probe_exempt_from_envelope(self):
        """Probe writes should not trigger envelope flagging."""
        t = [0.0]
        clock_fn = lambda: t[0]
        space = _make_space(clock=0.0)
        envelope = _make_envelope(clock_fn, min_samples=1, window=10.0)
        guard = Guard(space, envelope=envelope)

        probe = DiagnosticProbe(space)
        # Register probe agent as exempt
        envelope.add_exempt_agent(probe.probe_agent.id)

        # Write many canaries - should not trigger envelope
        for i in range(50):
            probe.inject_canary("test", content={"canary": i})

        # Probe agent should not be tracked at all
        stats = envelope.get_stats(probe.probe_agent.id)
        assert stats is None

    def test_write_mark_rejects_intent(self):
        """write_mark() must reject Intent marks."""
        space = _make_space()
        guard = Guard(space)
        agent = _make_agent()

        from markspace import Intent

        with pytest.raises(ValueError, match="Cannot write intent"):
            guard.write_mark(
                agent,
                Intent(scope="test", resource="room-1", action="book"),
            )

    def test_write_mark_rejects_action(self):
        """write_mark() must reject Action marks."""
        space = _make_space()
        guard = Guard(space)
        agent = _make_agent()

        from markspace import Action

        with pytest.raises(ValueError, match="Cannot write action"):
            guard.write_mark(
                agent,
                Action(scope="test", resource="room-1", action="booked"),
            )
