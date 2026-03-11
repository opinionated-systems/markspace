# -*- coding: utf-8 -*-
"""
Stigmergic Coordination Protocol — Scenario Tests

End-to-end scenarios demonstrating the DSL and proving coordination works.
Each scenario is a miniature multi-agent system that exercises the protocol.

Run: python -m pytest tests/test_scenarios.py -v
"""

from __future__ import annotations

import uuid

import pytest

from markspace import (
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
    Severity,
    Source,
    Warning,
    hours,
    minutes,
)

# ---------------------------------------------------------------------------
# Scenario 1: Calendar Conflict
#
# Two agents want to modify the same time slot. Intent marks prevent conflict.
# This is the minimal test of whether markspace works for coordination.
# ---------------------------------------------------------------------------


class TestCalendarConflict:
    """Two agents, one calendar slot, conflicting intentions."""

    @pytest.fixture
    def calendar_space(self) -> MarkSpace:
        scope = Scope(
            name="calendar",
            allowed_intent_verbs=("book", "reschedule", "cancel"),
            allowed_action_verbs=("booked", "rescheduled", "cancelled"),
            decay=DecayConfig(
                observation_half_life=hours(1),
                warning_half_life=hours(4),
                intent_ttl=minutes(30),
            ),
            conflict_policy=ConflictPolicy.HIGHEST_CONFIDENCE,
        )
        space = MarkSpace(scopes=[scope])
        space.set_clock(1_000_000.0)
        return space

    @pytest.fixture
    def booker(self) -> Agent:
        return Agent(
            name="flight-booker",
            scopes={
                "calendar": ["intent", "action", "observation"],
            },
        )

    @pytest.fixture
    def optimizer(self) -> Agent:
        return Agent(
            name="calendar-optimizer",
            scopes={
                "calendar": ["intent", "action", "observation"],
            },
        )

    def test_higher_confidence_wins(
        self,
        calendar_space: MarkSpace,
        booker: Agent,
        optimizer: Agent,
    ) -> None:
        """Booker (0.9 confidence) takes priority over optimizer (0.6)."""
        # Booker writes intent first
        calendar_space.write(
            booker,
            Intent(
                scope="calendar",
                resource="thu-14:00",
                action="book",
                confidence=0.9,
            ),
        )
        # Optimizer also wants the slot
        calendar_space.write(
            optimizer,
            Intent(
                scope="calendar",
                resource="thu-14:00",
                action="reschedule",
                confidence=0.6,
            ),
        )

        # Resolve conflict
        winner = calendar_space.check_conflict("calendar", "thu-14:00")
        intents = calendar_space.get_intents("calendar", "thu-14:00")
        winner_intent = next(i for i in intents if i.id == winner)

        assert winner_intent.confidence == 0.9
        assert winner_intent.agent_id == booker.id

    def test_action_supersedes_intent(
        self,
        calendar_space: MarkSpace,
        booker: Agent,
    ) -> None:
        """After execution, intent is replaced by action."""
        intent_id = calendar_space.write(
            booker,
            Intent(
                scope="calendar",
                resource="thu-14:00",
                action="book",
                confidence=0.9,
            ),
        )

        # Execute: write action that supersedes the intent
        calendar_space.write(
            booker,
            Action(
                scope="calendar",
                resource="thu-14:00",
                action="booked",
                result={"flight": "DL413"},
                supersedes=intent_id,
            ),
        )

        # Read the resource — should see action, not intent
        marks = calendar_space.read(scope="calendar", resource="thu-14:00")
        assert len(marks) == 1
        assert isinstance(marks[0], Action)
        assert marks[0].result == {"flight": "DL413"}

    def test_expired_intent_frees_slot(
        self,
        calendar_space: MarkSpace,
        booker: Agent,
        optimizer: Agent,
    ) -> None:
        """If booker's intent expires without action, optimizer can proceed."""
        calendar_space.write(
            booker,
            Intent(
                scope="calendar",
                resource="thu-14:00",
                action="book",
                confidence=0.9,
            ),
        )

        # Advance past intent TTL (30 min)
        calendar_space.set_clock(1_000_000.0 + minutes(31))

        # Booker's intent is expired — optimizer can now claim the slot
        intents = calendar_space.get_intents("calendar", "thu-14:00")
        assert len(intents) == 0

        # Optimizer writes new intent — no conflict
        calendar_space.write(
            optimizer,
            Intent(
                scope="calendar",
                resource="thu-14:00",
                action="reschedule",
                confidence=0.7,
            ),
        )
        winner = calendar_space.check_conflict("calendar", "thu-14:00")
        intents = calendar_space.get_intents("calendar", "thu-14:00")
        assert len(intents) == 1
        assert intents[0].agent_id == optimizer.id


