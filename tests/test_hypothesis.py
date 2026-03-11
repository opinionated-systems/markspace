# -*- coding: utf-8 -*-
"""
Hypothesis property-based tests for the markspace protocol.

These complement the deterministic tests in test_properties.py and test_guard.py
by exercising the same mathematical invariants across thousands of randomized
inputs - catching edge cases around floating-point boundaries, extreme time
values, and unusual parameter combinations.
"""

from __future__ import annotations

import uuid

from hypothesis import assume, given, settings
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule
from hypothesis.strategies import (
    composite,
    floats,
    integers,
    just,
    lists,
    one_of,
    sampled_from,
)

from markspace.core import (
    Action,
    Agent,
    AnyMark,
    ConflictPolicy,
    DecayConfig,
    Intent,
    MarkType,
    Need,
    Observation,
    REINFORCEMENT_CAP,
    Scope,
    Severity,
    Source,
    Warning,
    compute_strength,
    effective_strength,
    effective_strength_with_warnings,
    project_mark,
    reinforce,
    resolve_conflict,
)
from markspace.space import MarkSpace

# ---------------------------------------------------------------------------
# Section A: Custom Strategies
# ---------------------------------------------------------------------------

st_source = sampled_from(list(Source))
st_conflict_policy = sampled_from(list(ConflictPolicy))
st_severity = sampled_from(list(Severity))
st_confidence = floats(
    min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False
)
st_strength = floats(
    min_value=0.0, max_value=10.0, allow_nan=False, allow_infinity=False
)
st_positive_strength = floats(
    min_value=0.01, max_value=5.0, allow_nan=False, allow_infinity=False
)
st_time = floats(min_value=0.0, max_value=1e9, allow_nan=False, allow_infinity=False)


@composite
def st_decay_config(draw):
    """Generate a valid DecayConfig with positive half-lives and TTL."""
    return DecayConfig(
        observation_half_life=draw(
            floats(min_value=1.0, max_value=1e6, allow_nan=False, allow_infinity=False)
        ),
        warning_half_life=draw(
            floats(min_value=1.0, max_value=1e6, allow_nan=False, allow_infinity=False)
        ),
        intent_ttl=draw(
            floats(min_value=1.0, max_value=1e6, allow_nan=False, allow_infinity=False)
        ),
    )


@composite
def st_observation(draw, created_at=None):
    return Observation(
        scope="test",
        topic="topic",
        content="data",
        confidence=draw(st_confidence),
        source=draw(st_source),
        initial_strength=draw(st_strength),
        created_at=created_at if created_at is not None else draw(st_time),
    )


@composite
def st_warning(draw, created_at=None):
    return Warning(
        scope="test",
        topic="topic",
        reason="reason",
        severity=draw(st_severity),
        initial_strength=draw(st_strength),
        created_at=created_at if created_at is not None else draw(st_time),
    )


@composite
def st_intent(draw, created_at=None):
    return Intent(
        scope="test",
        resource="resource",
        action="act",
        confidence=draw(st_confidence),
        initial_strength=draw(st_strength),
        created_at=created_at if created_at is not None else draw(st_time),
    )


@composite
def st_action(draw, created_at=None):
    return Action(
        scope="test",
        resource="resource",
        action="acted",
        result="ok",
        initial_strength=draw(st_strength),
        created_at=created_at if created_at is not None else draw(st_time),
    )


@composite
def st_need(draw, created_at=None):
    return Need(
        scope="test",
        question="what?",
        priority=draw(st_confidence),
        initial_strength=draw(st_strength),
        created_at=created_at if created_at is not None else draw(st_time),
        resolved_by=draw(one_of(just(None), just(uuid.uuid4()))),
    )


st_any_mark = one_of(
    st_observation(), st_warning(), st_intent(), st_action(), st_need()
)


# ---------------------------------------------------------------------------
# Section B: Pure Function Property Tests
# ---------------------------------------------------------------------------


