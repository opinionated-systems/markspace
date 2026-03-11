# -*- coding: utf-8 -*-
"""
Stigmergic Coordination Protocol - Composition Property Tests

Tests for P48-P55: Agent manifests, watch/subscribe, composition validation.
"""

from __future__ import annotations

import copy
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.strategies import composite

from markspace import (
    Agent,
    AgentManifest,
    ConflictPolicy,
    DecayConfig,
    Intent,
    MarkSpace,
    MarkType,
    Observation,
    Scope,
    Source,
    Warning,
    WatchPattern,
    hours,
    minutes,
    validate_manifest_permissions,
    validate_pipeline,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def decay() -> DecayConfig:
    return DecayConfig(
        observation_half_life=hours(6),
        warning_half_life=hours(2),
        intent_ttl=hours(2),
    )


@pytest.fixture
def scope_a(decay: DecayConfig) -> Scope:
    return Scope(
        name="sensors",
        observation_topics=("reading", "status"),
        warning_topics=("*",),
        decay=decay,
        conflict_policy=ConflictPolicy.FIRST_WRITER,
    )


@pytest.fixture
def scope_b(decay: DecayConfig) -> Scope:
    return Scope(
        name="filtered",
        observation_topics=("reading",),
        warning_topics=("*",),
        decay=decay,
        conflict_policy=ConflictPolicy.FIRST_WRITER,
    )


@pytest.fixture
def scope_c(decay: DecayConfig) -> Scope:
    return Scope(
        name="alerts",
        warning_topics=("*",),
        allowed_intent_verbs=("respond",),
        allowed_action_verbs=("responded",),
        decay=decay,
        conflict_policy=ConflictPolicy.FIRST_WRITER,
    )


@pytest.fixture
def space(scope_a: Scope, scope_b: Scope, scope_c: Scope) -> MarkSpace:
    s = MarkSpace(scopes=[scope_a, scope_b, scope_c])
    s.set_clock(1_000_000.0)
    return s


@pytest.fixture
def writer() -> Agent:
    return Agent(
        name="sensor-1",
        scopes={"sensors": ["observation"]},
    )


@pytest.fixture
def subscriber() -> Agent:
    return Agent(
        name="filter-1",
        scopes={"filtered": ["observation"]},
        read_scopes=frozenset({"sensors"}),
    )


@pytest.fixture
def other_writer() -> Agent:
    return Agent(
        name="sensor-2",
        scopes={"sensors": ["observation"]},
    )


# ---------------------------------------------------------------------------
# P48: Subscription Idempotency
# ---------------------------------------------------------------------------


class TestP40SubscriptionIdempotency:
    """Re-subscribing replaces patterns, never duplicates."""

    def test_resubscribe_replaces_patterns(
        self, space: MarkSpace, subscriber: Agent, writer: Agent
    ) -> None:
        """Re-subscribing replaces patterns; old patterns stop matching."""
        # Subscribe to topic "reading"
        space.subscribe(
            subscriber,
            [WatchPattern(scope="sensors", topic="reading")],
        )
        # Write a matching mark
        space.write(
            writer,
            Observation(scope="sensors", topic="reading", content="v1"),
        )
        marks = space.get_watched_marks(subscriber)
        assert len(marks) == 1

        # Re-subscribe to a different topic
        space.subscribe(
            subscriber,
            [WatchPattern(scope="sensors", topic="status")],
        )
        # Write to old topic - should NOT be delivered
        space.write(
            writer,
            Observation(scope="sensors", topic="reading", content="v2"),
        )
        marks = space.get_watched_marks(subscriber)
        assert len(marks) == 0

        # Write to new topic - should be delivered
        space.write(
            writer,
            Observation(scope="sensors", topic="status", content="ok"),
        )
        marks = space.get_watched_marks(subscriber)
        assert len(marks) == 1
        assert isinstance(marks[0], Observation)
        assert marks[0].topic == "status"

    def test_resubscribe_no_duplicates(
        self, space: MarkSpace, subscriber: Agent, writer: Agent
    ) -> None:
        """Re-subscribing does not cause duplicate deliveries."""
        pattern = [WatchPattern(scope="sensors", topic="reading")]
        space.subscribe(subscriber, pattern)
        space.subscribe(subscriber, pattern)  # re-subscribe same pattern

        space.write(
            writer,
            Observation(scope="sensors", topic="reading", content="v1"),
        )
        marks = space.get_watched_marks(subscriber)
        assert len(marks) == 1  # exactly one, not two


# ---------------------------------------------------------------------------
# P49: Subscription Prospective
# ---------------------------------------------------------------------------


class TestP41SubscriptionProspective:
    """Existing marks are not retroactively delivered."""

    def test_existing_marks_not_delivered(
        self, space: MarkSpace, subscriber: Agent, writer: Agent
    ) -> None:
        # Write marks BEFORE subscribing
        space.write(
            writer,
            Observation(scope="sensors", topic="reading", content="old"),
        )

        # Now subscribe
        space.subscribe(
            subscriber,
            [WatchPattern(scope="sensors", topic="reading")],
        )

        # No retroactive delivery
        marks = space.get_watched_marks(subscriber)
        assert len(marks) == 0

        # New write IS delivered
        space.write(
            writer,
            Observation(scope="sensors", topic="reading", content="new"),
        )
        marks = space.get_watched_marks(subscriber)
        assert len(marks) == 1
        assert isinstance(marks[0], Observation)
        assert marks[0].content == "new"


# ---------------------------------------------------------------------------
# P50: Watch Subset
# ---------------------------------------------------------------------------


class TestP42WatchSubset:
    """Only marks matching the pattern are delivered."""

    def test_scope_filter(
        self, space: MarkSpace, subscriber: Agent, writer: Agent
    ) -> None:
        """Only marks in the subscribed scope are delivered."""
        space.subscribe(
            subscriber,
            [WatchPattern(scope="filtered")],  # watch scope "filtered"
        )

        # Write to "sensors" - should NOT be delivered
        space.write(
            writer,
            Observation(scope="sensors", topic="reading", content="v1"),
        )
        marks = space.get_watched_marks(subscriber)
        assert len(marks) == 0

    def test_mark_type_filter(
        self, space: MarkSpace, subscriber: Agent, writer: Agent
    ) -> None:
        """Pattern filters by mark type."""
        space.subscribe(
            subscriber,
            [WatchPattern(scope="sensors", mark_type=MarkType.WARNING)],
        )

        # Observation - no match
        space.write(
            writer,
            Observation(scope="sensors", topic="reading", content="v1"),
        )
        assert len(space.get_watched_marks(subscriber)) == 0

    def test_topic_filter(
        self, space: MarkSpace, subscriber: Agent, writer: Agent
    ) -> None:
        """Pattern filters by topic."""
        space.subscribe(
            subscriber,
            [WatchPattern(scope="sensors", topic="status")],
        )

        # Wrong topic
        space.write(
            writer,
            Observation(scope="sensors", topic="reading", content="v1"),
        )
        assert len(space.get_watched_marks(subscriber)) == 0

        # Right topic
        space.write(
            writer,
            Observation(scope="sensors", topic="status", content="ok"),
        )
        assert len(space.get_watched_marks(subscriber)) == 1

    def test_resource_filter(self, space: MarkSpace, subscriber: Agent) -> None:
        """Pattern filters by resource."""
        intent_writer = Agent(
            name="actor",
            scopes={"alerts": ["intent"]},
        )
        space.subscribe(
            subscriber,
            [WatchPattern(scope="alerts", resource="zone-1")],
        )

        # Wrong resource
        space.write(
            intent_writer,
            Intent(scope="alerts", resource="zone-2", action="respond"),
        )
        assert len(space.get_watched_marks(subscriber)) == 0

        # Right resource
        space.write(
            intent_writer,
            Intent(scope="alerts", resource="zone-1", action="respond"),
        )
        assert len(space.get_watched_marks(subscriber)) == 1

    def test_hierarchical_scope_matching(
        self, space: MarkSpace, subscriber: Agent
    ) -> None:
        """Subscribing to parent scope matches child scope marks."""
        child_scope = Scope(
            name="sensors/temperature",
            observation_topics=("*",),
            decay=DecayConfig(
                observation_half_life=hours(6),
                warning_half_life=hours(2),
                intent_ttl=hours(2),
            ),
            conflict_policy=ConflictPolicy.FIRST_WRITER,
        )
        space.register_scope(child_scope)

        child_writer = Agent(
            name="temp-sensor",
            scopes={"sensors": ["observation"]},
        )

        # Subscribe to parent scope
        space.subscribe(
            subscriber,
            [WatchPattern(scope="sensors")],
        )

        # Write to child scope - should match
        space.write(
            child_writer,
            Observation(scope="sensors/temperature", topic="reading", content="22C"),
        )
        marks = space.get_watched_marks(subscriber)
        assert len(marks) == 1

    def test_no_self_notification(self, space: MarkSpace, writer: Agent) -> None:
        """Agents are not notified about their own writes."""
        space.subscribe(
            writer,
            [WatchPattern(scope="sensors")],
        )
        space.write(
            writer,
            Observation(scope="sensors", topic="reading", content="v1"),
        )
        marks = space.get_watched_marks(writer)
        assert len(marks) == 0


# ---------------------------------------------------------------------------
# P51: At-Most-Once Delivery
# ---------------------------------------------------------------------------


class TestP43AtMostOnce:
    """Each mark is delivered at most once per agent per poll."""

    def test_clear_prevents_redelivery(
        self, space: MarkSpace, subscriber: Agent, writer: Agent
    ) -> None:
        space.subscribe(
            subscriber,
            [WatchPattern(scope="sensors")],
        )
        space.write(
            writer,
            Observation(scope="sensors", topic="reading", content="v1"),
        )

        marks_first = space.get_watched_marks(subscriber, clear=True)
        assert len(marks_first) == 1

        marks_second = space.get_watched_marks(subscriber, clear=True)
        assert len(marks_second) == 0

    def test_no_clear_allows_reread(
        self, space: MarkSpace, subscriber: Agent, writer: Agent
    ) -> None:
        space.subscribe(
            subscriber,
            [WatchPattern(scope="sensors")],
        )
        space.write(
            writer,
            Observation(scope="sensors", topic="reading", content="v1"),
        )

        marks_first = space.get_watched_marks(subscriber, clear=False)
        assert len(marks_first) == 1

        marks_second = space.get_watched_marks(subscriber, clear=False)
        assert len(marks_second) == 1


# ---------------------------------------------------------------------------
# P52: Write-Order Delivery
# ---------------------------------------------------------------------------


class TestP44WriteOrder:
    """Marks are returned in the order they were written."""

    def test_marks_in_write_order(
        self, space: MarkSpace, subscriber: Agent, writer: Agent
    ) -> None:
        space.subscribe(
            subscriber,
            [WatchPattern(scope="sensors")],
        )

        contents = [f"reading-{i}" for i in range(10)]
        for c in contents:
            space.write(
                writer,
                Observation(scope="sensors", topic="reading", content=c),
            )

        marks = space.get_watched_marks(subscriber)
        assert len(marks) == 10
        assert all(isinstance(m, Observation) for m in marks)
        assert [m.content for m in marks] == contents  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# P53: Pipeline Structural Validation
# ---------------------------------------------------------------------------


class TestP45PipelineValidation:
    """Pipeline validation depends only on manifests, not runtime state."""

    def test_valid_pipeline(self) -> None:
        sensor = Agent(
            name="sensor",
            scopes={"sensors": ["observation"]},
            manifest=AgentManifest(
                inputs=(),
                outputs=(("sensors", MarkType.OBSERVATION),),
            ),
        )
        filt = Agent(
            name="filter",
            scopes={"filtered": ["observation"]},
            manifest=AgentManifest(
                inputs=(WatchPattern(scope="sensors", mark_type=MarkType.OBSERVATION),),
                outputs=(("filtered", MarkType.OBSERVATION),),
            ),
        )
        alerter = Agent(
            name="alerter",
            scopes={"alerts": ["warning"]},
            manifest=AgentManifest(
                inputs=(
                    WatchPattern(scope="filtered", mark_type=MarkType.OBSERVATION),
                ),
                outputs=(("alerts", MarkType.WARNING),),
            ),
        )
        errors = validate_pipeline([sensor, filt, alerter])
        assert errors == []

    def test_disconnected_pipeline(self) -> None:
        sensor = Agent(
            name="sensor",
            scopes={"sensors": ["observation"]},
            manifest=AgentManifest(
                inputs=(),
                outputs=(("sensors", MarkType.OBSERVATION),),
            ),
        )
        # Alerter reads from "alerts", but sensor writes to "sensors"
        alerter = Agent(
            name="alerter",
            scopes={"alerts": ["warning"]},
            manifest=AgentManifest(
                inputs=(WatchPattern(scope="alerts", mark_type=MarkType.WARNING),),
                outputs=(("alerts", MarkType.WARNING),),
            ),
        )
        errors = validate_pipeline([sensor, alerter])
        assert len(errors) == 1
        assert "No connection" in errors[0]

    def test_missing_manifest(self) -> None:
        agent_no_manifest = Agent(name="bare", scopes={})
        agent_with = Agent(
            name="filter",
            scopes={"filtered": ["observation"]},
            manifest=AgentManifest(
                inputs=(WatchPattern(scope="sensors"),),
                outputs=(("filtered", MarkType.OBSERVATION),),
            ),
        )
        errors = validate_pipeline([agent_no_manifest, agent_with])
        assert len(errors) == 1
        assert "no manifest" in errors[0]

    def test_wildcard_mark_type_matches(self) -> None:
        """Consumer input with mark_type=None matches any producer output."""
        producer = Agent(
            name="producer",
            scopes={"data": ["observation"]},
            manifest=AgentManifest(
                outputs=(("data", MarkType.OBSERVATION),),
            ),
        )
        consumer = Agent(
            name="consumer",
            scopes={"processed": ["observation"]},
            manifest=AgentManifest(
                inputs=(WatchPattern(scope="data"),),  # no mark_type filter
                outputs=(("processed", MarkType.OBSERVATION),),
            ),
        )
        errors = validate_pipeline([producer, consumer])
        assert errors == []


# ---------------------------------------------------------------------------
# P54: Manifest-Permission Consistency
# ---------------------------------------------------------------------------


class TestP46ManifestPermissions:
    """Manifest outputs must be a subset of agent's write permissions."""

    def test_valid_manifest(self) -> None:
        agent = Agent(
            name="sensor",
            scopes={"sensors": ["observation"]},
            manifest=AgentManifest(
                outputs=(("sensors", MarkType.OBSERVATION),),
            ),
        )
        errors = validate_manifest_permissions(agent)
        assert errors == []

    def test_invalid_manifest(self) -> None:
        agent = Agent(
            name="sensor",
            scopes={"sensors": ["observation"]},
            manifest=AgentManifest(
                # Declares warning output but has no warning write permission
                outputs=(("sensors", MarkType.WARNING),),
            ),
        )
        errors = validate_manifest_permissions(agent)
        assert len(errors) == 1
        assert "lacks write permission" in errors[0]

    def test_no_manifest_no_errors(self) -> None:
        agent = Agent(name="bare", scopes={})
        errors = validate_manifest_permissions(agent)
        assert errors == []

    def test_hierarchical_scope_valid(self) -> None:
        """Permission for parent scope covers child scope in manifest."""
        agent = Agent(
            name="sensor",
            scopes={"sensors": ["observation"]},
            manifest=AgentManifest(
                outputs=(("sensors/temperature", MarkType.OBSERVATION),),
            ),
        )
        errors = validate_manifest_permissions(agent)
        assert errors == []


# ---------------------------------------------------------------------------
# P55: Pattern Match Purity
# ---------------------------------------------------------------------------


class TestP47PatternMatchPurity:
    """WatchPattern.matches() is a pure function with no side effects."""

    def test_matches_does_not_mutate_mark(self) -> None:
        pattern = WatchPattern(scope="sensors", topic="reading")
        mark = Observation(
            scope="sensors", topic="reading", content="v1", confidence=0.9
        )
        before = mark.model_dump()
        pattern.matches(mark)
        assert mark.model_dump() == before

    def test_matches_is_deterministic(self) -> None:
        pattern = WatchPattern(scope="sensors", topic="reading")
        mark = Observation(scope="sensors", topic="reading", content="v1")
        r1 = pattern.matches(mark)
        r2 = pattern.matches(mark)
        assert r1 == r2 == True

    def test_no_match_is_deterministic(self) -> None:
        pattern = WatchPattern(scope="sensors", topic="status")
        mark = Observation(scope="sensors", topic="reading", content="v1")
        r1 = pattern.matches(mark)
        r2 = pattern.matches(mark)
        assert r1 == r2 == False


# ---------------------------------------------------------------------------
# Hypothesis property-based tests
# ---------------------------------------------------------------------------


@composite
def st_watch_pattern(draw: st.DrawFn) -> WatchPattern:
    scope = draw(st.sampled_from(["sensors", "filtered", "alerts"]))
    mark_type = draw(st.one_of(st.none(), st.sampled_from(list(MarkType))))
    topic = draw(st.one_of(st.none(), st.sampled_from(["reading", "status", "alert"])))
    resource = draw(st.one_of(st.none(), st.sampled_from(["zone-1", "zone-2"])))
    return WatchPattern(
        scope=scope, mark_type=mark_type, topic=topic, resource=resource
    )


class TestWatchPatternHypothesis:
    @given(pattern=st_watch_pattern())
    @settings(max_examples=100)
    def test_matches_is_deterministic(self, pattern: WatchPattern) -> None:
        mark = Observation(scope="sensors", topic="reading", content="v")
        r1 = pattern.matches(mark)
        r2 = pattern.matches(mark)
        assert r1 == r2

    @given(pattern=st_watch_pattern())
    @settings(max_examples=100)
    def test_matches_does_not_mutate(self, pattern: WatchPattern) -> None:
        mark = Observation(scope="sensors", topic="reading", content="v")
        before = mark.model_dump()
        pattern.matches(mark)
        assert mark.model_dump() == before


# ---------------------------------------------------------------------------
# Concurrent subscription tests
# ---------------------------------------------------------------------------


class TestConcurrentSubscription:
    """Thread safety of subscribe/write/poll interactions."""

    def test_concurrent_write_notify(self) -> None:
        """Multiple writers, one subscriber. All matching marks delivered."""
        decay = DecayConfig(
            observation_half_life=hours(6),
            warning_half_life=hours(2),
            intent_ttl=hours(2),
        )
        scope = Scope(
            name="data",
            observation_topics=("*",),
            decay=decay,
            conflict_policy=ConflictPolicy.FIRST_WRITER,
        )
        space = MarkSpace(scopes=[scope])
        space.set_clock(1_000_000.0)

        n_writers = 20
        writers = [
            Agent(name=f"writer-{i}", scopes={"data": ["observation"]})
            for i in range(n_writers)
        ]
        sub = Agent(name="subscriber", scopes={})
        space.subscribe(sub, [WatchPattern(scope="data")])

        def write_mark(agent: Agent, idx: int) -> None:
            space.write(
                agent,
                Observation(scope="data", topic="reading", content=f"v{idx}"),
            )

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [
                executor.submit(write_mark, writers[i], i) for i in range(n_writers)
            ]
            for f in as_completed(futures):
                f.result()

        marks = space.get_watched_marks(sub)
        assert len(marks) == n_writers

    def test_concurrent_subscribe_and_write(self) -> None:
        """Subscribing and writing concurrently. No crashes."""
        decay = DecayConfig(
            observation_half_life=hours(6),
            warning_half_life=hours(2),
            intent_ttl=hours(2),
        )
        scope = Scope(
            name="data",
            observation_topics=("*",),
            decay=decay,
            conflict_policy=ConflictPolicy.FIRST_WRITER,
        )
        space = MarkSpace(scopes=[scope])
        space.set_clock(1_000_000.0)

        writer = Agent(name="writer", scopes={"data": ["observation"]})
        subscribers = [Agent(name=f"sub-{i}", scopes={}) for i in range(10)]

        def subscribe_agent(agent: Agent) -> None:
            space.subscribe(agent, [WatchPattern(scope="data")])

        def write_marks() -> None:
            for i in range(20):
                space.write(
                    writer,
                    Observation(scope="data", topic="reading", content=f"v{i}"),
                )

        with ThreadPoolExecutor(max_workers=11) as executor:
            futures = [executor.submit(subscribe_agent, s) for s in subscribers]
            futures.append(executor.submit(write_marks))
            for f in as_completed(futures):
                f.result()

        # No assertion on exact counts (race between subscribe and write),
        # but all subscribers should have some marks or zero (no crashes)
        for sub in subscribers:
            marks = space.get_watched_marks(sub)
            assert isinstance(marks, list)

    def test_fan_in_concurrent(self) -> None:
        """Multiple writers feeding one subscriber, all marks accounted for."""
        decay = DecayConfig(
            observation_half_life=hours(6),
            warning_half_life=hours(2),
            intent_ttl=hours(2),
        )
        scope = Scope(
            name="data",
            observation_topics=("*",),
            decay=decay,
            conflict_policy=ConflictPolicy.FIRST_WRITER,
        )
        space = MarkSpace(scopes=[scope])
        space.set_clock(1_000_000.0)

        n_writers = 50
        marks_per_writer = 5
        writers = [
            Agent(name=f"w-{i}", scopes={"data": ["observation"]})
            for i in range(n_writers)
        ]
        sub = Agent(name="consumer", scopes={})
        space.subscribe(sub, [WatchPattern(scope="data")])

        def write_batch(agent: Agent, base: int) -> None:
            for j in range(marks_per_writer):
                space.write(
                    agent,
                    Observation(scope="data", topic="reading", content=f"{base}-{j}"),
                )

        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = [
                executor.submit(write_batch, writers[i], i) for i in range(n_writers)
            ]
            for f in as_completed(futures):
                f.result()

        marks = space.get_watched_marks(sub)
        assert len(marks) == n_writers * marks_per_writer


# ---------------------------------------------------------------------------
# Unsubscribe tests
# ---------------------------------------------------------------------------


class TestUnsubscribe:
    """Unsubscribing stops all deliveries."""

    def test_unsubscribe_stops_delivery(
        self, space: MarkSpace, subscriber: Agent, writer: Agent
    ) -> None:
        space.subscribe(subscriber, [WatchPattern(scope="sensors")])
        space.unsubscribe(subscriber)

        space.write(
            writer,
            Observation(scope="sensors", topic="reading", content="v1"),
        )
        marks = space.get_watched_marks(subscriber)
        assert len(marks) == 0

    def test_unsubscribe_clears_pending(
        self, space: MarkSpace, subscriber: Agent, writer: Agent
    ) -> None:
        space.subscribe(subscriber, [WatchPattern(scope="sensors")])
        space.write(
            writer,
            Observation(scope="sensors", topic="reading", content="v1"),
        )

        # Unsubscribe clears pending too
        space.unsubscribe(subscriber)
        marks = space.get_watched_marks(subscriber)
        assert len(marks) == 0