# ---------------------------------------------------------------------------
# Scenario 2: Research Knowledge Sharing
#
# Multiple agents research the same topic. Observations are shared through
# marks. Fresh observations outweigh stale ones.
# ---------------------------------------------------------------------------


class TestResearchSharing:
    """Agents share research findings through observation marks."""

    @pytest.fixture
    def research_space(self) -> MarkSpace:
        scope = Scope(
            name="research",
            observation_topics=("*",),
            warning_topics=("*",),
            decay=DecayConfig(
                observation_half_life=hours(12),
                warning_half_life=hours(6),
                intent_ttl=hours(4),
            ),
        )
        space = MarkSpace(scopes=[scope])
        space.set_clock(1_000_000.0)
        return space

    @pytest.fixture
    def agent_a(self) -> Agent:
        return Agent(
            name="researcher-a",
            scopes={
                "research": ["observation", "warning"],
            },
        )

    @pytest.fixture
    def agent_b(self) -> Agent:
        return Agent(
            name="researcher-b",
            scopes={
                "research": ["observation", "warning"],
            },
        )

    def test_independent_observations_reinforce(
        self,
        research_space: MarkSpace,
        agent_a: Agent,
        agent_b: Agent,
    ) -> None:
        """Two agents observing the same fact produces stronger signal."""
        research_space.write(
            agent_a,
            Observation(
                scope="research",
                topic="acme-revenue",
                content="$10M",
                source=Source.FLEET,
                confidence=0.8,
            ),
        )
        research_space.write(
            agent_b,
            Observation(
                scope="research",
                topic="acme-revenue",
                content="$10M",
                source=Source.FLEET,
                confidence=0.7,
            ),
        )

        marks = research_space.read(scope="research", topic="acme-revenue")
        assert len(marks) == 2  # both visible

    def test_fresh_observation_outweighs_stale(
        self,
        research_space: MarkSpace,
        agent_a: Agent,
        agent_b: Agent,
    ) -> None:
        """After 12 hours, old observation at half strength. New observation is full."""
        research_space.write(
            agent_a,
            Observation(
                scope="research",
                topic="acme-status",
                content="independent",
                source=Source.FLEET,
                confidence=0.9,
            ),
        )

        # Advance 12 hours (one half-life)
        research_space.set_clock(1_000_000.0 + hours(12))

        research_space.write(
            agent_b,
            Observation(
                scope="research",
                topic="acme-status",
                content="merging",
                source=Source.FLEET,
                confidence=0.9,
            ),
        )

        marks = research_space.read(scope="research", topic="acme-status")
        assert len(marks) == 2
        # The fresh mark should be first (higher strength)
        assert isinstance(marks[0], Observation)
        assert marks[0].content == "merging"

    def test_warning_invalidates_stale_observation(
        self,
        research_space: MarkSpace,
        agent_a: Agent,
        agent_b: Agent,
    ) -> None:
        """Agent B discovers Agent A's observation is wrong, writes warning."""
        obs_id = research_space.write(
            agent_a,
            Observation(
                scope="research",
                topic="acme-ceo",
                content="John Smith",
                source=Source.FLEET,
                confidence=0.9,
            ),
        )

        # Agent B discovers the CEO changed
        research_space.write(
            agent_b,
            Warning(
                scope="research",
                invalidates=obs_id,
                topic="acme-ceo",
                reason="CEO resigned, new CEO is Jane Doe",
                severity=Severity.CRITICAL,
            ),
        )

        # Old observation should be suppressed
        marks = research_space.read(scope="research", topic="acme-ceo")
        obs_marks = [m for m in marks if isinstance(m, Observation)]
        assert len(obs_marks) == 0, "Invalidated observation should not be visible"