class TestComputeStrengthBounds:
    """compute_strength output is bounded: 0 <= result <= initial_strength."""

    @given(mark=st_observation(), config=st_decay_config(), now=st_time)
    def test_observation_bounded(self, mark, config, now):
        assume(now >= mark.created_at)
        s = compute_strength(mark, now, config)
        assert 0.0 <= s <= mark.initial_strength + 1e-10

    @given(mark=st_warning(), config=st_decay_config(), now=st_time)
    def test_warning_bounded(self, mark, config, now):
        assume(now >= mark.created_at)
        s = compute_strength(mark, now, config)
        assert 0.0 <= s <= mark.initial_strength + 1e-10

    @given(mark=st_intent(), config=st_decay_config(), now=st_time)
    def test_intent_bounded(self, mark, config, now):
        assume(now >= mark.created_at)
        s = compute_strength(mark, now, config)
        assert 0.0 <= s <= mark.initial_strength + 1e-10

    @given(mark=st_action(), config=st_decay_config(), now=st_time)
    def test_action_bounded(self, mark, config, now):
        assume(now >= mark.created_at)
        s = compute_strength(mark, now, config)
        assert 0.0 <= s <= mark.initial_strength + 1e-10

    @given(mark=st_need(), config=st_decay_config(), now=st_time)
    def test_need_bounded(self, mark, config, now):
        assume(now >= mark.created_at)
        s = compute_strength(mark, now, config)
        assert 0.0 <= s <= mark.initial_strength + 1e-10


class TestDecayMonotonicity:
    """P1: For observation/warning marks, strength(t1) >= strength(t2) when t1 < t2."""

    @given(
        mark=st_observation(),
        config=st_decay_config(),
        t1=st_time,
        delta=floats(
            min_value=0.0, max_value=1e8, allow_nan=False, allow_infinity=False
        ),
    )
    def test_observation_monotonic(self, mark, config, t1, delta):
        assume(t1 >= mark.created_at)
        t2 = t1 + delta
        s1 = compute_strength(mark, t1, config)
        s2 = compute_strength(mark, t2, config)
        assert s1 >= s2 - 1e-10

    @given(
        mark=st_warning(),
        config=st_decay_config(),
        t1=st_time,
        delta=floats(
            min_value=0.0, max_value=1e8, allow_nan=False, allow_infinity=False
        ),
    )
    def test_warning_monotonic(self, mark, config, t1, delta):
        assume(t1 >= mark.created_at)
        t2 = t1 + delta
        s1 = compute_strength(mark, t1, config)
        s2 = compute_strength(mark, t2, config)
        assert s1 >= s2 - 1e-10


class TestActionPermanence:
    """P2: Action strength is constant for all time."""

    @given(mark=st_action(), config=st_decay_config(), t1=st_time, t2=st_time)
    def test_action_constant(self, mark, config, t1, t2):
        assume(t1 >= mark.created_at and t2 >= mark.created_at)
        assert compute_strength(mark, t1, config) == compute_strength(mark, t2, config)


class TestIntentStepFunction:
    """P4: Intent is full strength before TTL, zero after."""

    @given(
        mark=st_intent(),
        config=st_decay_config(),
        age_fraction=floats(
            min_value=0.0, max_value=0.99, allow_nan=False, allow_infinity=False
        ),
    )
    def test_before_ttl(self, mark, config, age_fraction):
        now = mark.created_at + config.intent_ttl * age_fraction
        assert compute_strength(mark, now, config) == mark.initial_strength

    @given(
        mark=st_intent(),
        config=st_decay_config(),
        extra=floats(
            min_value=0.001, max_value=1e6, allow_nan=False, allow_infinity=False
        ),
    )
    def test_after_ttl(self, mark, config, extra):
        now = mark.created_at + config.intent_ttl + extra
        assert compute_strength(mark, now, config) == 0.0


class TestNeedResolutionBehavior:
    """P5: Need strength depends only on resolution status."""

    @given(config=st_decay_config(), now=st_time, strength=st_strength)
    def test_unresolved_full_strength(self, config, now, strength):
        mark = Need(
            scope="test", question="q", initial_strength=strength, created_at=0.0
        )
        assert compute_strength(mark, now, config) == strength

    @given(config=st_decay_config(), now=st_time, strength=st_strength)
    def test_resolved_zero(self, config, now, strength):
        mark = Need(
            scope="test",
            question="q",
            initial_strength=strength,
            created_at=0.0,
            resolved_by=uuid.uuid4(),
        )
        assert compute_strength(mark, now, config) == 0.0


