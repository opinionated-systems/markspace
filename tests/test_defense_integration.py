# -*- coding: utf-8 -*-
"""
Integration tests for the defenses: barrier -> guard -> space, the
principal's restore, the diagnostic probe, and the statistical envelope as a
monitor that a harness attaches to the space (spec 9.7).
"""

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
    Scope,
    ScopeError,
    Source,
    hours,
    minutes,
)
from markspace.envelope import (
    EnvelopeConfig,
    StatisticalEnvelope,
    WelfordConfig,
    WelfordDetector,
)
from markspace.probe import DiagnosticProbe


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


class TestBarrierBlocksWrites:
    """A barrier set on the guard blocks the agent's writes."""

    def test_barrier_blocks_pre_action(self):
        """Barrier also blocks intent/action writes via pre_action."""
        space = _make_space(clock=0.0)
        guard = Guard(space)
        agent = _make_agent()

        # Manually set a barrier
        token = guard._principal_token
        barrier = AgentBarrier(agent_id=agent.id, _principal_token=token)
        barrier.narrow("test", MarkType.INTENT.value)
        guard.set_barrier(agent.id, barrier, token)

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
        guard.set_barrier(agent.id, barrier, token)

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
        guard.set_barrier(agent.id, barrier, token)

        # resolve_deferred should deny this agent
        results = guard.resolve_deferred("test", "room-1", agents={agent.id: agent})
        assert agent.id in results
        assert results[agent.id].verdict == GuardVerdict.DENIED
        assert "barrier" in results[agent.id].reason


class TestProbeAndMonitor:
    """The envelope as a monitor, attached by a write hook, with the probe's
    agent exempt (spec 9.7, 9.9)."""

    def test_probe_exempt_from_the_monitor(self):
        """The monitor records every stored mark except those of exempt
        agents: the probe's canaries do not reach it."""
        t = [0.0]
        clock_fn = lambda: t[0]
        space = _make_space(clock=0.0)
        envelope = _make_envelope(clock_fn, min_samples=1, window=10.0)
        Guard(space)
        space.add_write_hook(lambda agent_id, mark: envelope.record(agent_id, mark))

        probe = DiagnosticProbe(space)
        envelope.add_exempt_agent(probe.probe_agent.id)
        for i in range(50):
            probe.inject_canary("test", content={"canary": i})
        assert envelope.get_stats(probe.probe_agent.id) is None

        agent = _make_agent()
        space.write(
            agent,
            Observation(scope="test", topic="x", content={}, source=Source.FLEET),
        )
        assert envelope.get_stats(agent.id) is not None


class TestWriteMarkRejectsContestedMarks:
    """Intents and actions go through pre_action and post_action only."""

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
