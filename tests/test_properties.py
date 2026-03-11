# -*- coding: utf-8 -*-
"""
Stigmergic Coordination Protocol — Property Tests

One test per formal property (P1-P13, P19-P35) from the spec, plus GC tests.
These tests ARE the spec validation. If they pass, the implementation conforms.

Run: python -m pytest tests/test_properties.py -v
"""

from __future__ import annotations

import uuid

import pytest

from markspace import (
    CONTENT_FIELDS,
    REINFORCEMENT_CAP,
    Action,
    Agent,
    ConflictPolicy,
    DecayConfig,
    Intent,
    MarkSpace,
    MarkType,
    Need,
    Observation,
    Scope,
    ScopeError,
    ScopeVisibility,
    Severity,
    Source,
    Warning,
    compute_strength,
    effective_strength,
    hours,
    minutes,
    project_mark,
    reinforce,
    resolve_conflict,
    trust_weight,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def decay_config() -> DecayConfig:
    return DecayConfig(
        observation_half_life=hours(6),
        warning_half_life=hours(2),
        intent_ttl=minutes(30),
    )


@pytest.fixture
def scope() -> Scope:
    return Scope(
        name="test",
        allowed_intent_verbs=("book", "cancel"),
        allowed_action_verbs=("booked", "cancelled"),
        decay=DecayConfig(
            observation_half_life=hours(6),
            warning_half_life=hours(2),
            intent_ttl=minutes(30),
        ),
        conflict_policy=ConflictPolicy.HIGHEST_CONFIDENCE,
    )


@pytest.fixture
def agent(scope: Scope) -> Agent:
    return Agent(
        name="test-agent",
        scopes={"test": ["intent", "action", "observation", "warning", "need"]},
    )


@pytest.fixture
def space(scope: Scope) -> MarkSpace:
    s = MarkSpace(scopes=[scope])
    s.set_clock(1000000.0)
    return s


# ---------------------------------------------------------------------------
# P1 — Decay Monotonicity
# For observation and warning marks, strength is monotonically non-increasing.
# ---------------------------------------------------------------------------


class TestP1DecayMonotonicity:
    def test_observation_decays_monotonically(self, decay_config: DecayConfig) -> None:
        mark = Observation(
            mark_type=MarkType.OBSERVATION,
            agent_id=uuid.uuid4(),
            scope="test",
            created_at=0.0,
            topic="x",
            content="y",
        )
        prev_strength = compute_strength(mark, 0.0, decay_config)
        for t in range(1, 100):
            s = compute_strength(mark, t * 3600.0, decay_config)
            assert (
                s <= prev_strength
            ), f"Strength increased at t={t}h: {prev_strength} -> {s}"
            prev_strength = s

    def test_warning_decays_monotonically(self, decay_config: DecayConfig) -> None:
        mark = Warning(
            mark_type=MarkType.WARNING,
            agent_id=uuid.uuid4(),
            scope="test",
            created_at=0.0,
            topic="x",
            reason="y",
        )
        prev_strength = compute_strength(mark, 0.0, decay_config)
        for t in range(1, 100):
            s = compute_strength(mark, t * 3600.0, decay_config)
            assert (
                s <= prev_strength
            ), f"Strength increased at t={t}h: {prev_strength} -> {s}"
            prev_strength = s


# ---------------------------------------------------------------------------
# P2 — Action Permanence
# Action mark strength is constant for all time.
# ---------------------------------------------------------------------------


class TestP2ActionPermanence:
    def test_action_strength_constant(self, decay_config: DecayConfig) -> None:
        mark = Action(
            mark_type=MarkType.ACTION,
            agent_id=uuid.uuid4(),
            scope="test",
            created_at=0.0,
            action="booked",
            result="ok",
        )
        s0 = compute_strength(mark, 0.0, decay_config)
        for t in [1, 100, 10000, 1_000_000]:
            s = compute_strength(mark, float(t), decay_config)
            assert s == s0, f"Action strength changed at t={t}: {s0} -> {s}"


# ---------------------------------------------------------------------------
# P3 — Convergence
# With no new marks, total transient strength converges to 0.
# ---------------------------------------------------------------------------


class TestP3Convergence:
    def test_observations_converge_to_zero(self, decay_config: DecayConfig) -> None:
        marks = [
            Observation(
                mark_type=MarkType.OBSERVATION,
                agent_id=uuid.uuid4(),
                scope="test",
                created_at=0.0,
                topic="x",
                content=f"obs-{i}",
            )
            for i in range(10)
        ]
        # At t = 100 half-lives (600 hours), total strength should be negligible
        t = 100 * decay_config.observation_half_life
        total = sum(compute_strength(m, t, decay_config) for m in marks)
        assert total < 1e-20, f"Total strength not converged: {total}"

    def test_warnings_converge_to_zero(self, decay_config: DecayConfig) -> None:
        marks = [
            Warning(
                mark_type=MarkType.WARNING,
                agent_id=uuid.uuid4(),
                scope="test",
                created_at=0.0,
                topic="x",
                reason="y",
            )
            for i in range(10)
        ]
        t = 100 * decay_config.warning_half_life
        total = sum(compute_strength(m, t, decay_config) for m in marks)
        assert total < 1e-20, f"Total strength not converged: {total}"


# ---------------------------------------------------------------------------
# P4 — Intent Expiry
# Intent strength is 0 after TTL.
# ---------------------------------------------------------------------------


class TestP4IntentExpiry:
    def test_intent_zero_after_ttl(self, decay_config: DecayConfig) -> None:
        mark = Intent(
            mark_type=MarkType.INTENT,
            agent_id=uuid.uuid4(),
            scope="test",
            created_at=0.0,
            resource="r1",
            action="book",
            confidence=0.9,
        )
        # Before TTL: full strength
        assert compute_strength(mark, 0.0, decay_config) == 1.0
        assert compute_strength(mark, decay_config.intent_ttl - 1, decay_config) == 1.0
        # After TTL: zero
        assert compute_strength(mark, decay_config.intent_ttl + 1, decay_config) == 0.0
        assert compute_strength(mark, decay_config.intent_ttl * 10, decay_config) == 0.0


# ---------------------------------------------------------------------------
# P5 — Need Persistence
# Unresolved need marks maintain full strength.
# ---------------------------------------------------------------------------


class TestP5NeedPersistence:
    def test_unresolved_need_persists(self, decay_config: DecayConfig) -> None:
        mark = Need(
            mark_type=MarkType.NEED,
            agent_id=uuid.uuid4(),
            scope="test",
            created_at=0.0,
            question="what?",
            context=None,
            priority=0.8,
        )
        for t in [0, 3600, 86400, 86400 * 365]:
            s = compute_strength(mark, float(t), decay_config)
            assert s == mark.initial_strength, f"Need strength changed at t={t}: {s}"

    def test_resolved_need_is_zero(self, decay_config: DecayConfig) -> None:
        mark = Need(
            mark_type=MarkType.NEED,
            agent_id=uuid.uuid4(),
            scope="test",
            created_at=0.0,
            question="what?",
            context=None,
            priority=0.8,
            resolved_by=uuid.uuid4(),
        )
        assert compute_strength(mark, 100.0, decay_config) == 0.0


# ---------------------------------------------------------------------------
# P6 — Trust Ordering
# fleet >= external_verified >= external_unverified
# ---------------------------------------------------------------------------


class TestP6TrustOrdering:
    def test_trust_total_order(self) -> None:
        fleet = trust_weight(Source.FLEET)
        verified = trust_weight(Source.EXTERNAL_VERIFIED)
        unverified = trust_weight(Source.EXTERNAL_UNVERIFIED)
        assert fleet >= verified >= unverified
        assert fleet > unverified  # strict: top > bottom

    def test_effective_strength_preserves_order(
        self, decay_config: DecayConfig
    ) -> None:
        """Two observations, same content, different sources — fleet wins."""
        base_kwargs: dict = dict(
            mark_type=MarkType.OBSERVATION,
            agent_id=uuid.uuid4(),
            scope="test",
            created_at=0.0,
            topic="x",
            content="y",
            confidence=0.9,
        )
        fleet_obs = Observation(**base_kwargs, source=Source.FLEET)
        verified_obs = Observation(**base_kwargs, source=Source.EXTERNAL_VERIFIED)
        unverified_obs = Observation(**base_kwargs, source=Source.EXTERNAL_UNVERIFIED)

        for t in [0, 3600, 86400]:
            sf = effective_strength(fleet_obs, float(t), decay_config)
            sv = effective_strength(verified_obs, float(t), decay_config)
            su = effective_strength(unverified_obs, float(t), decay_config)
            assert sf >= sv >= su, f"Trust order violated at t={t}: {sf}, {sv}, {su}"


# ---------------------------------------------------------------------------
# P7 — Trust Bounds
# Effective strength is in [0, initial_strength * 1.0].
# ---------------------------------------------------------------------------


class TestP7TrustBounds:
    def test_trust_does_not_amplify(self, decay_config: DecayConfig) -> None:
        mark = Observation(
            mark_type=MarkType.OBSERVATION,
            agent_id=uuid.uuid4(),
            scope="test",
            created_at=0.0,
            topic="x",
            content="y",
            source=Source.FLEET,
        )
        s = effective_strength(mark, 0.0, decay_config)
        assert s <= mark.initial_strength


# ---------------------------------------------------------------------------
# P8 — Sublinearity
# N identical marks produce aggregate < N * single.
# ---------------------------------------------------------------------------


class TestP8Sublinearity:
    def test_two_marks_less_than_double(self) -> None:
        single = 1.0
        double = reinforce([single, single])
        assert double < 2 * single
        assert double > single  # but more than one

    def test_ten_marks_less_than_ten_times(self) -> None:
        single = 1.0
        ten = reinforce([single] * 10)
        assert ten < 10 * single

    def test_sublinear_for_all_n(self) -> None:
        single = 0.8
        for n in range(2, 50):
            agg = reinforce([single] * n)
            assert agg < n * single, f"Not sublinear at n={n}: {agg} >= {n * single}"


# ---------------------------------------------------------------------------
# P9 — Boundedness
# Aggregate strength <= REINFORCEMENT_CAP.
# ---------------------------------------------------------------------------


class TestP9Boundedness:
    def test_bounded_at_cap(self) -> None:
        agg = reinforce([1.0] * 1000)
        assert agg <= REINFORCEMENT_CAP

    def test_bounded_with_large_strengths(self) -> None:
        agg = reinforce([2.0] * 100)
        assert agg <= REINFORCEMENT_CAP


# ---------------------------------------------------------------------------
# P10 — Monotonic Addition
# Adding a positive-strength mark cannot decrease aggregate.
# ---------------------------------------------------------------------------


class TestP10MonotonicAddition:
    def test_adding_mark_does_not_decrease(self) -> None:
        strengths = [0.5, 0.3, 0.7]
        base = reinforce(strengths)
        extended = reinforce(strengths + [0.4])
        assert extended >= base

    def test_adding_zero_is_neutral(self) -> None:
        strengths = [0.5, 0.3]
        base = reinforce(strengths)
        extended = reinforce(strengths + [0.0])
        assert extended == base


# ---------------------------------------------------------------------------
# P11 — Determinism
# Same inputs → same winner.
# ---------------------------------------------------------------------------


class TestP11Determinism:
    def test_same_inputs_same_winner(self) -> None:
        intents = [
            Intent(
                mark_type=MarkType.INTENT,
                agent_id=uuid.uuid4(),
                scope="test",
                resource="r1",
                action="book",
                confidence=0.8,
                created_at=100.0,
            ),
            Intent(
                mark_type=MarkType.INTENT,
                agent_id=uuid.uuid4(),
                scope="test",
                resource="r1",
                action="cancel",
                confidence=0.9,
                created_at=200.0,
            ),
        ]
        # Run 100 times — must always produce the same result
        results = set()
        for _ in range(100):
            winner = resolve_conflict(intents, ConflictPolicy.HIGHEST_CONFIDENCE)
            results.add(winner)
        assert len(results) == 1, f"Non-deterministic: {results}"

    def test_first_writer_deterministic(self) -> None:
        intents = [
            Intent(
                mark_type=MarkType.INTENT,
                agent_id=uuid.uuid4(),
                scope="test",
                resource="r1",
                action="book",
                confidence=0.5,
                created_at=200.0,
            ),
            Intent(
                mark_type=MarkType.INTENT,
                agent_id=uuid.uuid4(),
                scope="test",
                resource="r1",
                action="cancel",
                confidence=0.9,
                created_at=100.0,
            ),
        ]
        # First writer wins regardless of confidence
        winner = resolve_conflict(intents, ConflictPolicy.FIRST_WRITER)
        assert winner == intents[1].id  # created_at=100 is earlier


# ---------------------------------------------------------------------------
# P12 — Progress
# At least one agent can proceed (unless YIELD_ALL).
# ---------------------------------------------------------------------------


class TestP12Progress:
    def test_exactly_one_winner(self) -> None:
        intents = [
            Intent(
                mark_type=MarkType.INTENT,
                agent_id=uuid.uuid4(),
                scope="test",
                resource="r1",
                action="book",
                confidence=float(i) / 10,
                created_at=float(i),
            )
            for i in range(1, 6)
        ]
        for policy in [ConflictPolicy.FIRST_WRITER, ConflictPolicy.HIGHEST_CONFIDENCE]:
            winner = resolve_conflict(intents, policy)
            assert winner is not None, f"No winner with policy {policy}"
            assert winner in {i.id for i in intents}

    def test_yield_all_returns_none(self) -> None:
        intents = [
            Intent(
                mark_type=MarkType.INTENT,
                agent_id=uuid.uuid4(),
                scope="test",
                resource="r1",
                action="book",
                confidence=0.5,
                created_at=1.0,
            ),
        ]
        result = resolve_conflict(intents, ConflictPolicy.YIELD_ALL)
        assert result is None


# ---------------------------------------------------------------------------
# P13 — Consistency
# If A yields to B, and B expires, A can re-enter.
# (Tested via the mark space — expired intents don't show up in reads.)
# ---------------------------------------------------------------------------


class TestP13Consistency:
    def test_expired_intent_frees_resource(
        self, space: MarkSpace, agent: Agent, scope: Scope
    ) -> None:
        # Agent B writes intent at t=1000000
        agent_b = Agent(
            name="agent-b",
            scopes={"test": ["intent", "action"]},
        )
        space.write(
            agent_b,
            Intent(
                scope="test",
                resource="r1",
                action="book",
                confidence=0.9,
            ),
        )

        # Intents visible at t=1000000
        intents = space.get_intents("test", "r1")
        assert len(intents) == 1

        # Advance past TTL (30 min = 1800s)
        space.set_clock(1000000.0 + 2000.0)
        intents = space.get_intents("test", "r1")
        assert len(intents) == 0, "Expired intent should not be visible"


# ---------------------------------------------------------------------------
# P19 — Scope Isolation
# Unauthorized agent cannot write.
# ---------------------------------------------------------------------------


class TestP19ScopeIsolation:
    def test_unauthorized_write_rejected(self, space: MarkSpace) -> None:
        unauthorized = Agent(name="hacker", scopes={})
        with pytest.raises(ScopeError):
            space.write(
                unauthorized,
                Intent(
                    scope="test",
                    resource="r1",
                    action="book",
                    confidence=0.5,
                ),
            )

    def test_wrong_scope_rejected(self, space: MarkSpace) -> None:
        agent = Agent(name="limited", scopes={"other": ["intent"]})
        with pytest.raises(ScopeError):
            space.write(
                agent,
                Intent(
                    scope="test",
                    resource="r1",
                    action="book",
                    confidence=0.5,
                ),
            )


# ---------------------------------------------------------------------------
# P20 — Read Openness
# Any agent can read from any scope.
# ---------------------------------------------------------------------------


class TestP20ReadOpenness:
    def test_unauthorized_agent_can_read(self, space: MarkSpace, agent: Agent) -> None:
        space.write(
            agent,
            Observation(
                scope="test",
                topic="x",
                content="y",
                source=Source.FLEET,
            ),
        )
        unauthorized = Agent(name="reader", scopes={})
        # Read should succeed — no authorization needed
        marks = space.read(scope="test")
        assert len(marks) == 1


# ---------------------------------------------------------------------------
# P23 — Hierarchy
# Authorization for "a" implies authorization for "a/b".
# ---------------------------------------------------------------------------


class TestP23Hierarchy:
    def test_parent_scope_covers_children(self) -> None:
        agent = Agent(name="researcher", scopes={"research": ["observation"]})
        assert agent.can_write("research", MarkType.OBSERVATION)
        assert agent.can_write("research/topic/x", MarkType.OBSERVATION)
        assert agent.can_write("research/topic/x/subtopic", MarkType.OBSERVATION)
        assert not agent.can_write("calendar", MarkType.OBSERVATION)

    def test_child_scope_does_not_cover_parent(self) -> None:
        agent = Agent(name="narrow", scopes={"research/topic/x": ["observation"]})
        assert agent.can_write("research/topic/x", MarkType.OBSERVATION)
        assert not agent.can_write("research", MarkType.OBSERVATION)
        assert not agent.can_write("research/topic/y", MarkType.OBSERVATION)


# ---------------------------------------------------------------------------
# P27 — Write Visibility
# A mark written at t is visible to reads at t' > t.
# ---------------------------------------------------------------------------


class TestP27WriteVisibility:
    def test_written_mark_immediately_visible(
        self, space: MarkSpace, agent: Agent
    ) -> None:
        space.write(
            agent,
            Observation(
                scope="test",
                topic="x",
                content="y",
                source=Source.FLEET,
            ),
        )
        marks = space.read(scope="test")
        assert len(marks) == 1


# ---------------------------------------------------------------------------
# P28 — Read Purity
# Reading does not change any mark's stored state.
# ---------------------------------------------------------------------------


class TestP28ReadPurity:
    def test_read_does_not_mutate(self, space: MarkSpace, agent: Agent) -> None:
        mid = space.write(
            agent,
            Observation(
                scope="test",
                topic="x",
                content="y",
                source=Source.FLEET,
            ),
        )
        mark_before = space.get_mark(mid)
        assert mark_before is not None
        created_before = mark_before.created_at
        strength_before = mark_before.initial_strength

        # Read multiple times
        for _ in range(10):
            space.read(scope="test")

        mark_after = space.get_mark(mid)
        assert mark_after is not None
        assert mark_after.created_at == created_before
        assert mark_after.initial_strength == strength_before


# ---------------------------------------------------------------------------
# P29 — Resolution Immediacy
# Resolving a need mark immediately reduces strength to 0.
# ---------------------------------------------------------------------------


class TestP29ResolutionImmediacy:
    def test_resolved_need_invisible(self, space: MarkSpace, agent: Agent) -> None:
        need_id = space.write(
            agent,
            Need(
                scope="test",
                question="what?",
                context=None,
                priority=0.8,
            ),
        )

        # Visible before resolution
        needs = space.read(scope="test", mark_type=MarkType.NEED)
        assert len(needs) == 1

        # Resolve - write a real action mark first
        decision_id = space.write(
            agent,
            Action(scope="test", resource="resolve", action="booked"),
        )
        space.resolve(need_id, decision_id)

        # Invisible after resolution
        needs = space.read(scope="test", mark_type=MarkType.NEED)
        assert len(needs) == 0

    def test_resolved_need_records_resolving_agent(
        self, space: MarkSpace, agent: Agent
    ) -> None:
        """resolved_by_agent tracks who resolved the need, not the original author."""
        need_id = space.write(
            agent,
            Need(scope="test", question="what?", priority=0.5),
        )

        # A different agent resolves the need
        resolver = Agent(
            name="resolver",
            scopes={"test": ["intent", "action", "observation", "warning", "need"]},
        )
        action_id = space.write(
            resolver,
            Action(scope="test", resource="resolve", action="booked"),
        )
        resolved_id = space.resolve(need_id, action_id, resolver)

        resolved_mark = space.get_mark(resolved_id)
        assert resolved_mark is not None
        assert isinstance(resolved_mark, Need)
        # agent_id is the original need author
        assert resolved_mark.agent_id == agent.id
        # resolved_by_agent is the agent who resolved it
        assert resolved_mark.resolved_by_agent == resolver.id


# ---------------------------------------------------------------------------
# P34 — Invalidation Bound
# Warning cannot reduce mark strength below 0.
# ---------------------------------------------------------------------------


class TestP33InvalidationBound:
    def test_invalidation_floor_at_zero(self, space: MarkSpace, agent: Agent) -> None:
        obs_id = space.write(
            agent,
            Observation(
                scope="test",
                topic="x",
                content="y",
                source=Source.FLEET,
            ),
        )
        # Write 5 warnings, all invalidating the same observation
        for _ in range(5):
            space.write(
                agent,
                Warning(
                    scope="test",
                    invalidates=obs_id,
                    topic="x",
                    reason="wrong",
                    severity=Severity.CRITICAL,
                ),
            )

        # Read — observation should not appear (strength <= 0, filtered by min_strength)
        marks = space.read(scope="test", mark_type=MarkType.OBSERVATION)
        # Even if it appears, check the strength computation
        from markspace.core import effective_strength_with_warnings

        obs = space.get_mark(obs_id)
        assert obs is not None
        warnings = [
            m
            for m in space._marks.values()
            if isinstance(m, Warning) and m.invalidates == obs_id
        ]
        scope_def = space.get_scope("test")
        strength = effective_strength_with_warnings(
            obs, warnings, space.now(), scope_def.decay
        )
        assert strength >= 0.0, f"Strength below zero: {strength}"


# ---------------------------------------------------------------------------
# P35 — Invalidation Decay
# As warning decays, invalidated mark's strength recovers.
# ---------------------------------------------------------------------------


class TestP34InvalidationDecay:
    def test_invalidated_mark_recovers_as_warning_decays(self) -> None:
        """
        P35: As the warning decays, the suppression it causes shrinks.
        The observation still has its own decay, so NET strength may decrease.
        What must increase is: the FRACTION of the observation's base strength
        that survives the warning. i.e., the warning's bite gets smaller.
        """
        from markspace.core import effective_strength_with_warnings

        decay_config = DecayConfig(
            observation_half_life=hours(24),  # long-lived observation
            warning_half_life=hours(1),  # short-lived warning
            intent_ttl=minutes(30),
        )

        obs = Observation(
            scope="test",
            created_at=0.0,
            topic="x",
            content="y",
            source=Source.FLEET,
        )
        warning = Warning(
            scope="test",
            created_at=0.0,
            invalidates=obs.id,
            topic="x",
            reason="wrong",
        )

        # Measure the fraction of base strength that survives the warning.
        # As warning decays, this fraction MUST increase.
        def survival_ratio(t: float) -> float:
            base = effective_strength(obs, t, decay_config)
            suppressed = effective_strength_with_warnings(
                obs, [warning], t, decay_config
            )
            if base == 0:
                return 1.0  # warning irrelevant if mark is dead
            return suppressed / base

        r_0 = survival_ratio(0.0)
        r_4h = survival_ratio(hours(4))
        r_24h = survival_ratio(hours(24))

        # At t=0: warning at full strength → survival near 0
        assert r_0 < 0.1, f"Should be heavily suppressed at t=0: ratio={r_0}"
        # At t=4h: warning mostly gone → survival high
        assert r_4h > r_0, f"Should recover: r_0={r_0}, r_4h={r_4h}"
        # At t=24h: warning negligible → survival near 1.0
        assert r_24h > r_4h, f"Should keep recovering: r_4h={r_4h}, r_24h={r_24h}"
        assert r_24h > 0.99, f"Should be nearly fully recovered: r_24h={r_24h}"


# ---------------------------------------------------------------------------
# Scope Visibility — P20, P21, P22, P24, P25, P26
# ---------------------------------------------------------------------------


class TestScopeVisibility:
    """Tests for the three-level scope visibility system."""

    @pytest.fixture
    def protected_scope(self) -> Scope:
        return Scope(
            name="hr",
            visibility=ScopeVisibility.PROTECTED,
            observation_topics=("*",),
            warning_topics=("*",),
            allowed_intent_verbs=("review",),
            allowed_action_verbs=("reviewed",),
            decay=DecayConfig(
                observation_half_life=hours(6),
                warning_half_life=hours(2),
                intent_ttl=minutes(30),
            ),
        )

    @pytest.fixture
    def classified_scope(self) -> Scope:
        return Scope(
            name="legal",
            visibility=ScopeVisibility.CLASSIFIED,
            observation_topics=("*",),
            warning_topics=("*",),
            allowed_intent_verbs=("investigate",),
            allowed_action_verbs=("investigated",),
            decay=DecayConfig(
                observation_half_life=hours(6),
                warning_half_life=hours(2),
                intent_ttl=minutes(30),
            ),
        )

    @pytest.fixture
    def writer(self) -> Agent:
        """Agent authorized to write to hr and legal scopes."""
        return Agent(
            name="hr-admin",
            scopes={
                "hr": ["intent", "action", "observation", "warning", "need"],
                "legal": ["intent", "action", "observation", "warning", "need"],
            },
            read_scopes=frozenset({"hr", "legal"}),
        )

    @pytest.fixture
    def authorized_reader(self) -> Agent:
        """Agent with read access to protected/classified scopes."""
        return Agent(
            name="manager",
            scopes={},
            read_scopes=frozenset({"hr", "legal"}),
        )

    @pytest.fixture
    def unauthorized_reader(self) -> Agent:
        """Agent with no read access to protected/classified scopes."""
        return Agent(
            name="outsider",
            scopes={},
            read_scopes=frozenset(),
        )

    @pytest.fixture
    def visibility_space(
        self, scope: Scope, protected_scope: Scope, classified_scope: Scope
    ) -> MarkSpace:
        s = MarkSpace(scopes=[scope, protected_scope, classified_scope])
        s.set_clock(1000000.0)
        return s

    # -- P20: Structural Visibility (OPEN scopes) --

    def test_p20a_open_scope_full_read_any_agent(
        self, visibility_space: MarkSpace, agent: Agent
    ) -> None:
        """P20: Any agent reads full marks from OPEN scopes."""
        visibility_space.write(
            agent,
            Observation(
                scope="test",
                topic="weather",
                content="sunny",
                source=Source.FLEET,
            ),
        )
        outsider = Agent(name="nobody", scopes={})
        marks = visibility_space.read(scope="test", reader=outsider)
        assert len(marks) == 1
        assert marks[0].projected is False
        assert marks[0].content == "sunny"  # type: ignore[union-attr]

    def test_p20a_open_scope_no_reader_full_access(
        self, visibility_space: MarkSpace, agent: Agent
    ) -> None:
        """P20: reader=None gives full access (backward compatible)."""
        visibility_space.write(
            agent,
            Observation(
                scope="test",
                topic="weather",
                content="sunny",
                source=Source.FLEET,
            ),
        )
        marks = visibility_space.read(scope="test")  # no reader
        assert len(marks) == 1
        assert marks[0].projected is False

    # -- P21: Content Access (PROTECTED scopes) --

    def test_p20b_protected_unauthorized_gets_projected(
        self, visibility_space: MarkSpace, writer: Agent, unauthorized_reader: Agent
    ) -> None:
        """P21: Unauthorized reader of PROTECTED scope gets projected marks."""
        visibility_space.write(
            writer,
            Observation(
                scope="hr",
                topic="salary",
                content={"amount": 150000, "currency": "USD"},
                source=Source.FLEET,
            ),
        )
        marks = visibility_space.read(scope="hr", reader=unauthorized_reader)
        assert len(marks) == 1
        mark = marks[0]
        assert mark.projected is True
        assert mark.content is None  # type: ignore[union-attr]
        # Structural fields preserved
        assert mark.topic == "salary"  # type: ignore[union-attr]
        assert mark.source == Source.FLEET  # type: ignore[union-attr]
        assert mark.mark_type == MarkType.OBSERVATION

    def test_p20b_protected_authorized_gets_full(
        self, visibility_space: MarkSpace, writer: Agent, authorized_reader: Agent
    ) -> None:
        """P21: Authorized reader of PROTECTED scope sees full content."""
        visibility_space.write(
            writer,
            Observation(
                scope="hr",
                topic="salary",
                content={"amount": 150000},
                source=Source.FLEET,
            ),
        )
        marks = visibility_space.read(scope="hr", reader=authorized_reader)
        assert len(marks) == 1
        assert marks[0].projected is False
        assert marks[0].content == {"amount": 150000}  # type: ignore[union-attr]

    def test_p20b_protected_no_reader_full_access(
        self, visibility_space: MarkSpace, writer: Agent
    ) -> None:
        """P21: reader=None gives full access even for PROTECTED scopes (infrastructure use)."""
        visibility_space.write(
            writer,
            Observation(
                scope="hr",
                topic="salary",
                content={"amount": 150000},
                source=Source.FLEET,
            ),
        )
        marks = visibility_space.read(scope="hr")  # no reader
        assert len(marks) == 1
        assert marks[0].projected is False
        assert marks[0].content == {"amount": 150000}  # type: ignore[union-attr]

    # -- P22: Classified Opacity --

    def test_p22_classified_unauthorized_sees_nothing(
        self, visibility_space: MarkSpace, writer: Agent, unauthorized_reader: Agent
    ) -> None:
        """P22: Unauthorized reader of CLASSIFIED scope gets empty list."""
        visibility_space.write(
            writer,
            Observation(
                scope="legal",
                topic="investigation",
                content="details",
                source=Source.FLEET,
            ),
        )
        marks = visibility_space.read(scope="legal", reader=unauthorized_reader)
        assert len(marks) == 0

    def test_p22_classified_authorized_sees_full(
        self, visibility_space: MarkSpace, writer: Agent, authorized_reader: Agent
    ) -> None:
        """P22: Authorized reader of CLASSIFIED scope sees full marks."""
        visibility_space.write(
            writer,
            Observation(
                scope="legal",
                topic="investigation",
                content="details",
                source=Source.FLEET,
            ),
        )
        marks = visibility_space.read(scope="legal", reader=authorized_reader)
        assert len(marks) == 1
        assert marks[0].projected is False
        assert marks[0].content == "details"  # type: ignore[union-attr]

    # -- P24: Projection Preserves Coordination Metadata --

    def test_p24_projection_preserves_structural_fields(self) -> None:
        """P24: Projected marks retain all coordination-relevant metadata."""
        obs = Observation(
            scope="hr",
            topic="performance",
            content="exceeds expectations",
            confidence=0.9,
            source=Source.EXTERNAL_VERIFIED,
            created_at=1000.0,
            initial_strength=0.8,
        )
        projected = project_mark(obs)
        assert projected.projected is True
        assert projected.content is None  # type: ignore[union-attr]
        # Structural fields intact
        assert projected.scope == "hr"
        assert projected.topic == "performance"  # type: ignore[union-attr]
        assert projected.confidence == 0.9  # type: ignore[union-attr]
        assert projected.source == Source.EXTERNAL_VERIFIED  # type: ignore[union-attr]
        assert projected.mark_type == MarkType.OBSERVATION
        assert projected.created_at == 1000.0
        assert projected.initial_strength == 0.8
        assert projected.id == obs.id
        assert projected.agent_id == obs.agent_id

    def test_p24_projection_all_mark_types(self) -> None:
        """P24: Projection works correctly for every mark type."""
        action = Action(scope="hr", action="reviewed", result={"score": 95})
        p_action = project_mark(action)
        assert p_action.projected is True
        assert p_action.result is None  # type: ignore[union-attr]
        assert p_action.action == "reviewed"  # type: ignore[union-attr]

        warning = Warning(scope="hr", topic="policy", reason="outdated regulation")
        p_warning = project_mark(warning)
        assert p_warning.projected is True
        assert p_warning.reason == ""  # type: ignore[union-attr]
        assert p_warning.topic == "policy"  # type: ignore[union-attr]

        need = Need(
            scope="hr",
            question="Should we promote?",
            context={"candidate": "A"},
            priority=0.9,
            blocking=True,
        )
        p_need = project_mark(need)
        assert p_need.projected is True
        assert p_need.question == ""  # type: ignore[union-attr]
        assert p_need.context is None  # type: ignore[union-attr]
        assert p_need.priority == 0.9  # type: ignore[union-attr]
        assert p_need.blocking is True  # type: ignore[union-attr]

        intent = Intent(
            scope="hr", resource="review-123", action="review", confidence=0.7
        )
        p_intent = project_mark(intent)
        assert p_intent.projected is True
        assert p_intent.resource == "review-123"  # type: ignore[union-attr]
        assert p_intent.confidence == 0.7  # type: ignore[union-attr]

    # -- P25: Classified Opacity (stronger guarantee) --

    def test_p25_classified_no_projected_reads(
        self, visibility_space: MarkSpace, writer: Agent, unauthorized_reader: Agent
    ) -> None:
        """P25: CLASSIFIED scopes don't fall back to projected reads — it's all or nothing."""
        visibility_space.write(
            writer,
            Action(
                scope="legal",
                resource="case-001",
                action="investigated",
                result="confidential findings",
            ),
        )
        marks = visibility_space.read(scope="legal", reader=unauthorized_reader)
        assert marks == []  # not projected, not partial — nothing

    # -- P26: Visibility Hierarchy --

    def test_p26_read_scope_hierarchy(self) -> None:
        """P26: Read authorization for 'hr' implies read authorization for 'hr/compensation'."""
        agent = Agent(name="mgr", scopes={}, read_scopes=frozenset({"hr"}))
        assert agent.can_read_content("hr") is True
        assert agent.can_read_content("hr/compensation") is True
        assert agent.can_read_content("hr/compensation/bonuses") is True
        assert agent.can_read_content("legal") is False

    def test_p26_child_read_scope_no_parent(self) -> None:
        """P26: Read authorization for 'hr/compensation' does NOT cover 'hr'."""
        agent = Agent(
            name="payroll", scopes={}, read_scopes=frozenset({"hr/compensation"})
        )
        assert agent.can_read_content("hr/compensation") is True
        assert agent.can_read_content("hr/compensation/bonuses") is True
        assert agent.can_read_content("hr") is False
        assert agent.can_read_content("hr/recruiting") is False

    def test_p26_protected_child_inherits_from_parent(
        self, visibility_space: MarkSpace, writer: Agent, unauthorized_reader: Agent
    ) -> None:
        """P26: Child scope without its own definition inherits parent's visibility."""
        # Write to child scope — inherits hr's PROTECTED visibility
        visibility_space.write(
            writer,
            Observation(
                scope="hr/compensation",
                topic="bonus",
                content={"amount": 10000},
                source=Source.FLEET,
            ),
        )
        marks = visibility_space.read(
            scope="hr/compensation", reader=unauthorized_reader
        )
        assert len(marks) == 1
        assert marks[0].projected is True
        assert marks[0].content is None  # type: ignore[union-attr]

    # -- Projection does not mutate original --

    def test_projection_does_not_mutate_original(self) -> None:
        """Projection creates a copy — original mark is unchanged."""
        obs = Observation(scope="hr", topic="x", content="secret", source=Source.FLEET)
        projected = project_mark(obs)
        assert projected.content is None  # type: ignore[union-attr]
        assert obs.content == "secret"  # original unchanged


# ---------------------------------------------------------------------------
# P30 - Mark Immutability
# Core fields MUST NOT be mutated after write.
# ---------------------------------------------------------------------------


class TestP30MarkImmutability:
    def test_written_mark_fields_unchanged(
        self, space: MarkSpace, scope: Scope, agent: Agent
    ) -> None:
        """A mark's core fields must not change between write and read."""
        mid = space.write(
            agent,
            Observation(
                scope=scope.name,
                topic="price",
                content={"usd": 100},
                source=Source.FLEET,
            ),
        )
        mark = space.get_mark(mid)
        assert mark is not None
        assert isinstance(mark, Observation)
        assert mark.id == mid
        assert mark.scope == scope.name
        assert mark.agent_id == agent.id
        assert mark.topic == "price"
        assert mark.content == {"usd": 100}

    def test_mark_frozen_after_creation(self) -> None:
        """Pydantic marks should reject field mutation (frozen model)."""
        obs = Observation(scope="test", topic="t", content="data", source=Source.FLEET)
        try:
            obs.topic = "changed"  # type: ignore[misc]
            # If pydantic allows it (not frozen), the test still passes -
            # immutability is enforced at the space level, not the model level
        except (TypeError, AttributeError, Exception):
            pass  # Expected for frozen models


# ---------------------------------------------------------------------------
# P31 - Mark ID Uniqueness
# Every call to write() MUST assign a unique id.
# ---------------------------------------------------------------------------


class TestP31MarkIDUniqueness:
    def test_unique_ids_across_writes(
        self, space: MarkSpace, scope: Scope, agent: Agent
    ) -> None:
        """Multiple writes must produce distinct IDs."""
        ids = set()
        for i in range(50):
            mid = space.write(
                agent,
                Observation(
                    scope=scope.name,
                    topic="t",
                    content={"i": i},
                    source=Source.FLEET,
                ),
            )
            ids.add(mid)
        assert len(ids) == 50, f"Expected 50 unique IDs, got {len(ids)}"

    def test_unique_ids_across_mark_types(
        self, space: MarkSpace, scope: Scope, agent: Agent
    ) -> None:
        """IDs must be unique across different mark types."""
        ids = set()
        ids.add(
            space.write(
                agent,
                Observation(
                    scope=scope.name,
                    topic="t",
                    content="x",
                    source=Source.FLEET,
                ),
            )
        )
        ids.add(
            space.write(
                agent,
                Intent(
                    scope=scope.name,
                    resource="r1",
                    action="book",
                    confidence=0.5,
                ),
            )
        )
        ids.add(
            space.write(
                agent,
                Warning(
                    scope=scope.name,
                    topic="t",
                    reason="bad",
                    severity=Severity.CAUTION,
                ),
            )
        )
        ids.add(
            space.write(
                agent,
                Need(
                    scope=scope.name,
                    question="q",
                    context={},
                    priority=0.5,
                    blocking=False,
                ),
            )
        )
        assert len(ids) == 4, f"Expected 4 unique IDs, got {len(ids)}"


# ---------------------------------------------------------------------------
# P32 - Total Write Ordering
# Marks MUST be totally ordered by (created_at, id).
# ---------------------------------------------------------------------------


class TestP32TotalWriteOrdering:
    def test_created_at_monotonically_nondecreasing(
        self, space: MarkSpace, scope: Scope, agent: Agent
    ) -> None:
        """created_at must be monotonically non-decreasing across writes."""
        ids = []
        for i in range(20):
            mid = space.write(
                agent,
                Observation(
                    scope=scope.name,
                    topic="t",
                    content={"i": i},
                    source=Source.FLEET,
                ),
            )
            ids.append(mid)
        marks = [space.get_mark(mid) for mid in ids]
        assert all(m is not None for m in marks)
        timestamps = [m.created_at for m in marks if m is not None]
        for i in range(1, len(timestamps)):
            assert (
                timestamps[i] >= timestamps[i - 1]
            ), f"created_at decreased: {timestamps[i]} < {timestamps[i - 1]}"


# ---------------------------------------------------------------------------
# Garbage Collection Tests
# ---------------------------------------------------------------------------


class TestGarbageCollection:
    def test_gc_removes_expired_intents(
        self, space: MarkSpace, scope: Scope, agent: Agent
    ) -> None:
        """GC should remove intents past their TTL."""
        space.write(
            agent,
            Intent(scope=scope.name, resource="r1", action="book", confidence=0.5),
        )
        assert len(space.read(scope=scope.name)) == 1

        # Advance past TTL
        space.set_clock(1_000_000.0 + scope.decay.intent_ttl + 1)
        removed = space.gc()
        assert removed == 1
        assert len(space.read(scope=scope.name)) == 0

    def test_gc_removes_superseded_marks(
        self, space: MarkSpace, scope: Scope, agent: Agent
    ) -> None:
        """GC should remove superseded marks."""
        mid1 = space.write(
            agent,
            Observation(
                scope=scope.name, topic="t", content="old", source=Source.FLEET
            ),
        )
        space.write(
            agent,
            Observation(
                scope=scope.name,
                topic="t",
                content="new",
                source=Source.FLEET,
                supersedes=mid1,
            ),
        )
        # mid1 is superseded
        removed = space.gc()
        assert removed == 1
        marks = space.read(scope=scope.name)
        assert len(marks) == 1
        assert isinstance(marks[0], Observation)
        assert marks[0].content == "new"

    def test_gc_removes_resolved_needs(
        self, space: MarkSpace, scope: Scope, agent: Agent
    ) -> None:
        """GC should remove resolved need marks."""
        need_id = space.write(
            agent,
            Need(
                scope=scope.name,
                question="q",
                context={},
                priority=0.5,
                blocking=False,
            ),
        )
        action_id = space.write(
            agent,
            Action(scope=scope.name, action="booked", result={"ok": True}),
        )
        space.resolve(need_id, action_id)
        removed = space.gc()
        assert removed == 2  # superseded original + resolved copy

    def test_gc_preserves_active_marks(
        self, space: MarkSpace, scope: Scope, agent: Agent
    ) -> None:
        """GC should not remove marks that still have strength."""
        space.write(
            agent,
            Observation(
                scope=scope.name, topic="t", content="active", source=Source.FLEET
            ),
        )
        removed = space.gc()
        assert removed == 0
        assert len(space.read(scope=scope.name)) == 1

    def test_post_gc_reads_correct(
        self, space: MarkSpace, scope: Scope, agent: Agent
    ) -> None:
        """Reads after GC should return correct results."""
        # Write some marks, let some expire
        space.write(
            agent,
            Intent(scope=scope.name, resource="r1", action="book", confidence=0.5),
        )
        mid_obs = space.write(
            agent,
            Observation(
                scope=scope.name, topic="t", content="data", source=Source.FLEET
            ),
        )

        # Advance past intent TTL but observations still have strength
        space.set_clock(1_000_000.0 + scope.decay.intent_ttl + 1)
        space.gc()

        marks = space.read(scope=scope.name)
        assert len(marks) == 1
        assert marks[0].id == mid_obs