class TestTrustOrdering:
    """P6: FLEET >= EXTERNAL_VERIFIED >= EXTERNAL_UNVERIFIED for all inputs."""

    @given(
        config=st_decay_config(),
        now=st_time,
        confidence=st_confidence,
        strength=st_strength,
    )
    def test_effective_strength_preserves_order(
        self, config, now, confidence, strength
    ):
        assume(now >= 0.0)
        base = dict(
            scope="test",
            topic="t",
            content="c",
            confidence=confidence,
            initial_strength=strength,
            created_at=0.0,
        )
        fleet = effective_strength(
            Observation(**base, source=Source.FLEET), now, config  # type: ignore[arg-type]
        )
        verified = effective_strength(
            Observation(**base, source=Source.EXTERNAL_VERIFIED), now, config  # type: ignore[arg-type]
        )
        unverified = effective_strength(
            Observation(**base, source=Source.EXTERNAL_UNVERIFIED), now, config  # type: ignore[arg-type]
        )
        assert fleet >= verified - 1e-10
        assert verified >= unverified - 1e-10


class TestEffectiveStrengthBounds:
    """P7: effective_strength <= initial_strength (trust weight <= 1.0)."""

    @given(mark=st_any_mark, config=st_decay_config(), now=st_time)
    def test_bounded_by_initial(self, mark, config, now):
        assume(now >= mark.created_at)
        s = effective_strength(mark, now, config)
        assert s <= mark.initial_strength + 1e-10


class TestReinforcementProperties:
    """P8-P10: Reinforcement is sublinear, bounded, and monotonically non-decreasing."""

    @given(strengths=lists(st_positive_strength, min_size=2, max_size=50))
    def test_sublinear(self, strengths):
        """P8: aggregate < N * max for N > 1."""
        agg = reinforce(strengths)
        assert agg < len(strengths) * max(strengths) + 1e-10

    @given(strengths=lists(st_strength, min_size=1, max_size=100))
    def test_bounded_by_cap(self, strengths):
        """P9: aggregate <= REINFORCEMENT_CAP."""
        assert reinforce(strengths) <= REINFORCEMENT_CAP + 1e-10

    @given(
        strengths=lists(st_strength, min_size=0, max_size=20),
        new_strength=st_strength,
    )
    def test_monotonic_addition(self, strengths, new_strength):
        """P10: Adding a non-negative strength cannot decrease aggregate."""
        base = reinforce(strengths)
        extended = reinforce(strengths + [new_strength])
        assert extended >= base - 1e-10

    @given(strengths=lists(st_strength, min_size=0, max_size=20))
    def test_non_negative(self, strengths):
        """Reinforcement result is always >= 0."""
        assert reinforce(strengths) >= 0.0

    @given(strengths=lists(st_positive_strength, min_size=1, max_size=20))
    def test_at_least_max(self, strengths):
        """Aggregate is at least as large as the strongest input (up to cap)."""
        agg = reinforce(strengths)
        # reinforce caps at REINFORCEMENT_CAP, so the property is:
        # agg >= min(max_individual, REINFORCEMENT_CAP)
        assert agg >= min(max(strengths), REINFORCEMENT_CAP) - 1e-10


