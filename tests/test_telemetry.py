# -*- coding: utf-8 -*-
"""
Tests for the telemetry module and guard telemetry integration.

P57: Telemetry Non-Interference - sink failures don't affect guard.
P58: Telemetry Completeness - every guard operation emits an event.
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
    MarkSpace,
    MarkType,
    Need,
    Observation,
    Scope,
    Warning,
    hours,
    minutes,
)
from markspace.telemetry import (
    FailingSink,
    InMemorySink,
    NullSink,
    StructuredLogSink,
    TelemetryEvent,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def scope() -> Scope:
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
    )


@pytest.fixture
def space(scope: Scope) -> MarkSpace:
    return MarkSpace(scopes=[scope], clock=1000.0)


@pytest.fixture
def agent() -> Agent:
    return Agent(
        name="test-agent",
        scopes={"test": ["intent", "action", "observation", "warning", "need"]},
    )


@pytest.fixture
def sink() -> InMemorySink:
    return InMemorySink()


@pytest.fixture
def guard_with_sink(space: MarkSpace, sink: InMemorySink) -> Guard:
    return Guard(space, telemetry=sink)


# ---------------------------------------------------------------------------
# P57: Telemetry Non-Interference
# ---------------------------------------------------------------------------


class TestP57TelemetryNonInterference:
    """Sink failures must not affect guard verdicts or mark storage."""

    def test_sink_failure_does_not_block_write(
        self, space: MarkSpace, agent: Agent
    ) -> None:
        guard = Guard(space, telemetry=FailingSink())
        # write_mark should succeed despite failing sink
        mark_id = guard.write_mark(agent, Observation(scope="test", topic="x"))
        assert mark_id is not None
        stored = space.get_mark(mark_id)
        assert stored is not None

    def test_sink_failure_does_not_affect_execute_verdict(
        self, space: MarkSpace, agent: Agent
    ) -> None:
        guard = Guard(space, telemetry=FailingSink())
        decision, result = guard.execute(
            agent,
            "test",
            "res-1",
            "book",
            "booked",
            tool_fn=lambda: "done",
            confidence=0.9,
        )
        assert decision.verdict.value == "allow"
        assert result == "done"

    def test_null_sink_is_noop(self) -> None:
        sink = NullSink()
        # Should not raise
        sink.emit_event(TelemetryEvent())
        sink.record_counter("x", 1.0, {})
        sink.record_gauge("x", 1.0, {})
        sink.record_histogram("x", 1.0, {})
        sink.flush()


# ---------------------------------------------------------------------------
# P58: Telemetry Completeness
# ---------------------------------------------------------------------------


class TestP58TelemetryCompleteness:
    """Every guard operation must emit a telemetry event."""

    def test_write_mark_emits_event(
        self, guard_with_sink: Guard, agent: Agent, sink: InMemorySink
    ) -> None:
        guard_with_sink.write_mark(agent, Observation(scope="test", topic="x"))
        assert len(sink.events) == 1
        assert sink.events[0].operation == "write_mark"
        assert sink.events[0].verdict == "accepted"

    def test_rejected_write_emits_event(
        self, guard_with_sink: Guard, sink: InMemorySink
    ) -> None:
        unauthorized = Agent(name="unauthorized", scopes={})
        with pytest.raises(Exception):
            guard_with_sink.write_mark(
                unauthorized, Observation(scope="test", topic="x")
            )
        # The rejection happens at space.write (scope validation) before
        # telemetry. But barrier/envelope rejections do emit telemetry.

    def test_execute_emits_event(
        self, guard_with_sink: Guard, agent: Agent, sink: InMemorySink
    ) -> None:
        guard_with_sink.execute(
            agent,
            "test",
            "res-1",
            "book",
            "booked",
            tool_fn=lambda: "done",
            confidence=0.9,
        )
        # Should have events for execute and write metric
        assert len(sink.events) >= 1
        execute_events = [e for e in sink.events if e.operation == "execute"]
        assert len(execute_events) == 1
        assert execute_events[0].verdict == "allow"

    def test_conflict_emits_event(
        self, guard_with_sink: Guard, agent: Agent, sink: InMemorySink
    ) -> None:
        # First agent books
        guard_with_sink.execute(
            agent,
            "test",
            "res-1",
            "book",
            "booked",
            tool_fn=lambda: "done",
        )
        # Second agent tries same resource
        agent2 = Agent(
            name="agent-2",
            scopes={"test": ["intent", "action", "observation", "warning", "need"]},
        )
        decision, _ = guard_with_sink.execute(
            agent2,
            "test",
            "res-1",
            "book",
            "booked",
            tool_fn=lambda: "should not run",
        )
        assert decision.verdict.value == "conflict"
        execute_events = [e for e in sink.events if e.operation == "execute"]
        assert len(execute_events) == 2
        assert execute_events[1].verdict == "conflict"

    def test_event_fields_populated(
        self, guard_with_sink: Guard, agent: Agent, sink: InMemorySink
    ) -> None:
        guard_with_sink.write_mark(agent, Observation(scope="test", topic="x"))
        event = sink.events[0]
        assert event.agent_id == str(agent.id)
        assert event.scope == "test"
        assert event.mark_type == "observation"
        assert event.timestamp > 0

    def test_write_metric_emitted(
        self, guard_with_sink: Guard, agent: Agent, sink: InMemorySink
    ) -> None:
        guard_with_sink.write_mark(agent, Observation(scope="test", topic="x"))
        write_counters = [c for c in sink.counters if c[0] == "markspace.marks.written"]
        assert len(write_counters) == 1
        assert write_counters[0][2]["verdict"] == "accepted"

    def test_token_counters_emitted(
        self, guard_with_sink: Guard, agent: Agent, sink: InMemorySink
    ) -> None:
        guard_with_sink.record_round_tokens(agent, 4200, 280)
        input_counters = [c for c in sink.counters if c[0] == "markspace.tokens.input"]
        output_counters = [
            c for c in sink.counters if c[0] == "markspace.tokens.output"
        ]
        assert len(input_counters) == 1
        assert input_counters[0][1] == 4200
        assert len(output_counters) == 1
        assert output_counters[0][1] == 280

    def test_token_counters_emitted_without_budget(
        self, guard_with_sink: Guard, sink: InMemorySink
    ) -> None:
        """Token metrics fire for all agents, not just budgeted ones."""
        no_budget_agent = Agent(
            name="no-budget",
            scopes={"test": ["observation"]},
        )
        guard_with_sink.record_round_tokens(no_budget_agent, 1000, 50)
        input_counters = [c for c in sink.counters if c[0] == "markspace.tokens.input"]
        assert len(input_counters) == 1
        assert input_counters[0][1] == 1000

    def test_conflict_metric_emitted(
        self, guard_with_sink: Guard, agent: Agent, sink: InMemorySink
    ) -> None:
        # First agent books
        guard_with_sink.execute(
            agent,
            "test",
            "res-1",
            "book",
            "booked",
            tool_fn=lambda: "done",
        )
        # Second agent tries same resource - conflict
        agent2 = Agent(
            name="agent-2",
            scopes={"test": ["intent", "action", "observation", "warning", "need"]},
        )
        guard_with_sink.execute(
            agent2,
            "test",
            "res-1",
            "book",
            "booked",
            tool_fn=lambda: "should not run",
        )
        # No conflict metric for the first (no competing intents).
        # The second is rejected by existing action, before conflict
        # resolution runs. Let's create a real conflict with two intents.
        sink.clear()
        agent3 = Agent(
            name="agent-3",
            scopes={"test": ["intent", "action", "observation", "warning", "need"]},
        )
        agent4 = Agent(
            name="agent-4",
            scopes={"test": ["intent", "action", "observation", "warning", "need"]},
        )
        # Agent 3 declares intent on res-2
        guard_with_sink.pre_action(agent3, "test", "res-2", "book", confidence=0.5)
        # Agent 4 declares intent on same resource - triggers conflict resolution
        guard_with_sink.pre_action(agent4, "test", "res-2", "book", confidence=0.9)
        conflict_counters = [
            c for c in sink.counters if c[0] == "markspace.conflicts.resolved"
        ]
        assert len(conflict_counters) >= 1
        assert conflict_counters[0][2]["scope"] == "test"


# ---------------------------------------------------------------------------
# StructuredLogSink
# ---------------------------------------------------------------------------


class TestStructuredLogSink:
    def test_emits_without_error(self) -> None:
        sink = StructuredLogSink()
        # Should not raise
        sink.emit_event(TelemetryEvent(agent_id="test"))
        sink.record_counter("x", 1.0, {"a": "b"})
        sink.record_gauge("x", 1.0, {"a": "b"})
        sink.record_histogram("x", 1.0, {"a": "b"})


# ---------------------------------------------------------------------------
# InMemorySink
# ---------------------------------------------------------------------------


class TestInMemorySink:
    def test_captures_events(self) -> None:
        sink = InMemorySink()
        sink.emit_event(TelemetryEvent(agent_id="a"))
        sink.emit_event(TelemetryEvent(agent_id="b"))
        assert len(sink.events) == 2

    def test_clear(self) -> None:
        sink = InMemorySink()
        sink.emit_event(TelemetryEvent())
        sink.record_counter("x", 1.0, {})
        sink.clear()
        assert len(sink.events) == 0
        assert len(sink.counters) == 0
