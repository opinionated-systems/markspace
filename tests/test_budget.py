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

import threading
import uuid
from typing import Any

import pytest

from markspace import (
    Action,
    Agent,
    AgentManifest,
    ConflictPolicy,
    DecayConfig,
    Guard,
    MarkSpace,
    MarkType,
    Need,
    Observation,
    QuotaExceededError,
    Scope,
    ScopeError,
    WriteBudget,
    hours,
    minutes,
)
from markspace.budget import BudgetStatus, BudgetTracker, TokenBudget
from markspace.schedule import Scheduler
from markspace.telemetry import METRIC_BUDGET_REMAINING, FailingSink, InMemorySink


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


def _scope_for_limit() -> Scope:
    return Scope(
        name="test",
        decay=DecayConfig(
            observation_half_life=hours(6),
            warning_half_life=hours(2),
            intent_ttl=minutes(30),
        ),
    )


def _manifest(total: int) -> AgentManifest:
    return AgentManifest(budget=TokenBudget(max_input_tokens_total=total))


def _with_budget(total: int) -> Agent:
    return Agent(
        name="budget-agent",
        scopes={"test": ["observation"]},
        manifest=_manifest(total),
    )


def _dimension_budget(
    dimension: str, total: int, warning_fraction: float = 4 / 5
) -> TokenBudget:
    if dimension == "input":
        return TokenBudget(
            max_input_tokens_total=total, warning_fraction=warning_fraction
        )
    return TokenBudget(max_output_tokens_total=total, warning_fraction=warning_fraction)


def _with_dimension_budget(
    dimension: str, total: int, warning_fraction: float = 4 / 5
) -> Agent:
    return Agent(
        name="budget-agent",
        scopes={"test": ["observation"]},
        manifest=AgentManifest(
            budget=_dimension_budget(dimension, total, warning_fraction)
        ),
    )


def _record(guard: Guard, agent: Agent, dimension: str, tokens: int) -> None:
    if dimension == "input":
        guard.record_round_tokens(agent, tokens, 0)
    else:
        guard.record_round_tokens(agent, 0, tokens)


def _budget_warnings(space: MarkSpace) -> list[tuple[str, int, int]]:
    """The budget warnings ever written as (dimension, usage, total), sorted. A
    warning that a stop superseded or the principal answered still counts."""
    return sorted(
        (m.context["dimension"], m.context["consumed"], m.context["total"])
        for m in space._marks.values()
        if isinstance(m, Need)
        and m.resolved_by is None
        and m.context["level"] == "warning"
    )


def _open_needs(space: MarkSpace) -> list[tuple[str, str, int, int]]:
    """What the principal sees now: (level, dimension, usage, total), sorted."""
    return sorted(
        (
            n.context["level"],
            n.context["dimension"],
            n.context["consumed"],
            n.context["total"],
        )
        for c in space.aggregate_needs()
        for n in c.needs
    )


def _actions(space: MarkSpace) -> list[Action]:
    return [m for m in space._marks.values() if isinstance(m, Action)]