class TestConflictResolutionProperties:
    """P11-P12: Conflict resolution is deterministic and guarantees progress."""

    @given(
        intents=lists(st_intent(), min_size=1, max_size=10),
        policy=sampled_from(
            [ConflictPolicy.HIGHEST_CONFIDENCE, ConflictPolicy.FIRST_WRITER]
        ),
    )
    def test_deterministic(self, intents, policy):
        """P11: Same inputs produce same winner, always."""
        # Ensure distinct created_at to avoid UUID-dependent tie-breaking
        intents = [intent.model_copy(update={"created_at": float(i * 100)}) for i, intent in enumerate(intents)]  # type: ignore[arg-type]
        result1 = resolve_conflict(intents, policy)
        result2 = resolve_conflict(intents, policy)
        assert result1 == result2

    @given(
        intents=lists(st_intent(), min_size=1, max_size=10),
        policy=sampled_from(
            [ConflictPolicy.HIGHEST_CONFIDENCE, ConflictPolicy.FIRST_WRITER]
        ),
    )
    def test_winner_is_from_input(self, intents, policy):
        """P12: Winner id belongs to one of the input intents."""
        intents = [intent.model_copy(update={"created_at": float(i * 100)}) for i, intent in enumerate(intents)]  # type: ignore[arg-type]
        winner = resolve_conflict(intents, policy)
        assert winner in {i.id for i in intents}

    @given(intents=lists(st_intent(), min_size=1, max_size=10))
    def test_yield_all_returns_none(self, intents):
        """YIELD_ALL always returns None regardless of input."""
        assert resolve_conflict(intents, ConflictPolicy.YIELD_ALL) is None

    @given(intents=lists(st_intent(), min_size=2, max_size=10))
    def test_highest_confidence_selects_max(self, intents):
        """HIGHEST_CONFIDENCE winner has the highest confidence."""
        intents = [intent.model_copy(update={"created_at": float(i * 100)}) for i, intent in enumerate(intents)]  # type: ignore[arg-type]
        winner_id = resolve_conflict(intents, ConflictPolicy.HIGHEST_CONFIDENCE)
        winner = next(i for i in intents if i.id == winner_id)
        max_conf = max(i.confidence for i in intents)
        assert winner.confidence == max_conf

    @given(intents=lists(st_intent(), min_size=2, max_size=10))
    def test_first_writer_selects_earliest(self, intents):
        """FIRST_WRITER winner has the earliest created_at."""
        intents = [intent.model_copy(update={"created_at": float(i * 100)}) for i, intent in enumerate(intents)]  # type: ignore[arg-type]
        winner_id = resolve_conflict(intents, ConflictPolicy.FIRST_WRITER)
        winner = next(i for i in intents if i.id == winner_id)
        min_time = min(i.created_at for i in intents)
        assert winner.created_at == min_time


class TestWarningInvalidationProperties:
    """P34-P35: Warning invalidation is floored at 0 and recovers as warnings decay."""

    @given(
        config=st_decay_config(),
        now=st_time,
        obs_strength=st_strength,
        n_warnings=integers(min_value=1, max_value=10),
        warning_strength=st_strength,
    )
    def test_floor_at_zero(
        self, config, now, obs_strength, n_warnings, warning_strength
    ):
        """P34: effective_strength_with_warnings >= 0, always."""
        obs = Observation(
            scope="test",
            topic="t",
            content="c",
            source=Source.FLEET,
            created_at=0.0,
            initial_strength=obs_strength,
        )
        warnings = [
            Warning(
                scope="test",
                topic="t",
                reason="r",
                invalidates=obs.id,
                created_at=0.0,
                initial_strength=warning_strength,
            )
            for _ in range(n_warnings)
        ]
        assume(now >= 0.0)
        result = effective_strength_with_warnings(obs, warnings, now, config)
        assert result >= 0.0

    @given(config=st_decay_config())
    def test_recovery_as_warnings_decay(self, config):
        """P35: As warning decays, invalidated mark's effective strength recovers."""
        # Need meaningfully different decay rates so the warning decays away
        # while the observation retains significant strength
        assume(config.warning_half_life < config.observation_half_life * 0.5)

        obs = Observation(
            scope="test",
            topic="t",
            content="c",
            source=Source.FLEET,
            created_at=0.0,
            initial_strength=1.0,
        )
        warning = Warning(
            scope="test",
            topic="t",
            reason="r",
            invalidates=obs.id,
            created_at=0.0,
            initial_strength=1.0,
        )

        # After many warning half-lives, the warning has decayed away
        # but the observation is still partially alive
        t_late = config.warning_half_life * 20
        base = effective_strength(obs, t_late, config)
        if base > 0.0:
            suppressed = effective_strength_with_warnings(
                obs, [warning], t_late, config
            )
            # Survival ratio should be near 1.0 since the warning has decayed
            assert suppressed / base > 0.99