# ---------------------------------------------------------------------------
# Scenario 3: External Source Trust
#
# Fleet observations dominate external ones. Adversarial external observations
# are naturally downweighted.
# ---------------------------------------------------------------------------


class TestExternalTrust:
    """Trust weighting downweights external sources."""

    @pytest.fixture
    def intel_space(self) -> MarkSpace:
        scope = Scope(
            name="intel",
            observation_topics=("*",),
            decay=DecayConfig(
                observation_half_life=hours(6),
                warning_half_life=hours(2),
                intent_ttl=minutes(30),
            ),
        )
        space = MarkSpace(scopes=[scope])
        space.set_clock(1_000_000.0)
        return space

    @pytest.fixture
    def fleet_agent(self) -> Agent:
        return Agent(
            name="fleet-researcher",
            scopes={
                "intel": ["observation"],
            },
        )

    @pytest.fixture
    def external_agent(self) -> Agent:
        return Agent(
            name="external-scraper",
            scopes={
                "intel": ["observation"],
            },
        )

    def test_fleet_outranks_external(
        self,
        intel_space: MarkSpace,
        fleet_agent: Agent,
        external_agent: Agent,
    ) -> None:
        """Fleet observation at full weight, external at 0.3."""
        intel_space.write(
            fleet_agent,
            Observation(
                scope="intel",
                topic="market-signal",
                content="bullish",
                source=Source.FLEET,
                confidence=0.8,
            ),
        )
        intel_space.write(
            external_agent,
            Observation(
                scope="intel",
                topic="market-signal",
                content="bearish",
                source=Source.EXTERNAL_UNVERIFIED,
                confidence=0.9,
            ),
        )

        marks = intel_space.read(scope="intel", topic="market-signal")
        assert len(marks) == 2
        # Fleet mark should rank higher despite lower confidence
        assert isinstance(marks[0], Observation)
        assert marks[0].source == Source.FLEET

    def test_many_external_cant_overwhelm_fleet(
        self,
        intel_space: MarkSpace,
        fleet_agent: Agent,
        external_agent: Agent,
    ) -> None:
        """Even 5 external observations don't outrank 1 fleet observation."""
        intel_space.write(
            fleet_agent,
            Observation(
                scope="intel",
                topic="price",
                content="$100",
                source=Source.FLEET,
                confidence=0.8,
            ),
        )
        for i in range(5):
            intel_space.write(
                external_agent,
                Observation(
                    scope="intel",
                    topic="price",
                    content=f"${50 + i}",
                    source=Source.EXTERNAL_UNVERIFIED,
                    confidence=0.9,
                ),
            )

        marks = intel_space.read(scope="intel", topic="price")
        # Fleet mark should still be #1
        assert isinstance(marks[0], Observation)
        assert marks[0].source == Source.FLEET


# ---------------------------------------------------------------------------
# Scenario 4: Principal Attention
#
# Multiple agents generate need marks. Aggregator clusters and prioritizes.
# Principal responds with decision marks.
# ---------------------------------------------------------------------------