def _remaining(sink: InMemorySink, agent: Agent) -> dict[str, float]:
    """The last remaining-budget gauge of each dimension."""
    return {
        labels["dimension"]: value
        for name, value, labels in sink.gauges
        if name == METRIC_BUDGET_REMAINING and labels["agent_id"] == str(agent.id)
    }


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

    def test_a_raise_resumes_the_same_agent(self, guard: Guard) -> None:
        agent = _with_budget(100)
        guard.record_round_tokens(agent, 100, 0)
        raised = TokenBudget(max_input_tokens_total=200)
        assert guard.update_budget(agent, raised, guard._principal_token) is True
        assert guard.check_budget_activation(agent) is None

    def test_a_lowered_budget_applies_to_the_same_agent(self, guard: Guard) -> None:
        agent = _with_budget(1000)
        guard.record_round_tokens(agent, 100, 0)
        assert guard.check_budget_activation(agent) is None
        lowered = TokenBudget(max_input_tokens_total=50)
        assert guard.update_budget(agent, lowered, guard._principal_token) is True
        assert guard.check_budget_activation(agent) is not None

    def test_a_check_of_the_old_agent_does_not_stop_it_again(
        self, guard: Guard
    ) -> None:
        old = _with_budget(100)
        guard.record_round_tokens(old, 100, 0)
        raised = TokenBudget(max_input_tokens_total=1000)
        new = old.model_copy(update={"manifest": AgentManifest(budget=raised)})
        guard.update_budget(old, raised, guard._principal_token)
        assert guard.check_budget_activation(old) is None
        assert guard.check_budget_activation(new) is None

    def test_the_stored_budget_takes_precedence_over_any_manifest(
        self, guard: Guard
    ) -> None:
        agent = _with_budget(100)
        guard.record_round_tokens(agent, 60, 0)
        lowered = TokenBudget(max_input_tokens_total=50)
        guard.update_budget(agent, lowered, guard._principal_token)
        generous = agent.model_copy(update={"manifest": _manifest(1000)})
        assert guard.check_budget_activation(generous) is not None
        assert guard.record_round_tokens(generous, 1, 0) is BudgetStatus.WARNING_INPUT

    def test_a_wrong_token_changes_no_budget(self, guard: Guard) -> None:
        agent = _with_budget(100)
        guard.record_round_tokens(agent, 100, 0)
        raised = TokenBudget(max_input_tokens_total=200)
        assert guard.update_budget(agent, raised, uuid.uuid4()) is False
        assert guard.check_budget_activation(agent) is not None
        spare = _with_budget(1000)
        guard.record_round_tokens(spare, 100, 0)
        lowered = TokenBudget(max_input_tokens_total=50)
        assert guard.update_budget(spare, lowered, uuid.uuid4()) is False
        assert guard.check_budget_activation(spare) is None
        fresh = _with_budget(1000)
        zero = TokenBudget(max_input_tokens_total=0)
        assert guard.update_budget(fresh, zero, uuid.uuid4()) is False
        assert guard.get_budget_status(fresh.id) is None
        assert guard.check_budget_activation(fresh) is None

    @pytest.mark.parametrize("dimension", ["input", "output"])
    def test_a_raise_warns_again_at_the_new_line(
        self, guard: Guard, space: MarkSpace, dimension: str
    ) -> None:
        agent = _with_dimension_budget(dimension, 100)
        _record(guard, agent, dimension, 90)
        assert _budget_warnings(space) == [(dimension, 90, 100)]
        guard.update_budget(
            agent, _dimension_budget(dimension, 1000), guard._principal_token
        )
        _record(guard, agent, dimension, 10)
        assert _budget_warnings(space) == [(dimension, 90, 100)]
        _record(guard, agent, dimension, 800)
        assert _budget_warnings(space) == [
            (dimension, 90, 100),
            (dimension, 900, 1000),
        ]
        _record(guard, agent, dimension, 10)
        assert len(_budget_warnings(space)) == 2

    @pytest.mark.parametrize("dimension", ["input", "output"])
    def test_usage_above_the_new_line_does_not_repeat_a_warning(
        self, guard: Guard, space: MarkSpace, dimension: str
    ) -> None:
        agent = _with_dimension_budget(dimension, 100)
        _record(guard, agent, dimension, 90)
        guard.update_budget(
            agent, _dimension_budget(dimension, 110), guard._principal_token
        )
        _record(guard, agent, dimension, 1)
        assert _budget_warnings(space) == [(dimension, 90, 100)]

    @pytest.mark.parametrize("dimension", ["input", "output"])
    def test_usage_on_the_new_line_has_crossed_it(
        self, guard: Guard, space: MarkSpace, dimension: str
    ) -> None:
        agent = _with_dimension_budget(dimension, 100)
        _record(guard, agent, dimension, 90)
        guard.update_budget(
            agent,
            _dimension_budget(dimension, 180, warning_fraction=0.5),
            guard._principal_token,
        )
        _record(guard, agent, dimension, 0)
        assert _budget_warnings(space) == [(dimension, 90, 100)]

    @pytest.mark.parametrize("dimension", ["input", "output"])
    def test_the_rearm_uses_the_new_budgets_fraction(
        self, guard: Guard, space: MarkSpace, dimension: str
    ) -> None:
        budget = _dimension_budget(dimension, 100, warning_fraction=0.5)
        agent = _with_dimension_budget(dimension, 100, warning_fraction=0.5)
        _record(guard, agent, dimension, 60)
        guard.update_budget(agent, budget, guard._principal_token)
        _record(guard, agent, dimension, 1)
        assert _budget_warnings(space) == [(dimension, 60, 100)]

    @pytest.mark.parametrize("dimension", ["input", "output"])
    def test_a_removed_limit_warns_again_under_a_later_limit(
        self, guard: Guard, space: MarkSpace, dimension: str
    ) -> None:
        agent = _with_dimension_budget(dimension, 100)
        _record(guard, agent, dimension, 90)
        guard.update_budget(agent, TokenBudget(), guard._principal_token)
        guard.update_budget(
            agent, _dimension_budget(dimension, 100), guard._principal_token
        )
        _record(guard, agent, dimension, 1)
        assert _budget_warnings(space) == [
            (dimension, 90, 100),
            (dimension, 91, 100),
        ]

    @pytest.mark.parametrize(
        ("raised", "other"), [("input", "output"), ("output", "input")]
    )
    def test_each_dimension_rearms_alone(
        self, guard: Guard, space: MarkSpace, raised: str, other: str
    ) -> None:
        budget = TokenBudget(max_input_tokens_total=100, max_output_tokens_total=100)
        agent = Agent(
            name="budget-agent",
            scopes={"test": ["observation"]},
            manifest=AgentManifest(budget=budget),
        )
        _record(guard, agent, "input", 90)
        _record(guard, agent, "output", 90)
        new_budget = TokenBudget(
            max_input_tokens_total=1000 if raised == "input" else 100,
            max_output_tokens_total=1000 if raised == "output" else 100,
        )
        guard.update_budget(agent, new_budget, guard._principal_token)
        _record(guard, agent, other, 1)
        _record(guard, agent, raised, 800)
        assert _budget_warnings(space) == sorted(
            [(raised, 90, 100), (other, 90, 100), (raised, 890, 1000)]
        )

    def test_the_call_writes_no_warning(self, guard: Guard, space: MarkSpace) -> None:
        agent = _with_budget(1000)
        guard.record_round_tokens(agent, 100, 0)
        lowered = TokenBudget(max_input_tokens_total=120)
        guard.update_budget(agent, lowered, guard._principal_token)
        assert _budget_warnings(space) == []
        guard.record_round_tokens(agent, 1, 0)
        assert _budget_warnings(space) == [("input", 101, 120)]

    @pytest.mark.parametrize("dimension", ["input", "output"])
    def test_a_warned_agent_stopped_by_the_call_is_not_warned_again(
        self, guard: Guard, space: MarkSpace, dimension: str
    ) -> None:
        agent = _with_dimension_budget(dimension, 100)
        _record(guard, agent, dimension, 90)
        guard.update_budget(
            agent, _dimension_budget(dimension, 90), guard._principal_token
        )
        _record(guard, agent, dimension, 0)
        assert _budget_warnings(space) == [(dimension, 90, 100)]

    def test_the_warning_fraction_is_replaced_too(
        self, guard: Guard, space: MarkSpace
    ) -> None:
        agent = _with_dimension_budget("input", 100, warning_fraction=0.5)
        guard.record_round_tokens(agent, 10, 0)
        raised = TokenBudget(max_input_tokens_total=1000)
        guard.update_budget(agent, raised, guard._principal_token)
        guard.record_round_tokens(agent, 590, 0)
        assert _budget_warnings(space) == []
        guard.record_round_tokens(agent, 200, 0)
        assert _budget_warnings(space) == [("input", 800, 1000)]

    def test_a_budget_with_no_totals_removes_the_limits_and_resumes(
        self, guard: Guard
    ) -> None:
        agent = _with_budget(100)
        guard.record_round_tokens(agent, 100, 0)
        assert guard.check_budget_activation(agent) is not None
        only_a_round_limit = TokenBudget(max_input_tokens_per_round=10)
        guard.update_budget(agent, only_a_round_limit, guard._principal_token)
        guard.record_round_tokens(agent, 1_000_000, 1_000_000)
        assert guard.check_budget_activation(agent) is None

    @pytest.mark.parametrize("dimension", ["input", "output"])
    def test_the_guard_applies_only_the_totals(
        self, guard: Guard, dimension: str
    ) -> None:
        agent = _with_dimension_budget(dimension, 100)
        budget = TokenBudget(
            max_input_tokens_per_round=10,
            max_output_tokens_per_round=10,
            max_input_tokens_total=1000 if dimension == "input" else None,
            max_output_tokens_total=1000 if dimension == "output" else None,
        )
        assert guard.update_budget(agent, budget, guard._principal_token) is True
        _record(guard, agent, dimension, 500)
        assert guard.check_budget_activation(agent) is None

    def test_a_manifest_total_of_zero_gets_one_round(self, guard: Guard) -> None:
        agent = _with_budget(0)
        assert guard.check_budget_activation(agent) is None
        assert guard.get_budget_status(agent.id) is None
        guard.record_round_tokens(agent, 5, 0)
        assert guard.check_budget_activation(agent) is not None

    def test_get_token_budget_returns_what_the_principal_set(
        self, guard: Guard
    ) -> None:
        agent = _with_budget(100)
        assert guard.get_token_budget(agent.id) is None
        budget = TokenBudget(max_input_tokens_total=50)
        assert guard.update_budget(agent, budget, uuid.uuid4()) is False
        assert guard.get_token_budget(agent.id) is None
        guard.update_budget(agent, budget, guard._principal_token)
        assert guard.get_token_budget(agent.id) == budget

    def test_a_new_budget_replaces_the_whole_budget(
        self, guard: Guard, space: MarkSpace
    ) -> None:
        budget = TokenBudget(
            max_input_tokens_total=100,
            max_output_tokens_total=100,
            warning_fraction=0.5,
        )
        agent = Agent(
            name="budget-agent",
            scopes={"test": ["observation"]},
            manifest=AgentManifest(budget=budget),
        )
        guard.record_round_tokens(agent, 100, 0)
        raised = TokenBudget(max_input_tokens_total=200)
        guard.update_budget(agent, raised, guard._principal_token)
        guard.record_round_tokens(agent, 0, 100_000)
        assert guard.check_budget_activation(agent) is None
        assert _budget_warnings(space) == []

    def test_a_total_of_zero_from_the_call_stops_an_agent_before_its_first_round(
        self, guard: Guard
    ) -> None:
        agent = _with_budget(1000)
        zero = TokenBudget(max_input_tokens_total=0)
        guard.update_budget(agent, zero, guard._principal_token)
        assert guard.check_budget_activation(agent) is not None
        assert guard.get_budget_status(agent.id) == (0, 0, True)

    def test_a_lowered_budget_shows_in_the_status_at_once(self, guard: Guard) -> None:
        agent = _with_budget(1000)
        guard.record_round_tokens(agent, 100, 0)
        assert guard.get_budget_status(agent.id) == (100, 0, False)
        lowered = TokenBudget(max_input_tokens_total=50)
        guard.update_budget(agent, lowered, guard._principal_token)
        assert guard.get_budget_status(agent.id) == (100, 0, True)

    def test_the_gauge_follows_the_stored_budget(self, space: MarkSpace) -> None:
        sink = InMemorySink()
        guard = Guard(space, telemetry=sink)
        budget = TokenBudget(max_input_tokens_total=1000, max_output_tokens_total=1000)
        agent = Agent(
            name="budget-agent",
            scopes={"test": ["observation"]},
            manifest=AgentManifest(budget=budget),
        )
        guard.record_round_tokens(agent, 100, 20)
        lowered = TokenBudget(max_input_tokens_total=150, max_output_tokens_total=70)
        guard.update_budget(agent, lowered, guard._principal_token)
        assert _remaining(sink, agent) == {"input": 50, "output": 50}
        guard.record_round_tokens(agent, 10, 30)
        assert _remaining(sink, agent) == {"input": 40, "output": 20}

    def test_a_removed_limit_gets_no_gauge(self, space: MarkSpace) -> None:
        sink = InMemorySink()
        guard = Guard(space, telemetry=sink)
        agent = _with_budget(100)
        guard.record_round_tokens(agent, 10, 0)
        before = len(sink.gauges)
        guard.update_budget(agent, TokenBudget(), guard._principal_token)
        guard.record_round_tokens(agent, 10, 0)
        assert len(sink.gauges) == before

    def test_a_failing_sink_does_not_stop_the_call(self, space: MarkSpace) -> None:
        guard = Guard(space, telemetry=FailingSink())
        agent = _with_budget(100)
        budget = TokenBudget(max_input_tokens_total=50)
        assert guard.update_budget(agent, budget, guard._principal_token) is True
        assert guard.get_token_budget(agent.id) == budget

    def test_every_use_of_the_stored_budgets_holds_the_lock(self, guard: Guard) -> None:
        """A check that read the budget before the lock could use a budget that
        update_budget had just replaced, and a store after the lock could let a
        check stop the agent that the call had resumed."""

        class LockChecked(dict[uuid.UUID, TokenBudget]):
            uses = 0

            def _held(self) -> None:
                assert guard._budget_lock.locked()
                LockChecked.uses += 1

            def get(self, *args: Any, **kwargs: Any) -> Any:
                self._held()
                return super().get(*args, **kwargs)

            def __getitem__(self, key: uuid.UUID) -> TokenBudget:
                self._held()
                return super().__getitem__(key)

            def __setitem__(self, key: uuid.UUID, value: TokenBudget) -> None:
                self._held()
                super().__setitem__(key, value)

            def __contains__(self, key: object) -> bool:
                self._held()
                return super().__contains__(key)

        class LockCheckedTracker(BudgetTracker):
            """A budget read under the lock and used after it would fail here."""

            def is_exhausted(self, budget: TokenBudget) -> bool:
                assert guard._budget_lock.locked()
                return super().is_exhausted(budget)

            def check_lifetime(self, budget: TokenBudget) -> BudgetStatus:
                assert guard._budget_lock.locked()
                return super().check_lifetime(budget)

            def try_clear_exhaustion(self, budget: TokenBudget) -> bool:
                assert guard._budget_lock.locked()
                return super().try_clear_exhaustion(budget)

            def rearm_warnings(self, budget: TokenBudget) -> None:
                assert guard._budget_lock.locked()
                super().rearm_warnings(budget)

            def take_new_stops(self, budget: TokenBudget) -> list[tuple[str, int, int]]:
                assert guard._budget_lock.locked()
                return super().take_new_stops(budget)

        guard._token_budgets = LockChecked()
        agent = _with_budget(100)
        guard._budget_trackers[agent.id] = LockCheckedTracker()
        budget = TokenBudget(max_input_tokens_total=1000)
        for use, call in enumerate(
            (
                lambda: guard.check_budget_activation(agent),
                lambda: guard.record_round_tokens(agent, 1, 0),
                lambda: guard.update_budget(agent, budget, guard._principal_token),
                lambda: guard.get_token_budget(agent.id),
            ),
            start=1,
        ):
            before = LockChecked.uses
            call()
            assert LockChecked.uses > before, f"call {use} used no stored budget"

    def test_a_budget_for_an_agent_without_one_counts_from_the_call(
        self, guard: Guard
    ) -> None:
        agent = Agent(name="no-budget", scopes={"test": ["observation"]})
        guard.record_round_tokens(agent, 500, 0)
        assert guard.check_budget_activation(agent) is None
        budget = TokenBudget(max_input_tokens_total=100)
        guard.update_budget(agent, budget, guard._principal_token)
        assert guard.check_budget_activation(agent) is None
        guard.record_round_tokens(agent, 100, 0)
        assert guard.check_budget_activation(agent) is not None