class TestProjectionProperties:
    """P24: Projection preserves identity fields, sets projected flag, is idempotent."""

    @given(mark=st_any_mark)
    def test_projection_sets_projected_flag(self, mark):
        projected = project_mark(mark)
        assert projected.projected is True

    @given(mark=st_any_mark)
    def test_projection_preserves_identity(self, mark):
        projected = project_mark(mark)
        assert projected.id == mark.id
        assert projected.agent_id == mark.agent_id
        assert projected.scope == mark.scope
        assert projected.mark_type == mark.mark_type
        assert projected.created_at == mark.created_at
        assert projected.initial_strength == mark.initial_strength

    @given(mark=st_any_mark)
    def test_projection_does_not_mutate_original(self, mark):
        original_projected = mark.projected
        project_mark(mark)
        assert mark.projected == original_projected

    @given(mark=st_any_mark)
    def test_projection_idempotent(self, mark):
        """Projecting twice produces the same result as projecting once."""
        p1 = project_mark(mark)
        p2 = project_mark(p1)
        assert p1.model_dump() == p2.model_dump()


# ---------------------------------------------------------------------------
# Section C: Stateful Testing - RuleBasedStateMachine
# ---------------------------------------------------------------------------


class MarkSpaceStateMachine(RuleBasedStateMachine):
    """
    Stateful property test for MarkSpace.

    Executes random sequences of write/read/resolve/advance-clock operations
    and verifies invariants hold after every step.
    """

    def __init__(self):
        super().__init__()
        self.scope = Scope(
            name="test",
            allowed_intent_verbs=("act",),
            allowed_action_verbs=("acted",),
            observation_topics=("*",),
            warning_topics=("*",),
            decay=DecayConfig(
                observation_half_life=3600.0,
                warning_half_life=1800.0,
                intent_ttl=600.0,
            ),
            conflict_policy=ConflictPolicy.HIGHEST_CONFIDENCE,
        )
        self.space = MarkSpace(scopes=[self.scope], clock=1_000_000.0)
        self.agent = Agent(
            name="test-agent",
            scopes={"test": ["intent", "action", "observation", "warning", "need"]},
        )
        self.written_ids: list[uuid.UUID] = []
        self.written_types: dict[uuid.UUID, MarkType] = {}
        self.need_ids: list[uuid.UUID] = []
        self.resolved_needs: set[uuid.UUID] = set()

    @rule(confidence=st_confidence, strength=st_strength)
    def write_observation(self, confidence, strength):
        obs = Observation(
            scope="test",
            topic="topic",
            content="data",
            confidence=confidence,
            source=Source.FLEET,
            initial_strength=strength,
        )
        mid = self.space.write(self.agent, obs)
        self.written_ids.append(mid)
        self.written_types[mid] = MarkType.OBSERVATION

    @rule(confidence=st_confidence, strength=st_strength)
    def write_intent(self, confidence, strength):
        intent = Intent(
            scope="test",
            resource="resource",
            action="act",
            confidence=confidence,
            initial_strength=strength,
        )
        mid = self.space.write(self.agent, intent)
        self.written_ids.append(mid)
        self.written_types[mid] = MarkType.INTENT

    @rule(strength=st_strength)
    def write_action(self, strength):
        action = Action(
            scope="test",
            resource="resource",
            action="acted",
            result="ok",
            initial_strength=strength,
        )
        mid = self.space.write(self.agent, action)
        self.written_ids.append(mid)
        self.written_types[mid] = MarkType.ACTION

    @rule(strength=st_strength)
    def write_warning(self, strength):
        # Optionally invalidate the most recent observation
        invalidates = None
        obs_ids = [
            mid for mid, mt in self.written_types.items() if mt == MarkType.OBSERVATION
        ]
        if obs_ids:
            invalidates = obs_ids[-1]
        warning = Warning(
            scope="test",
            topic="topic",
            reason="reason",
            invalidates=invalidates,
            initial_strength=strength,
        )
        mid = self.space.write(self.agent, warning)
        self.written_ids.append(mid)
        self.written_types[mid] = MarkType.WARNING

    @rule(strength=st_strength, priority=st_confidence)
    def write_need(self, strength, priority):
        need = Need(
            scope="test",
            question="q",
            priority=priority,
            initial_strength=strength,
        )
        mid = self.space.write(self.agent, need)
        self.written_ids.append(mid)
        self.written_types[mid] = MarkType.NEED
        self.need_ids.append(mid)

    @rule()
    def resolve_a_need(self):
        unresolved = [nid for nid in self.need_ids if nid not in self.resolved_needs]
        if unresolved:
            nid = unresolved[0]
            # Write a real action mark to use as the resolving action
            from markspace import Action

            action_id = self.space.write(
                self.agent,
                Action(scope="test", resource="resolve", action="acted"),
            )
            self.space.resolve(nid, action_id)
            self.resolved_needs.add(nid)

    @rule(
        delta=floats(
            min_value=0.0, max_value=3600.0, allow_nan=False, allow_infinity=False
        )
    )
    def advance_clock(self, delta):
        current = self.space.now()
        self.space.set_clock(current + delta)

    @invariant()
    def read_never_raises(self):
        """Reads must never raise, regardless of state."""
        marks = self.space.read(scope="test", min_strength=0.0)
        assert isinstance(marks, list)

    @invariant()
    def all_strengths_non_negative(self):
        """All returned marks have non-negative effective strength."""
        marks = self.space.read(scope="test", min_strength=0.0)
        now = self.space.now()
        decay_config = self.scope.decay
        warnings = [m for m in marks if isinstance(m, Warning)]
        for m in marks:
            s = effective_strength_with_warnings(m, warnings, now, decay_config)
            assert s >= 0.0

    @invariant()
    def resolved_needs_invisible(self):
        """Resolved need marks should not appear in reads (strength = 0)."""
        needs = self.space.read(scope="test", mark_type=MarkType.NEED)
        for need in needs:
            assert need.id not in self.resolved_needs

    @invariant()
    def written_marks_exist_in_storage(self):
        """Every written mark still exists in storage (marks are never deleted)."""
        for mid in self.written_ids:
            assert self.space.get_mark(mid) is not None


