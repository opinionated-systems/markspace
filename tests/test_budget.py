# -*- coding: utf-8 -*-
"""
Tests for token budget tracking and guard enforcement.

P59: Budget Backward Compatibility
P60: Budget Warning Threshold
P61: Budget Hard Stop
P62: Budget Resumption
P63: Budget Tracking Accuracy
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
    hours,
    minutes,
)
from markspace.budget import BudgetStatus, BudgetTracker, TokenBudget
from markspace.schedule import Scheduler


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
def guard(space: MarkSpace) -> Guard:
    return Guard(space)


# ---------------------------------------------------------------------------
# TokenBudget data type
# ---------------------------------------------------------------------------


class TestTokenBudget:
    def test_all_fields_optional(self) -> None:
        budget = TokenBudget()
        assert budget.max_input_tokens_per_round is None
        assert budget.max_output_tokens_per_round is None
        assert budget.max_input_tokens_total is None
        assert budget.max_output_tokens_total is None

    def test_warning_fraction_default(self) -> None:
        budget = TokenBudget()
        assert budget.warning_fraction == pytest.approx(0.8)

    def test_warning_fraction_configurable(self) -> None:
        budget = TokenBudget(warning_fraction=0.5)
        assert budget.warning_fraction == 0.5

    def test_warning_fraction_bounds(self) -> None:
        with pytest.raises(Exception):
            TokenBudget(warning_fraction=0.0)
        with pytest.raises(Exception):
            TokenBudget(warning_fraction=1.0)
        with pytest.raises(Exception):
            TokenBudget(warning_fraction=-0.1)

    def test_frozen(self) -> None:
        budget = TokenBudget(max_input_tokens_total=1000)
        with pytest.raises(Exception):
            budget.max_input_tokens_total = 2000  # type: ignore[misc]


# ---------------------------------------------------------------------------
# BudgetTracker
# ---------------------------------------------------------------------------


class TestBudgetTracker:
    def test_initial_state_zero(self) -> None:
        tracker = BudgetTracker()
        assert tracker.total_input_consumed == 0
        assert tracker.total_output_consumed == 0
        assert not tracker.exhausted

    def test_record_input_monotonic(self) -> None:
        """P63: usage never decreases."""
        tracker = BudgetTracker()
        tracker.record_input(100)
        assert tracker.total_input_consumed == 100
        tracker.record_input(50)
        assert tracker.total_input_consumed == 150

    def test_record_output_monotonic(self) -> None:
        """P63: usage never decreases."""
        tracker = BudgetTracker()
        tracker.record_output(200)
        assert tracker.total_output_consumed == 200
        tracker.record_output(100)
        assert tracker.total_output_consumed == 300

    def test_negative_tokens_rejected(self) -> None:
        tracker = BudgetTracker()
        with pytest.raises(ValueError):
            tracker.record_input(-1)
        with pytest.raises(ValueError):
            tracker.record_output(-1)

    def test_warning_threshold_input(self) -> None:
        tracker = BudgetTracker()
        budget = TokenBudget(max_input_tokens_total=1000)
        tracker.record_input(799)
        assert tracker.check_lifetime(budget) == BudgetStatus.OK
        tracker.record_input(1)  # 800 = 80% of 1000
        assert tracker.check_lifetime(budget) == BudgetStatus.WARNING_INPUT

    def test_warning_threshold_output(self) -> None:
        tracker = BudgetTracker()
        budget = TokenBudget(max_output_tokens_total=500)
        tracker.record_output(399)
        assert tracker.check_lifetime(budget) == BudgetStatus.OK
        tracker.record_output(1)  # 400 = 80% of 500
        assert tracker.check_lifetime(budget) == BudgetStatus.WARNING_OUTPUT

    def test_warning_emitted_once(self) -> None:
        """P60: exactly one warning per dimension."""
        tracker = BudgetTracker()
        budget = TokenBudget(max_input_tokens_total=1000)
        tracker.record_input(800)
        assert tracker.check_lifetime(budget) == BudgetStatus.WARNING_INPUT
        # Mark as emitted
        tracker.warning_emitted_input = True
        tracker.record_input(50)
        # Should be OK now (warning already emitted), not WARNING again
        assert tracker.check_lifetime(budget) == BudgetStatus.OK

    def test_exhausted_at_limit(self) -> None:
        """P61: hard stop at 100%.

        When usage jumps past both warning and exhaustion in one call,
        warning fires first (P60). Exhaustion is detected on the next
        check after warning_emitted is set.
        """
        tracker = BudgetTracker()
        budget = TokenBudget(max_input_tokens_total=1000)
        tracker.record_input(1000)
        # First check: warning fires (P60 - warn before stop)
        assert tracker.check_lifetime(budget) == BudgetStatus.WARNING_INPUT
        tracker.warning_emitted_input = True
        # Second check: exhaustion detected
        assert tracker.check_lifetime(budget) == BudgetStatus.EXHAUSTED_INPUT
        assert tracker.exhausted

    def test_exhausted_deterministic(self) -> None:
        """P61: once exhausted, stays exhausted."""
        tracker = BudgetTracker()
        budget = TokenBudget(max_input_tokens_total=100)
        tracker.record_input(100)
        assert tracker.is_exhausted(budget)
        # Still exhausted even without further recording
        assert tracker.is_exhausted(budget)
        assert tracker.exhausted

    def test_custom_warning_fraction(self) -> None:
        tracker = BudgetTracker()
        budget = TokenBudget(max_input_tokens_total=1000, warning_fraction=0.5)
        tracker.record_input(499)
        assert tracker.check_lifetime(budget) == BudgetStatus.OK
        tracker.record_input(1)  # 500 = 50% of 1000
        assert tracker.check_lifetime(budget) == BudgetStatus.WARNING_INPUT

    def test_lifetime_accumulation(self) -> None:
        tracker = BudgetTracker()
        tracker.record_input(100)
        tracker.record_output(50)
        tracker.record_input(200)
        tracker.record_output(100)
        assert tracker.total_input_consumed == 300
        assert tracker.total_output_consumed == 150


# ---------------------------------------------------------------------------
# P59: Budget Backward Compatibility
# ---------------------------------------------------------------------------


class TestP59BackwardCompat:
    def test_no_budget_no_enforcement(self, guard: Guard) -> None:
        agent = Agent(
            name="no-budget",
            scopes={"test": ["observation"]},
        )
        status = guard.record_round_tokens(agent, 999999, 999999)
        assert status == BudgetStatus.OK

    def test_none_budget_same_as_missing(self, guard: Guard) -> None:
        agent = Agent(
            name="none-budget",
            scopes={"test": ["observation"]},
            manifest=AgentManifest(budget=None),
        )
        status = guard.record_round_tokens(agent, 999999, 999999)
        assert status == BudgetStatus.OK

    def test_activation_always_allowed_without_budget(self, guard: Guard) -> None:
        agent = Agent(
            name="no-budget",
            scopes={"test": ["observation"]},
        )
        result = guard.check_budget_activation(agent)
        assert result is None


# ---------------------------------------------------------------------------
# P60: Budget Warning
# ---------------------------------------------------------------------------


class TestP60BudgetWarning:
    def test_guard_writes_need_at_threshold(
        self, guard: Guard, space: MarkSpace
    ) -> None:
        budget = TokenBudget(max_input_tokens_total=1000)
        agent = Agent(
            name="budget-agent",
            scopes={"test": ["observation"]},
            manifest=AgentManifest(budget=budget),
        )
        # Record enough to cross 80% threshold
        status = guard.record_round_tokens(agent, 800, 0)
        assert status == BudgetStatus.WARNING_INPUT

        # Check that a Need mark was written
        needs = space.read(scope="_system", mark_type=MarkType.NEED)
        assert len(needs) >= 1
        budget_need = [
            n for n in needs if isinstance(n, Need) and "budget" in n.question.lower()
        ]
        assert len(budget_need) == 1

    def test_need_is_not_blocking(self, guard: Guard, space: MarkSpace) -> None:
        budget = TokenBudget(max_input_tokens_total=1000)
        agent = Agent(
            name="budget-agent",
            scopes={"test": ["observation"]},
            manifest=AgentManifest(budget=budget),
        )
        guard.record_round_tokens(agent, 800, 0)
        needs = space.read(scope="_system", mark_type=MarkType.NEED)
        budget_needs = [
            n for n in needs if isinstance(n, Need) and "budget" in n.question.lower()
        ]
        assert not budget_needs[0].blocking

    def test_agent_continues_after_warning(self, guard: Guard) -> None:
        budget = TokenBudget(max_input_tokens_total=1000)
        agent = Agent(
            name="budget-agent",
            scopes={"test": ["observation"]},
            manifest=AgentManifest(budget=budget),
        )
        guard.record_round_tokens(agent, 800, 0)
        # Agent should still be activatable
        result = guard.check_budget_activation(agent)
        assert result is None


# ---------------------------------------------------------------------------
# P61: Budget Hard Stop
# ---------------------------------------------------------------------------


class TestP61BudgetHardStop:
    def test_activation_rejected_at_100pct(self, guard: Guard) -> None:
        budget = TokenBudget(max_input_tokens_total=1000)
        agent = Agent(
            name="budget-agent",
            scopes={"test": ["observation"]},
            manifest=AgentManifest(budget=budget),
        )
        guard.record_round_tokens(agent, 1000, 0)
        result = guard.check_budget_activation(agent)
        assert result is not None
        assert "exhausted" in result

    def test_hard_stop_output_dimension(self, guard: Guard) -> None:
        budget = TokenBudget(max_output_tokens_total=500)
        agent = Agent(
            name="budget-agent",
            scopes={"test": ["observation"]},
            manifest=AgentManifest(budget=budget),
        )
        guard.record_round_tokens(agent, 0, 500)
        result = guard.check_budget_activation(agent)
        assert result is not None

    def test_scheduler_skips_exhausted_agent(self, guard: Guard) -> None:
        budget = TokenBudget(max_input_tokens_total=100)
        agent = Agent(
            name="budget-agent",
            scopes={"test": ["observation"]},
            manifest=AgentManifest(
                budget=budget,
                schedule_interval=1.0,
            ),
        )

        t = 1000.0
        scheduler = Scheduler(
            clock=lambda: t,
            pre_activation_check=guard.check_budget_activation,
        )
        scheduler.register(agent)

        # Before exhaustion: agent is due
        due = scheduler.tick_all()
        assert agent in due

        # Exhaust budget
        guard.record_round_tokens(agent, 100, 0)

        # After exhaustion: agent is not due
        t = 1002.0
        due = scheduler.tick_all()
        assert agent not in due


# ---------------------------------------------------------------------------
# P62: Budget Resumption
# ---------------------------------------------------------------------------


class TestP62BudgetResumption:
    def test_principal_increases_budget(self, guard: Guard) -> None:
        budget = TokenBudget(max_input_tokens_total=100)
        agent = Agent(
            name="budget-agent",
            scopes={"test": ["observation"]},
            manifest=AgentManifest(budget=budget),
        )
        guard.record_round_tokens(agent, 100, 0)
        assert guard.check_budget_activation(agent) is not None

        # Principal increases budget
        new_budget = TokenBudget(max_input_tokens_total=200)
        result = guard.update_budget(agent, new_budget, guard._principal_token)
        assert result is True

        # Create updated agent with new budget
        updated_agent = agent.model_copy(
            update={"manifest": AgentManifest(budget=new_budget)}
        )
        assert guard.check_budget_activation(updated_agent) is None

    def test_wrong_principal_rejected(self, guard: Guard) -> None:
        budget = TokenBudget(max_input_tokens_total=100)
        agent = Agent(
            name="budget-agent",
            scopes={"test": ["observation"]},
            manifest=AgentManifest(budget=budget),
        )
        new_budget = TokenBudget(max_input_tokens_total=200)
        result = guard.update_budget(agent, new_budget, uuid.uuid4())
        assert result is False

    def test_partial_increase_still_exhausted(self, guard: Guard) -> None:
        budget = TokenBudget(max_input_tokens_total=100)
        agent = Agent(
            name="budget-agent",
            scopes={"test": ["observation"]},
            manifest=AgentManifest(budget=budget),
        )
        guard.record_round_tokens(agent, 100, 0)

        # Increase to 100 (same as consumed) - still exhausted
        same_budget = TokenBudget(max_input_tokens_total=100)
        guard.update_budget(agent, same_budget, guard._principal_token)
        assert guard.check_budget_activation(agent) is not None


# ---------------------------------------------------------------------------
# P63: Tracking Accuracy
# ---------------------------------------------------------------------------


class TestBudgetJumpPastBoth:
    """Edge case: usage jumps past warning AND exhaustion in one call."""

    def test_warning_fires_before_hard_stop(
        self, guard: Guard, space: MarkSpace
    ) -> None:
        budget = TokenBudget(max_input_tokens_total=100, warning_fraction=0.5)
        agent = Agent(
            name="jumper",
            scopes={"test": ["observation"]},
            manifest=AgentManifest(budget=budget),
        )
        # Single round consumes entire budget
        status = guard.record_round_tokens(agent, 100, 0)
        # Warning fires first, not exhaustion
        assert status == BudgetStatus.WARNING_INPUT
        # Need mark was written
        needs = space.read(scope="_system", mark_type=MarkType.NEED)
        assert any(isinstance(n, Need) and "jumper" in n.question for n in needs)
        # But agent IS exhausted on next activation check
        assert guard.check_budget_activation(agent) is not None

    def test_gradual_approach_then_jump(self, guard: Guard) -> None:
        budget = TokenBudget(max_input_tokens_total=100)
        agent = Agent(
            name="gradual",
            scopes={"test": ["observation"]},
            manifest=AgentManifest(budget=budget),
        )
        # Round 1: below warning (80)
        status = guard.record_round_tokens(agent, 70, 0)
        assert status == BudgetStatus.OK
        # Round 2: jumps past warning AND exhaustion
        status = guard.record_round_tokens(agent, 40, 0)
        # Warning fires (total 110 >= 80 threshold, warning not yet emitted)
        assert status == BudgetStatus.WARNING_INPUT
        # Agent stopped on next check
        assert guard.check_budget_activation(agent) is not None


class TestBudgetStatusSnapshot:
    def test_returns_none_without_tracker(self, guard: Guard) -> None:
        result = guard.get_budget_status(uuid.uuid4())
        assert result is None

    def test_returns_immutable_snapshot(self, guard: Guard) -> None:
        budget = TokenBudget(max_input_tokens_total=1000)
        agent = Agent(
            name="budget-agent",
            scopes={"test": ["observation"]},
            manifest=AgentManifest(budget=budget),
        )
        guard.record_round_tokens(agent, 500, 100)
        result = guard.get_budget_status(agent.id)
        assert result is not None
        input_consumed, output_consumed, exhausted = result
        assert input_consumed == 500
        assert output_consumed == 100
        assert not exhausted


class TestReadTokenTruncation:
    """max_tokens parameter on MarkSpace.read() for per-round input budget."""

    def test_truncates_at_token_limit(self, space: MarkSpace) -> None:
        agent = Agent(
            name="writer",
            scopes={"test": ["observation"]},
        )
        # Write 10 observations
        for i in range(10):
            space.write(
                agent, Observation(scope="test", topic=f"t{i}", content=f"data-{i}")
            )
        # Read with no limit: all 10
        all_marks = space.read(scope="test")
        assert len(all_marks) == 10
        # Each mark is ~90 estimated tokens. 500 tokens fits ~5 marks.
        limited = space.read(scope="test", max_tokens=500)
        assert 1 <= len(limited) < len(all_marks)

    def test_no_limit_returns_all(self, space: MarkSpace) -> None:
        agent = Agent(
            name="writer",
            scopes={"test": ["observation"]},
        )
        for i in range(5):
            space.write(agent, Observation(scope="test", topic=f"t{i}"))
        assert len(space.read(scope="test", max_tokens=None)) == 5

    def test_strongest_marks_kept(self, space: MarkSpace) -> None:
        agent = Agent(
            name="writer",
            scopes={"test": ["observation"]},
        )
        # Write marks with different confidence (affects initial_strength indirectly
        # through confidence field, but strength is initial_strength * decay * trust).
        # All fleet source, same age, so strength ordering = insertion order (same strength).
        # The point: truncation preserves the order (strength-descending).
        for i in range(10):
            space.write(
                agent, Observation(scope="test", topic=f"t{i}", content="x" * 100)
            )
        limited = space.read(scope="test", max_tokens=200)
        all_marks = space.read(scope="test")
        # Truncated set is a prefix of the full set (same order)
        for i, m in enumerate(limited):
            assert m.id == all_marks[i].id


class TestP63TrackingAccuracy:
    def test_monotonic_input_tracking(self) -> None:
        tracker = BudgetTracker()
        values = []
        for _ in range(10):
            tracker.record_input(42)
            values.append(tracker.total_input_consumed)
        # Strictly increasing
        for i in range(1, len(values)):
            assert values[i] > values[i - 1]

    def test_monotonic_output_tracking(self) -> None:
        tracker = BudgetTracker()
        values = []
        for _ in range(10):
            tracker.record_output(37)
            values.append(tracker.total_output_consumed)
        for i in range(1, len(values)):
            assert values[i] > values[i - 1]