class TestPrincipalAttention:
    """Need marks accumulate, cluster, and get resolved."""

    @pytest.fixture
    def multi_scope_space(self) -> MarkSpace:
        deal_scope = Scope(
            name="deal",
            decay=DecayConfig(
                observation_half_life=hours(12),
                warning_half_life=hours(6),
                intent_ttl=hours(4),
            ),
        )
        email_scope = Scope(
            name="email",
            decay=DecayConfig(
                observation_half_life=hours(1),
                warning_half_life=hours(1),
                intent_ttl=minutes(10),
            ),
        )
        space = MarkSpace(scopes=[deal_scope, email_scope])
        space.set_clock(1_000_000.0)
        return space

    def test_needs_cluster_by_scope(self, multi_scope_space: MarkSpace) -> None:
        """Three deal needs cluster together, one email need separate."""
        agents = [
            Agent(
                name=f"agent-{i}",
                scopes={
                    "deal": ["need"],
                    "email": ["need"],
                },
            )
            for i in range(4)
        ]

        # Three agents need deal decisions
        for i in range(3):
            multi_scope_space.write(
                agents[i],
                Need(
                    scope="deal",
                    question="Continue deal X?",
                    context={"reason": f"concern-{i}"},
                    priority=0.6 + i * 0.1,
                    blocking=i < 2,  # first two are blocked
                ),
            )

        # One agent needs email approval
        multi_scope_space.write(
            agents[3],
            Need(
                scope="email",
                question="Approve draft?",
                context={"draft": "..."},
                priority=0.8,
                blocking=True,
            ),
        )

        clusters = multi_scope_space.aggregate_needs()
        assert len(clusters) == 2

        # Deal cluster should have higher effective priority (3 needs, density bonus)
        deal_cluster = next(c for c in clusters if c.scope == "deal")
        email_cluster = next(c for c in clusters if c.scope == "email")

        assert len(deal_cluster.needs) == 3
        assert deal_cluster.blocking_count == 2
        assert len(email_cluster.needs) == 1
        assert email_cluster.blocking_count == 1

    def test_resolved_needs_disappear_from_aggregation(
        self,
        multi_scope_space: MarkSpace,
    ) -> None:
        """Once the principal decides, needs are no longer aggregated."""
        agent = Agent(name="agent-0", scopes={"deal": ["need"]})
        need_id = multi_scope_space.write(
            agent,
            Need(
                scope="deal",
                question="Continue?",
                context=None,
                priority=0.8,
                blocking=True,
            ),
        )

        # Before resolution: 1 cluster
        assert len(multi_scope_space.aggregate_needs()) == 1

        # Principal decides - write a real action mark first
        resolver = Agent(name="resolver", scopes={"deal": ["action"]})
        action_id = multi_scope_space.write(
            resolver,
            Action(scope="deal", resource="resolve", action="decided"),
        )
        multi_scope_space.resolve(need_id, action_id)

        # After resolution: 0 clusters
        assert len(multi_scope_space.aggregate_needs()) == 0


# ---------------------------------------------------------------------------
# Scenario 5: Scope Isolation
#
# An agent authorized for research cannot write to calendar.
# Demonstrates that coordination boundaries are enforced.
# ---------------------------------------------------------------------------


class TestScopeIsolation:
    """Agents can only write to authorized scopes."""

    @pytest.fixture
    def multi_scope_space(self) -> MarkSpace:
        calendar = Scope(
            name="calendar",
            allowed_intent_verbs=("book",),
            decay=DecayConfig(
                observation_half_life=hours(1),
                warning_half_life=hours(1),
                intent_ttl=minutes(30),
            ),
        )
        research = Scope(
            name="research",
            observation_topics=("*",),
            decay=DecayConfig(
                observation_half_life=hours(12),
                warning_half_life=hours(6),
                intent_ttl=hours(4),
            ),
        )
        return MarkSpace(scopes=[calendar, research])

    def test_researcher_cannot_modify_calendar(
        self, multi_scope_space: MarkSpace
    ) -> None:
        researcher = Agent(
            name="researcher",
            scopes={
                "research": ["observation", "warning"],
            },
        )
        with pytest.raises(ScopeError):
            multi_scope_space.write(
                researcher,
                Intent(
                    scope="calendar",
                    resource="thu-14:00",
                    action="book",
                    confidence=0.5,
                ),
            )

    def test_researcher_can_read_calendar(self, multi_scope_space: MarkSpace) -> None:
        """Read is open — any agent can read any scope."""
        multi_scope_space.set_clock(1_000_000.0)
        booker = Agent(
            name="booker",
            scopes={
                "calendar": ["intent", "action"],
            },
        )
        multi_scope_space.write(
            booker,
            Intent(
                scope="calendar",
                resource="thu-14:00",
                action="book",
                confidence=0.9,
            ),
        )

        # Researcher can read calendar marks even without write permission
        marks = multi_scope_space.read(scope="calendar")
        assert len(marks) == 1