class TestBudgetNeedsShowTheCurrentState:
    """P60, P61, P62: a stop shows in the principal's needs, and the principal's
    call answers the needs that it makes out of date."""

    def _answer(self, space: MarkSpace, guard: Guard) -> None:
        principal = Agent(name="principal", scopes={"test": ["action"]})
        action = space.write(
            principal, Action(scope="test", resource="b", action="booked", result="ok")
        )
        for cluster in space.aggregate_needs():
            for need in cluster.needs:
                space.resolve(need.id, action)

    def test_a_raise_answers_the_open_warning(
        self, guard: Guard, space: MarkSpace
    ) -> None:
        agent = _with_budget(100)
        guard.record_round_tokens(agent, 90, 0)
        assert _open_needs(space) == [("warning", "input", 90, 100)]
        assert space.aggregate_needs()[0].needs[0].priority == 0.7
        raised = TokenBudget(max_input_tokens_total=1000)
        guard.update_budget(agent, raised, guard._principal_token)
        assert guard.check_budget_activation(agent) is None
        assert _open_needs(space) == []
        resolved = [
            m for m in space._marks.values() if isinstance(m, Need) and m.resolved_by
        ]
        assert len(resolved) == 1
        assert resolved[0].resolved_by_agent == guard.system_agent_id
        assert resolved[0].resolved_by is not None
        action = space.get_mark(resolved[0].resolved_by)
        assert isinstance(action, Action)
        assert (action.scope, action.action) == ("_system", "set_token_budget")
        assert action.resource == str(agent.id)
        assert action.result == {
            "agent_id": str(agent.id),
            "max_input_tokens_total": 1000,
            "max_output_tokens_total": None,
            "warning_fraction": raised.warning_fraction,
            "input_consumed": 90,
            "output_consumed": 0,
        }

    def test_each_call_is_recorded_also_with_no_open_need(
        self, guard: Guard, space: MarkSpace
    ) -> None:
        agent = _with_budget(100)
        for total in (200, 300):
            budget = TokenBudget(max_input_tokens_total=total)
            guard.update_budget(agent, budget, guard._principal_token)
        assert [a.result["max_input_tokens_total"] for a in _actions(space)] == [
            200,
            300,
        ]

    def test_a_wrong_token_records_nothing_and_answers_nothing(
        self, guard: Guard, space: MarkSpace
    ) -> None:
        agent = _with_budget(100)
        guard.record_round_tokens(agent, 90, 0)
        raised = TokenBudget(max_input_tokens_total=1000)
        assert guard.update_budget(agent, raised, uuid.uuid4()) is False
        assert _open_needs(space) == [("warning", "input", 90, 100)]
        assert _actions(space) == []

    @pytest.mark.parametrize("dimension", ["input", "output"])
    def test_a_call_that_stops_the_agent_shows_in_the_needs(
        self, guard: Guard, space: MarkSpace, dimension: str
    ) -> None:
        agent = _with_dimension_budget(dimension, 1000)
        _record(guard, agent, dimension, 100)
        assert _open_needs(space) == []
        guard.update_budget(
            agent, _dimension_budget(dimension, 50), guard._principal_token
        )
        assert guard.check_budget_activation(agent) is not None
        assert _open_needs(space) == [("stopped", dimension, 100, 50)]
        need = space.aggregate_needs()[0].needs[0]
        assert need.blocking is False
        assert need.priority == 0.9
        assert need.agent_id == guard.system_agent_id
        assert need.supersedes is None
        assert f"{100}/{50} {dimension} tokens" in need.question

    @pytest.mark.parametrize("dimension", ["input", "output"])
    def test_a_call_that_leaves_the_agent_stopped_shows_the_new_numbers(
        self, guard: Guard, space: MarkSpace, dimension: str
    ) -> None:
        agent = _with_dimension_budget(dimension, 100)
        _record(guard, agent, dimension, 100)
        guard.check_budget_activation(agent)
        assert _open_needs(space) == [("stopped", dimension, 100, 100)]
        guard.update_budget(
            agent, _dimension_budget(dimension, 40), guard._principal_token
        )
        assert _open_needs(space) == [("stopped", dimension, 100, 40)]

    def test_a_total_of_zero_from_the_call_shows_in_the_needs(
        self, guard: Guard, space: MarkSpace
    ) -> None:
        agent = _with_budget(1000)
        zero = TokenBudget(max_input_tokens_total=0)
        guard.update_budget(agent, zero, guard._principal_token)
        assert _open_needs(space) == [("stopped", "input", 0, 0)]

    @pytest.mark.parametrize("dimension", ["input", "output"])
    def test_a_stop_by_usage_supersedes_the_open_warning(
        self, guard: Guard, space: MarkSpace, dimension: str
    ) -> None:
        agent = _with_dimension_budget(dimension, 100)
        _record(guard, agent, dimension, 90)
        assert _open_needs(space) == [("warning", dimension, 90, 100)]
        _record(guard, agent, dimension, 10)
        assert _open_needs(space) == [("stopped", dimension, 100, 100)]
        stopped = space.aggregate_needs()[0].needs[0]
        assert stopped.supersedes is not None
        superseded = space.get_mark(stopped.supersedes)
        assert isinstance(superseded, Need)
        assert superseded.context["level"] == "warning"
        assert not space.is_current(superseded.id)

    def test_a_stop_by_usage_after_an_answered_warning_shows(
        self, guard: Guard, space: MarkSpace
    ) -> None:
        agent = _with_budget(100)
        guard.record_round_tokens(agent, 90, 0)
        self._answer(space, guard)
        assert _open_needs(space) == []
        guard.record_round_tokens(agent, 10, 0)
        assert _open_needs(space) == [("stopped", "input", 100, 100)]
        assert space.aggregate_needs()[0].needs[0].supersedes is None

    def test_one_round_past_both_lines_in_both_dimensions_shows_two_stops(
        self, guard: Guard, space: MarkSpace
    ) -> None:
        budget = TokenBudget(max_input_tokens_total=100, max_output_tokens_total=100)
        agent = Agent(
            name="budget-agent",
            scopes={"test": ["observation"]},
            manifest=AgentManifest(budget=budget),
        )
        guard.record_round_tokens(agent, 100, 100)
        guard.record_round_tokens(agent, 0, 0)
        assert _budget_warnings(space) == []
        assert _open_needs(space) == [
            ("stopped", "input", 100, 100),
            ("stopped", "output", 100, 100),
        ]

    @pytest.mark.parametrize("dimension", ["input", "output"])
    def test_a_stopped_agent_is_never_asked_to_continue(
        self, guard: Guard, space: MarkSpace, dimension: str
    ) -> None:
        agent = _with_dimension_budget(dimension, 1000)
        _record(guard, agent, dimension, 100)
        guard.update_budget(
            agent, _dimension_budget(dimension, 50), guard._principal_token
        )
        self._answer(space, guard)
        _record(guard, agent, dimension, 1)
        assert _budget_warnings(space) == []
        assert _open_needs(space) == []

    def test_one_round_past_both_lines_shows_only_the_stop(
        self, guard: Guard, space: MarkSpace
    ) -> None:
        agent = _with_budget(100)
        status = guard.record_round_tokens(agent, 100, 0)
        assert status == BudgetStatus.WARNING_INPUT
        assert _budget_warnings(space) == []
        assert _open_needs(space) == [("stopped", "input", 100, 100)]
        assert guard.get_budget_status(agent.id) == (100, 0, True)

    @pytest.mark.parametrize("dimension", ["input", "output"])
    def test_a_stop_is_written_once(
        self, guard: Guard, space: MarkSpace, dimension: str
    ) -> None:
        agent = _with_dimension_budget(dimension, 100)
        _record(guard, agent, dimension, 100)
        for _ in range(3):
            assert guard.check_budget_activation(agent) is not None
            _record(guard, agent, dimension, 1)
        assert _open_needs(space) == [("stopped", dimension, 100, 100)]

    def test_an_answered_stop_is_not_raised_again(
        self, guard: Guard, space: MarkSpace
    ) -> None:
        agent = _with_budget(100)
        guard.record_round_tokens(agent, 100, 0)
        self._answer(space, guard)
        assert guard.check_budget_activation(agent) is not None
        guard.record_round_tokens(agent, 1, 0)
        assert _open_needs(space) == []

    def test_a_raise_answers_the_stop_and_a_later_stop_shows_again(
        self, guard: Guard, space: MarkSpace
    ) -> None:
        agent = _with_budget(100)
        guard.record_round_tokens(agent, 100, 0)
        raised = TokenBudget(max_input_tokens_total=1000)
        guard.update_budget(agent, raised, guard._principal_token)
        assert guard.check_budget_activation(agent) is None
        assert _open_needs(space) == []
        guard.record_round_tokens(agent, 900, 0)
        assert _open_needs(space) == [("stopped", "input", 1000, 1000)]

    def test_a_lower_manifest_writes_no_need_until_a_round(
        self, guard: Guard, space: MarkSpace
    ) -> None:
        agent = _with_budget(1000)
        guard.record_round_tokens(agent, 100, 0)
        lower = agent.model_copy(update={"manifest": _manifest(50)})
        assert guard.check_budget_activation(lower) is not None
        assert _open_needs(space) == []
        guard.record_round_tokens(lower, 1, 0)
        assert _open_needs(space) == [("stopped", "input", 101, 50)]

    def test_a_check_writes_no_mark(self, guard: Guard, space: MarkSpace) -> None:
        agent = _with_budget(1000)
        guard.record_round_tokens(agent, 100, 0)
        lower = agent.model_copy(update={"manifest": _manifest(50)})
        marks = len(space._marks)
        guard.check_budget_activation(lower)
        guard.check_budget_activation(agent)
        assert len(space._marks) == marks

    def test_a_round_during_a_call_keeps_its_stop_in_the_needs(
        self, guard: Guard, space: MarkSpace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A round that reaches the raised total while the call closes the old
        needs: its stop must not be closed with them."""
        agent = _with_budget(100)
        guard.record_round_tokens(agent, 10, 0)
        close = guard._close_principal_needs
        round_ = threading.Thread(
            target=guard.record_round_tokens, args=(agent, 990, 0), daemon=True
        )

        def close_with_a_round_in_flight(*args: Any) -> None:
            round_.start()
            round_.join(timeout=0.2)
            close(*args)

        monkeypatch.setattr(
            guard, "_close_principal_needs", close_with_a_round_in_flight
        )
        raised = TokenBudget(max_input_tokens_total=1000)
        guard.update_budget(agent, raised, guard._principal_token)
        round_.join(timeout=5)
        assert not round_.is_alive()
        assert guard.check_budget_activation(agent) is not None
        assert _open_needs(space) == [("stopped", "input", 1000, 1000)]

    def test_a_call_during_a_round_leaves_no_false_stop(
        self, guard: Guard, space: MarkSpace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A raise while a round that reached the old total writes its stop: the
        agent runs, so the needs must not show it stopped."""
        agent = _with_budget(100)
        note = guard._note_budget_stops
        raised = TokenBudget(max_input_tokens_total=1000)
        call = threading.Thread(
            target=guard.update_budget,
            args=(agent, raised, guard._principal_token),
            daemon=True,
        )

        def note_with_a_call_in_flight(*args: Any) -> None:
            if not call.is_alive() and call.ident is None:
                call.start()
                call.join(timeout=0.2)
            note(*args)

        monkeypatch.setattr(guard, "_note_budget_stops", note_with_a_call_in_flight)
        guard.record_round_tokens(agent, 100, 0)
        call.join(timeout=5)
        assert not call.is_alive()
        assert guard.check_budget_activation(agent) is None
        assert _open_needs(space) == []

    def test_two_calls_leave_the_needs_of_the_last_budget(
        self, guard: Guard, space: MarkSpace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        agent = _with_budget(1000)
        guard.record_round_tokens(agent, 100, 0)
        close = guard._close_principal_needs
        raised = TokenBudget(max_input_tokens_total=1000)
        second = threading.Thread(
            target=guard.update_budget,
            args=(agent, raised, guard._principal_token),
            daemon=True,
        )

        def close_with_a_call_in_flight(*args: Any) -> None:
            if second.ident is None:
                second.start()
                second.join(timeout=0.2)
            close(*args)

        monkeypatch.setattr(
            guard, "_close_principal_needs", close_with_a_call_in_flight
        )
        lowered = TokenBudget(max_input_tokens_total=50)
        guard.update_budget(agent, lowered, guard._principal_token)
        second.join(timeout=5)
        assert not second.is_alive()
        assert guard.get_token_budget(agent.id) == raised
        assert guard.check_budget_activation(agent) is None
        assert _open_needs(space) == []
        assert [a.result["max_input_tokens_total"] for a in _actions(space)] == [
            50,
            1000,
        ]

    def test_hooks_on_budget_marks_run_with_the_guard_locks_released(
        self, guard: Guard, space: MarkSpace
    ) -> None:
        agent = _with_budget(1000)
        other = _with_budget(100)
        seen: list[bool] = []

        def hook(_agent_id: uuid.UUID, mark: Any) -> None:
            if isinstance(mark, Action) or (
                isinstance(mark, Need) and mark.context["agent_id"] == str(agent.id)
            ):
                thread = threading.Thread(
                    target=guard.record_round_tokens,
                    args=(other, 1, 0),
                    daemon=True,
                )
                thread.start()
                thread.join(timeout=2)
                seen.append(not thread.is_alive())

        space.add_write_hook(hook)
        guard.record_round_tokens(agent, 900, 0)  # a warning
        lowered = TokenBudget(max_input_tokens_total=50)
        guard.update_budget(agent, lowered, guard._principal_token)  # an action, a stop
        assert seen == [True, True, True]

    def test_a_call_leaves_write_budget_needs_open(self, space: MarkSpace) -> None:
        agent = Agent(
            name="writer",
            scopes={"test": ["observation"]},
            manifest=AgentManifest(budget=TokenBudget(max_input_tokens_total=100)),
        )
        guard = Guard(space, write_budgets={agent.id: WriteBudget(per_hour={})})
        with pytest.raises(ScopeError):
            guard.write_mark(agent, Observation(scope="test", topic="t", content="x"))
        guard.record_round_tokens(agent, 90, 0)
        raised = TokenBudget(max_input_tokens_total=1000)
        guard.update_budget(agent, raised, guard._principal_token)
        levels = [n.context["level"] for c in space.aggregate_needs() for n in c.needs]
        assert levels == ["exhausted"]
        guard.record_round_tokens(agent, 900, 0)
        assert guard.set_write_budget(agent.id, None, guard._principal_token)
        levels = [n.context["level"] for c in space.aggregate_needs() for n in c.needs]
        assert levels == ["warning"]

    def test_an_ordinary_agents_action_counts_against_the_mark_limit(self) -> None:
        space = MarkSpace(
            scopes=[_scope_for_limit()], clock=1000.0, max_marks_per_agent=1
        )
        Guard(space)
        agent = Agent(name="writer", scopes={"test": ["action"]})
        space.write(
            agent, Action(scope="test", resource="a", action="used", result="ok")
        )
        with pytest.raises(QuotaExceededError):
            space.write(
                agent, Action(scope="test", resource="b", action="used", result="ok")
            )

    def test_a_stop_in_one_dimension_leaves_the_others_warning_open(
        self, guard: Guard, space: MarkSpace
    ) -> None:
        budget = TokenBudget(max_input_tokens_total=100, max_output_tokens_total=100)
        agent = Agent(
            name="budget-agent",
            scopes={"test": ["observation"]},
            manifest=AgentManifest(budget=budget),
        )
        guard.record_round_tokens(agent, 90, 0)
        guard.record_round_tokens(agent, 0, 90)
        guard.record_round_tokens(agent, 10, 0)
        assert _open_needs(space) == [
            ("stopped", "input", 100, 100),
            ("warning", "output", 90, 100),
        ]

    def test_a_call_answers_the_other_agents_needs_not_at_all(
        self, guard: Guard, space: MarkSpace
    ) -> None:
        agent = _with_budget(100)
        other = _with_budget(100)
        guard.record_round_tokens(agent, 90, 0)
        guard.record_round_tokens(other, 90, 0)
        raised = TokenBudget(max_input_tokens_total=1000)
        guard.update_budget(other, raised, guard._principal_token)
        assert _open_needs(space) == [("warning", "input", 90, 100)]

    def test_a_need_the_principal_answered_first_does_not_fail_the_call(
        self, guard: Guard, space: MarkSpace, caplog: pytest.LogCaptureFixture
    ) -> None:
        agent = _with_budget(100)
        guard.record_round_tokens(agent, 90, 0)
        self._answer(space, guard)
        raised = TokenBudget(max_input_tokens_total=1000)
        with caplog.at_level("WARNING", logger="markspace.guard"):
            assert guard.update_budget(agent, raised, guard._principal_token) is True
        assert "Failed to record" not in caplog.text
        assert len(_actions(space)) == 2  # the principal's and the guard's

    def test_needs_and_actions_are_written_outside_the_budget_lock(
        self, guard: Guard, space: MarkSpace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_write = space.write
        writes = []
        under_the_lock = []

        def checked_write(agent: Agent, mark: Any) -> uuid.UUID:
            # An assertion here would go to the guard's logged failure path.
            if guard._budget_lock.locked():
                under_the_lock.append(mark)
            writes.append(type(mark).__name__)
            return real_write(agent, mark)

        monkeypatch.setattr(space, "write", checked_write)
        agent = _with_budget(100)
        guard.record_round_tokens(agent, 90, 0)
        guard.record_round_tokens(agent, 10, 0)
        lower = agent.model_copy(update={"manifest": _manifest(50)})
        guard.check_budget_activation(lower)
        lowered = TokenBudget(max_input_tokens_total=40)
        guard.update_budget(agent, lowered, guard._principal_token)
        assert writes == ["Need", "Need", "Action", "Need"]
        assert under_the_lock == []

    def test_the_action_mark_takes_no_share_of_the_mark_limit(self) -> None:
        space = MarkSpace(
            scopes=[_scope_for_limit()],
            clock=1000.0,
            max_marks_per_agent=1,
        )
        guard = Guard(space)
        agent = _with_budget(100)
        for total in (200, 300, 400):
            budget = TokenBudget(max_input_tokens_total=total)
            guard.update_budget(agent, budget, guard._principal_token)
        assert len(_actions(space)) == 3
        assert space._agent_mark_counts.get(guard.system_agent_id, 0) == 0

    def test_a_refused_action_mark_is_logged_and_the_budget_applies(
        self,
        guard: Guard,
        space: MarkSpace,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        agent = _with_budget(100)
        guard.record_round_tokens(agent, 90, 0)
        real_write = space.write

        def refuse_actions(writer: Agent, mark: Any) -> uuid.UUID:
            if isinstance(mark, Action):
                raise ValueError("refused")
            return real_write(writer, mark)

        monkeypatch.setattr(space, "write", refuse_actions)
        lowered = TokenBudget(max_input_tokens_total=95)
        with caplog.at_level("WARNING", logger="markspace.guard"):
            assert guard.update_budget(agent, lowered, guard._principal_token) is True
        assert "Failed to record" in caplog.text
        assert guard.get_token_budget(agent.id) == lowered
        assert _open_needs(space) == [("warning", "input", 90, 100)]
        guard.record_round_tokens(agent, 5, 0)
        assert _open_needs(space) == [("stopped", "input", 95, 95)]


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