TestMarkSpaceStateful = MarkSpaceStateMachine.TestCase
TestMarkSpaceStateful.settings = settings(max_examples=50, stateful_step_count=30)


# ---------------------------------------------------------------------------
# Section D: Scheduler Property Tests (P56)
# ---------------------------------------------------------------------------

from markspace.core import AgentManifest
from markspace.schedule import Scheduler

st_schedule_interval = floats(
    min_value=0.1, max_value=1e6, allow_nan=False, allow_infinity=False
)
st_invalid_interval = one_of(
    just(0.0),
    just(None),
    floats(min_value=-1e6, max_value=-0.01, allow_nan=False, allow_infinity=False),
)


@composite
def st_agent_with_manifest(draw, interval=None):
    """Generate an Agent with a manifest containing a schedule interval."""
    name = f"agent-{draw(integers(min_value=0, max_value=9999))}"
    iv = interval if interval is not None else draw(st_schedule_interval)
    manifest = AgentManifest(
        outputs=(("test", MarkType.OBSERVATION),),
        schedule_interval=iv,
    )
    return Agent(
        name=name,
        scopes={"test": ["observation"]},
        manifest=manifest,
    )


class TestSchedulerMinimumInterval:
    """P56: Minimum interval between activations >= schedule_interval."""

    @given(
        interval=st_schedule_interval,
        fraction=floats(
            min_value=0.0, max_value=0.999, allow_nan=False, allow_infinity=False
        ),
    )
    def test_not_due_before_interval(self, interval, fraction):
        """An agent activated at t=0 is never due before t=interval."""
        space = MarkSpace(clock=1_000_000.0)
        scheduler = Scheduler(space)
        agent = Agent(
            name="a",
            scopes={"test": ["observation"]},
            manifest=AgentManifest(
                outputs=(("test", MarkType.OBSERVATION),),
                schedule_interval=interval,
            ),
        )
        scheduler.register(agent)
        scheduler.tick_all()  # activate at t=1_000_000

        space.set_clock(1_000_000.0 + interval * fraction)
        assert len(scheduler.due()) == 0

    @given(
        interval=st_schedule_interval,
        extra=floats(
            min_value=0.001, max_value=1e6, allow_nan=False, allow_infinity=False
        ),
    )
    def test_due_after_interval(self, interval, extra):
        """An agent activated at t=0 is due at t > interval."""
        space = MarkSpace(clock=1_000_000.0)
        scheduler = Scheduler(space)
        agent = Agent(
            name="a",
            scopes={"test": ["observation"]},
            manifest=AgentManifest(
                outputs=(("test", MarkType.OBSERVATION),),
                schedule_interval=interval,
            ),
        )
        scheduler.register(agent)
        scheduler.tick_all()

        space.set_clock(1_000_000.0 + interval + extra)
        assert len(scheduler.due()) == 1

    @given(interval=st_schedule_interval)
    def test_new_agent_immediately_due(self, interval):
        """A freshly registered agent is always immediately due."""
        space = MarkSpace(clock=1_000_000.0)
        scheduler = Scheduler(space)
        agent = Agent(
            name="a",
            scopes={"test": ["observation"]},
            manifest=AgentManifest(
                outputs=(("test", MarkType.OBSERVATION),),
                schedule_interval=interval,
            ),
        )
        scheduler.register(agent)
        assert len(scheduler.due()) == 1


