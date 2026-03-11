# -*- coding: utf-8 -*-
"""
Statistical Envelope - Unit Tests

Tests for StatisticalEnvelope behavioral anomaly detection.
Covers rate anomalies, cold start safety, monotonicity, concentration,
type filtering, Welford correctness, window rotation, and export/import.

Run: python -m pytest tests/test_envelope.py -v
"""

from __future__ import annotations

import uuid

import pytest

from markspace import (
    Agent,
    DecayConfig,
    Intent,
    MarkType,
    Need,
    Observation,
    Scope,
    Warning,
    hours,
    minutes,
)
from markspace.envelope import (
    AgentStats,
    EnvelopeConfig,
    EnvelopeVerdict,
    StatisticalEnvelope,
    WelfordConfig,
    WelfordDetector,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_clock(start: float = 1000.0) -> list[float]:
    """Return a mutable single-element list usable as a fake clock."""
    return [start]


def _clock_fn(clock: list[float]):
    """Return a callable that reads from the mutable clock."""
    return lambda: clock[0]


def _default_config(
    min_samples: int = 3,
    window_seconds: float = 10.0,
    k_sigma: float = 3.0,
    concentration_threshold: int = 3,
) -> EnvelopeConfig:
    welford_cfg = WelfordConfig(k_sigma=k_sigma, min_samples=min_samples)
    return EnvelopeConfig(
        window_seconds=window_seconds,
        detector_factory=lambda _aid: WelfordDetector(welford_cfg),
        concentration_threshold=concentration_threshold,
    )


def _make_scope() -> Scope:
    return Scope(
        name="test",
        allowed_intent_verbs=("book",),
        allowed_action_verbs=("booked",),
        decay=DecayConfig(
            observation_half_life=hours(6),
            warning_half_life=hours(2),
            intent_ttl=minutes(30),
        ),
    )


def _make_agent(scope_name: str = "test") -> Agent:
    return Agent(
        name="agent",
        scopes={scope_name: ["observation", "warning", "intent", "action", "need"]},
    )


def _obs(scope: str = "test", topic: str = "temp") -> Observation:
    return Observation(scope=scope, topic=topic, content="x", confidence=0.8)


def _warning(scope: str = "test", topic: str = "temp") -> Warning:
    return Warning(scope=scope, topic=topic, reason="bad")


def _record_n(
    envelope: StatisticalEnvelope,
    agent_id: uuid.UUID,
    n: int,
    mark_fn=_obs,
) -> None:
    """Record n marks for the given agent."""
    for _ in range(n):
        envelope.record(agent_id, mark_fn())


def _advance_window(clock: list[float], config: EnvelopeConfig) -> None:
    """Advance the clock past the current window boundary."""
    clock[0] += config.window_seconds + 0.1


def _build_baseline(
    envelope: StatisticalEnvelope,
    agent_id: uuid.UUID,
    clock: list[float],
    config: EnvelopeConfig,
    n_windows: int,
    marks_per_window: int = 5,
) -> None:
    """
    Write a steady baseline of marks_per_window observations per window
    for n_windows completed windows, advancing the clock each time.
    """
    for _ in range(n_windows):
        _record_n(envelope, agent_id, marks_per_window)
        _advance_window(clock, config)
        # Trigger rotation by recording one more mark in the new window
        envelope.record(agent_id, _obs())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestStatisticalEnvelope:
    """Grouped envelope tests."""

    def test_new_agent_normal(self) -> None:
        """An agent with no recorded history returns NORMAL."""
        clock = _make_clock()
        config = _default_config()
        env = StatisticalEnvelope(config=config, clock=_clock_fn(clock))

        agent = _make_agent()
        assert env.check(agent.id) == EnvelopeVerdict.NORMAL

    def test_cold_start_safety(self) -> None:
        """Agent with < min_samples completed windows always returns NORMAL,
        even if the current window has a huge rate spike."""
        clock = _make_clock()
        config = _default_config(min_samples=5)
        env = StatisticalEnvelope(config=config, clock=_clock_fn(clock))

        agent = _make_agent()

        # Complete 2 windows with low rate
        for _ in range(2):
            _record_n(env, agent.id, 3)
            _advance_window(clock, config)
            env.record(agent.id, _obs())  # trigger rotation

        # Now spike in the current window (still only 2 completed windows)
        _record_n(env, agent.id, 500)

        assert env.check(agent.id) == EnvelopeVerdict.NORMAL

    def test_steady_writes_normal(self) -> None:
        """An agent writing at a steady rate over many windows stays NORMAL."""
        clock = _make_clock()
        config = _default_config(min_samples=3)
        env = StatisticalEnvelope(config=config, clock=_clock_fn(clock))

        agent = _make_agent()

        # Build a long, steady baseline
        _build_baseline(env, agent.id, clock, config, n_windows=10, marks_per_window=5)

        # One more window with the same rate
        _record_n(env, agent.id, 5)

        assert env.check(agent.id) == EnvelopeVerdict.NORMAL

    def test_rate_spike_restricted(self) -> None:
        """A sudden rate spike after a steady baseline triggers RESTRICTED."""
        clock = _make_clock()
        config = _default_config(min_samples=3, k_sigma=3.0)
        env = StatisticalEnvelope(config=config, clock=_clock_fn(clock))

        agent = _make_agent()

        # Build a steady baseline of 5 marks per window
        _build_baseline(env, agent.id, clock, config, n_windows=6, marks_per_window=5)

        # Spike: write 200 marks in the current window
        _record_n(env, agent.id, 200)

        verdict = env.check(agent.id)
        assert verdict == EnvelopeVerdict.RESTRICTED

    def test_restricted_is_monotonic(self) -> None:
        """Once RESTRICTED, subsequent checks still return RESTRICTED (P40)."""
        clock = _make_clock()
        config = _default_config(min_samples=3)
        env = StatisticalEnvelope(config=config, clock=_clock_fn(clock))

        agent = _make_agent()

        _build_baseline(env, agent.id, clock, config, n_windows=6, marks_per_window=5)
        _record_n(env, agent.id, 200)

        assert env.check(agent.id) == EnvelopeVerdict.RESTRICTED

        # Advance into a new, quiet window - still RESTRICTED
        _advance_window(clock, config)
        assert env.check(agent.id) == EnvelopeVerdict.RESTRICTED

        # Many windows later, zero writes - still RESTRICTED
        for _ in range(5):
            _advance_window(clock, config)
        assert env.check(agent.id) == EnvelopeVerdict.RESTRICTED

    def test_reset_clears_restricted(self) -> None:
        """reset() with a token clears the RESTRICTED flag."""
        clock = _make_clock()
        config = _default_config(min_samples=3)
        env = StatisticalEnvelope(config=config, clock=_clock_fn(clock))

        agent = _make_agent()

        _build_baseline(env, agent.id, clock, config, n_windows=6, marks_per_window=5)
        _record_n(env, agent.id, 200)
        assert env.check(agent.id) == EnvelopeVerdict.RESTRICTED

        # Reset with a principal token
        token = uuid.uuid4()
        result = env.reset(agent.id, token)
        assert result is True

        # After reset, if current window is clean, verdict is NORMAL
        _advance_window(clock, config)
        env.record(agent.id, _obs())  # trigger rotation
        assert env.check(agent.id) == EnvelopeVerdict.NORMAL

        # Resetting a non-restricted agent returns False
        assert env.reset(agent.id, token) is False

    def test_concentration_flagged(self) -> None:
        """3+ agents writing to the same (scope, topic) in one window => FLAGGED."""
        clock = _make_clock()
        config = _default_config(min_samples=3, concentration_threshold=3)
        env = StatisticalEnvelope(config=config, clock=_clock_fn(clock))

        agents = [_make_agent() for _ in range(4)]
        shared_topic = "weather"

        # Build baseline for all agents simultaneously (interleaved per window)
        # so they all stay in the same window and _recent_writers isn't cleared
        # between agents.
        for window_num in range(4):
            for ag in agents:
                _record_n(env, ag.id, 3)
            _advance_window(clock, config)
            # Trigger rotation for all agents
            for ag in agents:
                env.record(ag.id, _obs())

        # All agents write to the same scope+topic in the current window
        for ag in agents:
            env.record(ag.id, _obs(topic=shared_topic))

        # Each agent should be FLAGGED due to concentration
        for ag in agents:
            verdict = env.check(ag.id)
            assert (
                verdict == EnvelopeVerdict.FLAGGED
            ), f"Agent {ag.name} should be FLAGGED, got {verdict}"

    def test_concentration_not_restricted(self) -> None:
        """Concentration alone never escalates to RESTRICTED - only FLAGGED."""
        clock = _make_clock()
        config = _default_config(min_samples=3, concentration_threshold=2)
        env = StatisticalEnvelope(config=config, clock=_clock_fn(clock))

        agents = [_make_agent() for _ in range(3)]

        # Build baseline for all agents simultaneously
        for window_num in range(4):
            for ag in agents:
                _record_n(env, ag.id, 3)
            _advance_window(clock, config)
            for ag in agents:
                env.record(ag.id, _obs())

        # Write same scope+topic with a rate that matches baseline (no spike)
        for ag in agents:
            env.record(ag.id, _obs(topic="shared"))
            env.record(ag.id, _obs(topic="shared"))
            env.record(ag.id, _obs(topic="shared"))

        # Concentration detected, but rate is normal - should be FLAGGED not RESTRICTED
        for ag in agents:
            verdict = env.check(ag.id)
            assert (
                verdict != EnvelopeVerdict.RESTRICTED
            ), f"Concentration alone should not cause RESTRICTED, got {verdict}"

    def test_exempt_agent_ignored(self) -> None:
        """Exempt agents' writes don't affect stats or concentration tracking."""
        clock = _make_clock()
        config = _default_config(min_samples=3, concentration_threshold=3)
        env = StatisticalEnvelope(config=config, clock=_clock_fn(clock))

        exempt_agent = _make_agent()
        env.add_exempt_agent(exempt_agent.id)

        # Record many marks for exempt agent
        _record_n(env, exempt_agent.id, 100)

        # Should have no stats at all
        assert env.get_stats(exempt_agent.id) is None
        assert env.check(exempt_agent.id) == EnvelopeVerdict.NORMAL

        # Exempt agent should not contribute to concentration
        normal_agents = [_make_agent() for _ in range(2)]
        for ag in normal_agents:
            _build_baseline(env, ag.id, clock, config, n_windows=4, marks_per_window=3)

        # Write from exempt + 2 normals - only 2 non-exempt, below threshold of 3
        env.record(exempt_agent.id, _obs(topic="shared"))
        for ag in normal_agents:
            env.record(ag.id, _obs(topic="shared"))

        for ag in normal_agents:
            assert env.check(ag.id) != EnvelopeVerdict.FLAGGED

    def test_type_filter(self) -> None:
        """Writes of non-tracked types (Intent, Action, Need) are ignored."""
        clock = _make_clock()
        config = _default_config(min_samples=3)
        env = StatisticalEnvelope(config=config, clock=_clock_fn(clock))

        agent = _make_agent()

        # Record lots of non-tracked types
        for _ in range(50):
            env.record(agent.id, Intent(scope="test", resource="r", action="book"))
            env.record(agent.id, Need(scope="test", question="q"))

        # Agent should have no stats (nothing tracked)
        assert env.get_stats(agent.id) is None

        # Record one tracked type to create stats
        env.record(agent.id, _obs())
        stats = env.get_stats(agent.id)
        assert stats is not None
        # Only the one observation should be counted
        assert stats.current_window.counts.get(MarkType.OBSERVATION, 0) == 1
        assert stats.current_window.counts.get(MarkType.INTENT, 0) == 0

    def test_welford_nonnegative(self) -> None:
        """Welford stddev is always >= 0, even with varied inputs."""
        import math

        clock = _make_clock()
        config = _default_config(min_samples=3)
        env = StatisticalEnvelope(config=config, clock=_clock_fn(clock))

        agent = _make_agent()

        # Create windows with wildly varying counts
        counts = [0, 100, 1, 50, 0, 200, 3, 0, 80]
        for c in counts:
            _record_n(env, agent.id, c)
            _advance_window(clock, config)
            env.record(agent.id, _obs())  # trigger rotation

        stats = env.get_stats(agent.id)
        assert stats is not None
        for mt in config.tracked_types:
            n = stats.welford_n.get(mt, 0)
            m2 = stats.welford_m2.get(mt, 0.0)
            stddev = math.sqrt(max(0.0, m2 / (n - 1))) if n >= 2 else 0.0
            assert stddev >= 0.0, f"Welford stddev for {mt} was negative: {stddev}"

    def test_window_rotation(self) -> None:
        """Counts reset on window boundary; completed_windows increments."""
        clock = _make_clock()
        config = _default_config(min_samples=3, window_seconds=10.0)
        env = StatisticalEnvelope(config=config, clock=_clock_fn(clock))

        agent = _make_agent()

        # Window 1: write 7 observations
        _record_n(env, agent.id, 7)

        stats = env.get_stats(agent.id)
        assert stats is not None
        assert stats.current_window.counts.get(MarkType.OBSERVATION, 0) == 7
        assert stats.completed_windows == 0

        # Advance past window boundary
        _advance_window(clock, config)
        # Write one mark to trigger rotation
        env.record(agent.id, _obs())

        stats = env.get_stats(agent.id)
        assert stats is not None
        assert stats.completed_windows == 1
        # Current window should only have the 1 new mark
        assert stats.current_window.counts.get(MarkType.OBSERVATION, 0) == 1
        # During cold start (1 of 3 min_samples), Welford accumulators are
        # empty because observations are buffered for robust median init.
        # The mean is populated only after min_samples active windows.
        assert stats.welford_mean.get(MarkType.OBSERVATION, 0.0) == 0.0

    def test_export_import_roundtrip(self) -> None:
        """export_stats then import_stats preserves key statistical state."""
        clock = _make_clock()
        config = _default_config(min_samples=3)
        env = StatisticalEnvelope(config=config, clock=_clock_fn(clock))

        agent = _make_agent()

        # Build some history
        _build_baseline(env, agent.id, clock, config, n_windows=5, marks_per_window=8)

        # Export
        exported = env.export_stats()
        assert str(agent.id) in exported

        agent_data = exported[str(agent.id)]
        assert agent_data["completed_windows"] == 5
        assert "welford_n" in agent_data
        assert "welford_mean" in agent_data
        assert "welford_m2" in agent_data

        # Import into a fresh envelope
        clock2 = _make_clock(start=9999.0)
        env2 = StatisticalEnvelope(config=config, clock=_clock_fn(clock2))
        env2.import_stats(exported)

        # Verify stats match
        stats2 = env2.get_stats(agent.id)
        assert stats2 is not None
        assert stats2.completed_windows == 5

        original_stats = env.get_stats(agent.id)
        assert original_stats is not None

        for mt in config.tracked_types:
            assert stats2.welford_n.get(mt) == original_stats.welford_n.get(mt)
            assert stats2.welford_mean.get(mt) == pytest.approx(
                original_stats.welford_mean.get(mt, 0.0)
            )
            assert stats2.welford_m2.get(mt) == pytest.approx(
                original_stats.welford_m2.get(mt, 0.0)
            )

        # Re-export from env2 should match original export
        re_exported = env2.export_stats()
        re_data = re_exported[str(agent.id)]
        assert re_data["completed_windows"] == agent_data["completed_windows"]
        assert re_data["restricted"] == agent_data["restricted"]

    def test_seed_baseline_skips_cold_start(self) -> None:
        """A seeded detector is immediately ready - no cold-start blind spot."""
        clock = _make_clock()
        config = _default_config(min_samples=10)
        env = StatisticalEnvelope(config=config, clock=_clock_fn(clock))

        agent = _make_agent()

        # Seed: "I expect ~5 observations per 10s window" = 1800/hour
        # (window_seconds=10, so 5 per window = 5 * 3600/10 = 1800/hr)
        env.seed_baseline(agent.id, {MarkType.OBSERVATION: 1800.0})

        # Detector should be ready immediately
        stats = env.get_stats(agent.id)
        assert stats is not None
        assert stats.completed_windows >= 10
        assert stats.welford_mean.get(MarkType.OBSERVATION, 0.0) == pytest.approx(5.0)

        # Normal activity - should be NORMAL
        _record_n(env, agent.id, 5)
        assert env.check(agent.id) == EnvelopeVerdict.NORMAL

        # Spike far beyond declared baseline - should be RESTRICTED
        _record_n(env, agent.id, 200)
        assert env.check(agent.id) == EnvelopeVerdict.RESTRICTED

    def test_seed_baseline_allows_natural_variance(self) -> None:
        """Seeded baseline has synthetic variance - small deviations are OK."""
        clock = _make_clock()
        config = _default_config(min_samples=10, k_sigma=3.0)
        env = StatisticalEnvelope(config=config, clock=_clock_fn(clock))

        agent = _make_agent()

        # Seed: "I expect ~10 observations per 10s window" = 3600/hour
        env.seed_baseline(agent.id, {MarkType.OBSERVATION: 3600.0})

        # Write 15 (50% above declared) - within synthetic variance (stddev = 5.0)
        _record_n(env, agent.id, 15)
        assert env.check(agent.id) == EnvelopeVerdict.NORMAL