# ---------------------------------------------------------------------------
# Scenario 6: Full Lifecycle
#
# End-to-end: intent → conflict → resolution → action → observation →
# warning → need → decision. The complete mark lifecycle.
# ---------------------------------------------------------------------------


class TestFullLifecycle:
    """Complete lifecycle demonstrating all mark types and interactions."""

    def test_complete_workflow(self) -> None:
        """
        Story: Two agents coordinate a deal investigation.
        Agent A handles research, Agent B handles logistics.
        A discovers a problem, warns the fleet, both escalate to principal.
        """
        scope = Scope(
            name="deal/acme",
            allowed_intent_verbs=("investigate", "contact", "draft"),
            allowed_action_verbs=("investigated", "contacted", "drafted"),
            decay=DecayConfig(
                observation_half_life=hours(24),
                warning_half_life=hours(6),
                intent_ttl=hours(2),
            ),
            conflict_policy=ConflictPolicy.FIRST_WRITER,
        )
        space = MarkSpace(scopes=[scope])
        space.set_clock(1_000_000.0)

        researcher = Agent(
            name="researcher",
            scopes={
                "deal": ["intent", "action", "observation", "warning", "need"],
            },
        )
        logistics = Agent(
            name="logistics",
            scopes={
                "deal": ["intent", "action", "observation", "need"],
            },
        )

        # 1. Researcher declares intent to investigate
        intent_id = space.write(
            researcher,
            Intent(
                scope="deal/acme",
                resource="financials",
                action="investigate",
                confidence=0.8,
            ),
        )

        # 2. Logistics also wants to investigate — sees researcher's intent, yields
        existing = space.get_intents("deal/acme", "financials")
        assert len(existing) == 1
        # Logistics checks a different resource instead
        space.write(
            logistics,
            Intent(
                scope="deal/acme",
                resource="contracts",
                action="investigate",
                confidence=0.7,
            ),
        )

        # 3. Researcher completes, writes action + observation
        space.write(
            researcher,
            Action(
                scope="deal/acme",
                resource="financials",
                action="investigated",
                result={"status": "concerning", "debt_ratio": 3.2},
                supersedes=intent_id,
            ),
        )
        obs_id = space.write(
            researcher,
            Observation(
                scope="deal/acme",
                topic="financial-health",
                content={"debt_ratio": 3.2, "assessment": "high risk"},
                source=Source.FLEET,
                confidence=0.95,
            ),
        )

        # 4. Researcher discovers the situation is worse — writes warning
        space.write(
            researcher,
            Warning(
                scope="deal/acme",
                invalidates=obs_id,
                topic="financial-health",
                reason="Undisclosed liabilities found. Debt ratio is actually 5.1",
                severity=Severity.CRITICAL,
            ),
        )

        # 5. Both agents need principal input
        space.write(
            researcher,
            Need(
                scope="deal/acme",
                question="Should we abort the deal? Undisclosed liabilities found.",
                context={"debt_ratio": 5.1, "risk": "critical"},
                priority=0.95,
                blocking=True,
            ),
        )
        space.write(
            logistics,
            Need(
                scope="deal/acme",
                question="Should I continue contract review given financial concerns?",
                context={"depends_on": "financial assessment"},
                priority=0.7,
                blocking=True,
            ),
        )

        # 6. Principal sees aggregated needs
        clusters = space.aggregate_needs()
        assert len(clusters) == 1
        assert clusters[0].scope == "deal/acme"
        assert len(clusters[0].needs) == 2
        assert clusters[0].blocking_count == 2

        # 7. Verify the observation was invalidated by the warning
        obs_marks = space.read(
            scope="deal/acme",
            topic="financial-health",
            mark_type=MarkType.OBSERVATION,
        )
        assert len(obs_marks) == 0, "Observation should be invalidated by warning"

        # 8. Action mark persists (it's a fact — the investigation happened)
        action_marks = space.read(
            scope="deal/acme",
            resource="financials",
            mark_type=MarkType.ACTION,
        )
        assert len(action_marks) == 1
        assert isinstance(action_marks[0], Action)
        assert action_marks[0].result["debt_ratio"] == 3.2