class TestSchedulerTickAllIdempotent:
    """tick_all does not double-fire within the same clock instant."""

    @given(interval=st_schedule_interval)
    def test_no_double_fire(self, interval):
        space = MarkSpace(clock=1_000_000.0)
        scheduler = Scheduler(space)
        agent = Agent(
            name="a",
            scopes={"test": ["observation"]},
            manifest=AgentManifest(
                outputs=(("test", MarkType.OBSERVATION),),
                schedule_interval=interval,
            ),
        )
        scheduler.register(agent)
        first = scheduler.tick_all()
        second = scheduler.tick_all()
        assert len(first) == 1
        assert len(second) == 0


class TestSchedulerRegisterFiltering:
    """Invalid intervals are never registered."""

    @given(interval=st_invalid_interval)
    def test_invalid_interval_rejected(self, interval):
        space = MarkSpace(clock=0.0)
        scheduler = Scheduler(space)
        agent = Agent(
            name="a",
            scopes={"test": ["observation"]},
            manifest=AgentManifest(
                outputs=(("test", MarkType.OBSERVATION),),
                schedule_interval=interval,
            ),
        )
        scheduler.register(agent)
        assert agent.id not in scheduler._entries

    def test_no_manifest_rejected(self):
        space = MarkSpace(clock=0.0)
        scheduler = Scheduler(space)
        agent = Agent(name="a", scopes={})
        scheduler.register(agent)
        assert agent.id not in scheduler._entries


class TestSchedulerUpdatePreservesActivation:
    """Updating schedule_interval preserves last activation time."""

    @given(
        old_interval=st_schedule_interval,
        new_interval=st_schedule_interval,
        elapsed=floats(
            min_value=0.0, max_value=1e6, allow_nan=False, allow_infinity=False
        ),
    )
    def test_update_does_not_reset_timer(self, old_interval, new_interval, elapsed):
        """After update, due() respects last_activation from before the update."""
        space = MarkSpace(clock=1_000_000.0)
        scheduler = Scheduler(space)

        agent = Agent(
            name="a",
            scopes={"test": ["observation"]},
            manifest=AgentManifest(
                outputs=(("test", MarkType.OBSERVATION),),
                schedule_interval=old_interval,
            ),
        )
        scheduler.register(agent)
        scheduler.tick_all()  # activate at t=1_000_000

        # Update to new interval (preserve agent id)
        updated = Agent(
            name="a",
            id=agent.id,
            scopes={"test": ["observation"]},
            manifest=AgentManifest(
                outputs=(("test", MarkType.OBSERVATION),),
                schedule_interval=new_interval,
            ),
        )
        scheduler.update(updated)

        # Advance clock
        space.set_clock(1_000_000.0 + elapsed)
        due = scheduler.due()

        # P56: due only if elapsed >= new_interval (with fp tolerance)
        if elapsed < new_interval - 1e-9:
            assert len(due) == 0
        elif elapsed > new_interval + 1e-9:
            assert len(due) == 1
        # At the exact boundary, either result is acceptable due to fp rounding


# ---------------------------------------------------------------------------
# Section E: Scheduler Stateful Testing
# ---------------------------------------------------------------------------


class SchedulerStateMachine(RuleBasedStateMachine):
    """
    Stateful property test for the Scheduler.

    Randomly registers/unregisters agents, advances clock, ticks,
    and verifies P56 (minimum interval) holds after every step.
    """

    def __init__(self):
        super().__init__()
        self.space = MarkSpace(clock=1_000_000.0)
        self.scheduler = Scheduler(self.space)
        # Track last activation times to verify P56 independently
        self._last_activation: dict[str, float] = {}
        self._intervals: dict[str, float] = {}
        # Track agent id per name slot for UUID-based scheduler keying
        self._agent_ids: dict[str, "uuid.UUID"] = {}

    @rule(
        interval=st_schedule_interval,
        idx=integers(min_value=0, max_value=9),
    )
    def register_agent(self, interval, idx):
        name = f"agent-{idx}"
        # Unregister the old entry first if re-registering the same slot,
        # otherwise the old UUID-keyed entry remains orphaned in the scheduler.
        if name in self._agent_ids:
            old_agent = Agent(name=name, id=self._agent_ids[name], scopes={})
            self.scheduler.unregister(old_agent)
        agent = Agent(
            name=name,
            scopes={"test": ["observation"]},
            manifest=AgentManifest(
                outputs=(("test", MarkType.OBSERVATION),),
                schedule_interval=interval,
            ),
        )
        self.scheduler.register(agent)
        self._intervals[name] = interval
        self._agent_ids[name] = agent.id
        # register() creates a fresh ScheduleEntry with last_activation=0.0,
        # so clear our model's activation tracking for this agent.
        self._last_activation.pop(name, None)

    @rule(
        new_interval=st_schedule_interval,
        idx=integers(min_value=0, max_value=9),
    )
    def update_agent(self, new_interval, idx):
        name = f"agent-{idx}"
        if name not in self._intervals:
            return  # nothing to update
        updated = Agent(
            name=name,
            id=self._agent_ids[name],
            scopes={"test": ["observation"]},
            manifest=AgentManifest(
                outputs=(("test", MarkType.OBSERVATION),),
                schedule_interval=new_interval,
            ),
        )
        self.scheduler.update(updated)
        # update() preserves last_activation, only changes interval
        self._intervals[name] = new_interval

    @rule(idx=integers(min_value=0, max_value=9))
    def unregister_agent(self, idx):
        name = f"agent-{idx}"
        if name not in self._agent_ids:
            return  # never registered, nothing to unregister
        agent = Agent(name=name, id=self._agent_ids[name], scopes={})
        self.scheduler.unregister(agent)
        self._intervals.pop(name, None)
        self._last_activation.pop(name, None)
        self._agent_ids.pop(name, None)

    @rule()
    def tick(self):
        fired = self.scheduler.tick_all()
        now = self.space.now()
        for agent in fired:
            self._last_activation[agent.name] = now

    @rule(
        delta=floats(
            min_value=0.0, max_value=3600.0, allow_nan=False, allow_infinity=False
        )
    )
    def advance_clock(self, delta):
        current = self.space.now()
        self.space.set_clock(current + delta)

    @invariant()
    def p43_minimum_interval(self):
        """P56: No agent in due() if less than schedule_interval has elapsed."""
        now = self.space.now()
        due = self.scheduler.due()
        for agent in due:
            name = agent.name
            if name in self._last_activation:
                elapsed = now - self._last_activation[name]
                assert elapsed >= self._intervals[name] - 1e-10

    @invariant()
    def only_registered_agents_due(self):
        """Only agents currently registered with a valid interval appear in due()."""
        due = self.scheduler.due()
        for agent in due:
            assert agent.name in self._intervals

    @invariant()
    def tick_all_idempotent(self):
        """Calling due() twice at the same time returns the same result."""
        due1 = self.scheduler.due()
        due2 = self.scheduler.due()
        assert {a.name for a in due1} == {a.name for a in due2}


TestSchedulerStateful = SchedulerStateMachine.TestCase
TestSchedulerStateful.settings = settings(max_examples=50, stateful_step_count=30)
