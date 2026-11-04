# -*- coding: utf-8 -*-
"""
Tests for write budgets (Spec Section 9.14).

P77: Write Budget Exhaustion - a spent budget refuses further writes of the type
P78: Write Budget Room - a write within the budget is never refused by it
P79: Write Budget Alerts - burn-rate tiers write need marks and refuse nothing
"""

from __future__ import annotations

import math
import threading
import uuid
from typing import Any

import pytest
from pydantic import ValidationError as PydanticValidationError

from markspace import (
    Action,
    Agent,
    AgentManifest,
    ConflictPolicy,
    ContentPolicy,
    ContentPolicyError,
    DecayConfig,
    Guard,
    GuardVerdict,
    Intent,
    MarkSpace,
    MarkType,
    Need,
    Observation,
    PolicyResult,
    PolicyVerdict,
    Scope,
    minutes,
)
from markspace.guard import ClaimState, NotGrantedError
from markspace.rate_limit import ScopeRateLimit
from markspace.space import QuotaExceededError, ScopeError
from markspace.telemetry import METRIC_LEASES_EXPIRED, InMemorySink
from markspace.write_budget import (
    DAY,
    DEFAULT_TIERS,
    Burn,
    BurnTier,
    WriteBudget,
    WriteBudgetMeter,
    _Times,
)

T0 = 1_000_000.0
O, W, I, A = (
    MarkType.OBSERVATION,
    MarkType.WARNING,
    MarkType.INTENT,
    MarkType.ACTION,
)

# One write per hour over a one-day period: a budget of 24, no alert tiers.
DAILY = WriteBudget(per_hour={O: 1.0}, period_seconds=DAY, tiers=())


def write(
    meter: WriteBudgetMeter,
    agent: uuid.UUID,
    budget: WriteBudget,
    at: float,
    mark_type: MarkType = O,
) -> tuple[bool, tuple[Burn, ...]]:
    """Reserve and confirm one write; (refused, tiers that started)."""
    r = meter.reserve(agent, budget, mark_type, at)
    if r.refused:
        return True, ()
    return False, meter.confirm(agent, budget, mark_type, at)


def scope(
    name: str, rate_limit: ScopeRateLimit | None = None, deferred: bool = False
) -> Scope:
    return Scope(
        name=name,
        decay=DecayConfig(
            observation_half_life=minutes(60),
            warning_half_life=minutes(60),
            intent_ttl=minutes(30),
        ),
        rate_limit=rate_limit,
        deferred=deferred,
    )


def space_and_guard(
    budget: WriteBudget | None,
) -> tuple[MarkSpace, Guard, Agent, uuid.UUID]:
    space = MarkSpace(scopes=[scope("a"), scope("b")])
    space.set_clock(T0)
    token = uuid.uuid4()
    agent = Agent(
        name="w",
        scopes={
            n: ["observation", "warning", "need", "intent", "action"] for n in "ab"
        },
    )
    guard = Guard(
        space,
        principal_token=token,
        write_budgets={agent.id: budget} if budget else None,
    )
    return space, guard, agent, token


def observe(guard: Guard, agent: Agent, scope_name: str = "a") -> None:
    guard.write_mark(agent, Observation(scope=scope_name, topic="t", content="c"))


def counted(
    guard: Guard, agent: Agent, mark_type: MarkType, at: float, period: float
) -> int:
    times = guard._write_budget_meter._times.get((agent.id, mark_type))
    return times.count_since(at - period) if times else 0


def counted_meter(
    meter: WriteBudgetMeter, agent: uuid.UUID, mark_type: MarkType, at: float
) -> int:
    times = meter._times.get((agent, mark_type))
    return times.count_since(at - DAY) if times else 0


def system_needs(space: MarkSpace) -> list[Need]:
    return [
        n
        for n in space.read(scope="_system", mark_type=MarkType.NEED)
        if isinstance(n, Need)
    ]


def levels(space: MarkSpace) -> list[str]:
    return sorted(str(n.context["level"]) for n in system_needs(space))


class TestWriteBudgetConfig:
    def test_default_tiers_are_the_workbook_values(self) -> None:
        budget = WriteBudget(per_hour={W: 12.0})
        fast, slow, ticket = DEFAULT_TIERS
        assert (fast.level, slow.level, ticket.level) == ("page", "page", "ticket")
        assert budget.period_seconds == 30 * DAY
        assert budget.budget(W) == pytest.approx(8640.0)
        # burn rates 14.4, 6 and 1 over 30 days, as in the workbook
        assert budget.threshold(fast, W, 3600) == pytest.approx(172.8)
        assert budget.threshold(fast, W, 300) == pytest.approx(14.4)
        assert budget.threshold(slow, W, 6 * 3600) == pytest.approx(432.0)
        assert budget.threshold(slow, W, 1800) == pytest.approx(36.0)
        assert budget.threshold(ticket, W, 3 * DAY) == pytest.approx(864.0)
        assert budget.threshold(ticket, W, 6 * 3600) == pytest.approx(72.0)

    def test_an_unlisted_type_has_a_budget_of_zero(self) -> None:
        assert WriteBudget(per_hour={O: 5.0}).budget(W) == 0.0

    @pytest.mark.parametrize("rate", [-1.0, math.nan, math.inf])
    def test_bad_rates_are_refused(self, rate: float) -> None:
        with pytest.raises(PydanticValidationError):
            WriteBudget(per_hour={O: rate})

    @pytest.mark.parametrize(
        "long, short, fraction",
        [(300, 300, 0.1), (3600, 300, 1.5), (math.inf, 300, 0.1)],
    )
    def test_bad_tiers_are_refused(
        self, long: float, short: float, fraction: float
    ) -> None:
        with pytest.raises(PydanticValidationError):
            BurnTier(
                level="page",
                long_window_seconds=long,
                short_window_seconds=short,
                budget_fraction=fraction,
            )

    def test_bad_periods_are_refused(self) -> None:
        with pytest.raises(PydanticValidationError):
            WriteBudget(per_hour={O: 1.0}, period_seconds=math.inf)
        with pytest.raises(PydanticValidationError):
            WriteBudget(per_hour={O: 1.0}, period_seconds=3600)  # 6 h tier

    def test_an_invalid_manifest_rate_is_refused_at_construction(self) -> None:
        with pytest.raises(PydanticValidationError):
            AgentManifest(expected_activity={"observations": 4.0})
        with pytest.raises(PydanticValidationError):
            AgentManifest(expected_activity={"observation": -1.0})

    def test_from_expected_activity_doubles_the_rate(self) -> None:
        budget = WriteBudget.from_expected_activity({O: 3.0})
        assert budget.per_hour == {O: 6.0}
        assert budget.tiers == DEFAULT_TIERS


class TestP77WriteBudgetExhaustion:
    def test_the_write_over_the_budget_is_refused_and_not_counted(self) -> None:
        meter = WriteBudgetMeter()
        agent = uuid.uuid4()
        for k in range(24):
            assert not write(meter, agent, DAILY, T0 + k)[0]
        refused = meter.reserve(agent, DAILY, O, T0 + 24)
        assert refused.refused and refused.period_count == 24 and refused.at is None
        for k in range(100):  # refused writes do not count
            assert write(meter, agent, DAILY, T0 + 25 + k)[0]
        # back within the budget as the trailing period moves past T0
        assert not write(meter, agent, DAILY, T0 + DAY)[0]
        assert write(meter, agent, DAILY, T0 + DAY)[0]

    def test_a_released_write_is_not_counted(self) -> None:
        meter = WriteBudgetMeter()
        agent = uuid.uuid4()
        for k in range(23):
            write(meter, agent, DAILY, T0 + k)
        r = meter.reserve(agent, DAILY, O, T0 + 30)
        assert not r.refused and r.at is not None
        meter.release(agent, O, r.at)
        assert not meter.spent(agent, DAILY, O, T0 + 31)

    def test_the_trailing_period_is_half_open(self) -> None:
        meter = WriteBudgetMeter()
        agent = uuid.uuid4()
        for _ in range(24):
            write(meter, agent, DAILY, T0)
        assert write(meter, agent, DAILY, T0 + DAY - 1)[0]
        assert not write(meter, agent, DAILY, T0 + DAY)[0]

    def test_an_unlisted_type_is_refused_at_its_first_write(self) -> None:
        assert write(WriteBudgetMeter(), uuid.uuid4(), DAILY, T0, W)[0]

    def test_force_counts_a_write_over_the_budget(self) -> None:
        meter = WriteBudgetMeter()
        agent = uuid.uuid4()
        for k in range(24):
            write(meter, agent, DAILY, T0 + k)
        forced = meter.reserve(agent, DAILY, O, T0 + 24, force=True)
        assert forced.refused is False and forced.period_count == 25
        assert write(meter, agent, DAILY, T0 + DAY)[0]  # 24 left in the period

    def test_an_exhaustion_starts_once_per_episode(self) -> None:
        meter = WriteBudgetMeter()
        agent = uuid.uuid4()
        for k in range(24):
            write(meter, agent, DAILY, T0 + k)
        assert meter.reserve(agent, DAILY, O, T0 + 24).exhaustion_started
        assert not meter.reserve(agent, DAILY, O, T0 + 25).exhaustion_started
        assert not write(meter, agent, DAILY, T0 + DAY)[0]
        assert meter.reserve(agent, DAILY, O, T0 + DAY).exhaustion_started

    def test_meters_are_per_agent_and_per_type(self) -> None:
        budget = WriteBudget(per_hour={O: 1.0, W: 1.0}, period_seconds=DAY, tiers=())
        meter = WriteBudgetMeter()
        a, b = uuid.uuid4(), uuid.uuid4()
        for k in range(24):
            write(meter, a, budget, T0 + k)
        assert not write(meter, b, budget, T0 + 24)[0]
        assert not write(meter, a, budget, T0 + 24, W)[0]
        assert write(meter, a, budget, T0 + 24)[0]

    def test_spent_counts_nothing(self) -> None:
        meter = WriteBudgetMeter()
        agent = uuid.uuid4()
        for k in range(23):
            write(meter, agent, DAILY, T0 + k)
        for _ in range(5):
            assert not meter.spent(agent, DAILY, O, T0 + 30)
        write(meter, agent, DAILY, T0 + 30)
        assert meter.spent(agent, DAILY, O, T0 + 31)

    def test_write_mark_refuses_and_writes_one_need(self) -> None:
        space, guard, agent, _ = space_and_guard(DAILY)
        for _ in range(24):
            observe(guard, agent)
        for name in ("a", "b", "a"):
            with pytest.raises(ScopeError, match="spent its write budget"):
                observe(guard, agent, name)
        assert levels(space) == ["exhausted"]
        assert system_needs(space)[0].priority == 0.9
        # need is not listed in DAILY: its budget is 0
        with pytest.raises(ScopeError, match="spent its write budget for need"):
            guard.write_mark(
                agent,
                Need(scope="a", question="q", context={}, priority=0.5, blocking=False),
            )
        # each type has its own open need
        assert sorted(str(n.context["mark_type"]) for n in system_needs(space)) == [
            "need",
            "observation",
        ]

    def test_one_open_need_while_the_agent_hovers_at_its_budget(self) -> None:
        """Each old write that leaves the period lets one write in and ends the
        episode; while the first need mark is open, no second one is written.
        After the principal resolves it, the next exhaustion writes one."""
        space, guard, agent, _ = space_and_guard(DAILY)
        for k in range(24):
            space.set_clock(T0 + k)
            observe(guard, agent)
        for k in range(24):
            space.set_clock(T0 + DAY + k)
            observe(guard, agent)  # one slot frees each second
            with pytest.raises(ScopeError):
                observe(guard, agent)
        assert levels(space) == ["exhausted"]
        need = system_needs(space)[0]
        principal = Agent(name="p", scopes={"_system": ["action"]})
        action_id = space.write(
            principal,
            Action(scope="_system", resource="n", action="seen", result="ok"),
        )
        space.resolve(need.id, action_id)
        space.set_clock(T0 + 3 * DAY)
        for _ in range(25):
            try:
                observe(guard, agent)
            except ScopeError:
                pass
        current = system_needs(space)
        assert [n.context["level"] for n in current] == ["exhausted"]
        assert current[0].id != need.id

    def test_writes_refused_by_other_checks_are_not_counted(self) -> None:
        """Refused by a scope rate limit, and by the space's quota: neither
        counts against the write budget."""
        space = MarkSpace(
            scopes=[scope("s", ScopeRateLimit(max_writes_per_agent_per_window=2))],
            max_marks_per_agent=4,
        )
        space.set_clock(T0)
        agent = Agent(name="r", scopes={"s": ["observation"]})
        budget = WriteBudget(per_hour={O: 9.0 / 720}, tiers=())
        guard = Guard(space, write_budgets={agent.id: budget})
        for k in range(6):
            space.set_clock(T0 + 301 * k)
            for _ in range(3):  # the third is refused by the rate limit
                try:
                    observe(guard, agent, "s")
                except (ScopeError, QuotaExceededError):
                    pass
        assert counted(guard, agent, O, T0 + 2000, 30 * DAY) == 4  # the quota stored 4

    def test_a_write_the_content_policy_refuses_is_not_counted(self) -> None:
        class Reject(ContentPolicy):
            def evaluate(
                self, agent: Agent, mark: Observation, scope: Scope
            ) -> PolicyResult:
                return PolicyResult(verdict=PolicyVerdict.REJECT, reason="no")

        space = MarkSpace(scopes=[scope("a")])
        space.set_clock(T0)
        agent = Agent(name="c", scopes={"a": ["observation"]})
        guard = Guard(
            space, write_budgets={agent.id: DAILY}, content_policies=[Reject()]
        )
        for _ in range(30):
            with pytest.raises(ContentPolicyError):
                observe(guard, agent)
        assert counted(guard, agent, O, T0, DAY) == 0
        assert system_needs(space) == []

    def test_an_intent_refused_by_a_held_resource_is_not_counted(self) -> None:
        budget = WriteBudget(per_hour={I: 2.0 / 720, A: 100.0}, tiers=())
        _, guard, agent, _ = space_and_guard(budget)
        other = Agent(name="o", scopes={"a": ["intent", "action"]})
        guard.post_action(other, "a", "r1", "booked", "ok")  # other holds r1
        for _ in range(10):
            decision = guard.pre_action(agent, "a", "r1", "book")
            assert decision.verdict == GuardVerdict.CONFLICT
        assert counted(guard, agent, I, T0, 30 * DAY) == 0
        assert guard.pre_action(agent, "a", "r2", "book").verdict == GuardVerdict.ALLOW

    def test_an_intent_the_space_refuses_is_not_counted(self) -> None:
        space = MarkSpace(scopes=[scope("a")], max_marks_per_agent=3)
        space.set_clock(T0)
        agent = Agent(name="q", scopes={"a": ["intent", "action"]})
        budget = WriteBudget(per_hour={I: 100.0, A: 100.0}, tiers=())
        guard = Guard(space, write_budgets={agent.id: budget})
        for k in range(10):
            try:
                guard.pre_action(agent, "a", f"r{k}", "book")
            except QuotaExceededError:
                pass
        assert counted(guard, agent, I, T0, 30 * DAY) == 3  # the quota stored 3

    def test_a_stored_intent_counts_whatever_the_decision(self) -> None:
        """An intent that the space stores counts once, also when another
        agent's lease beats it, and in a deferred scope, where it waits."""
        space = MarkSpace(scopes=[scope("a"), scope("d", deferred=True)])
        space.set_clock(T0)
        agent = Agent(name="w", scopes={n: ["intent", "action"] for n in "ad"})
        other = Agent(name="o", scopes={"a": ["intent", "action"]})
        budget = WriteBudget(
            per_hour={I: 100.0, A: 100.0}, period_seconds=DAY, tiers=()
        )
        guard = Guard(space, write_budgets={agent.id: budget})
        assert guard.pre_action(other, "a", "r1", "book").verdict == GuardVerdict.ALLOW
        beaten = guard.pre_action(agent, "a", "r1", "book")
        assert beaten.verdict != GuardVerdict.ALLOW and beaten.intent_id is not None
        waiting = guard.pre_action(agent, "d", "r2", "book")
        assert waiting.verdict == GuardVerdict.BLOCKED
        assert counted(guard, agent, I, T0, DAY) == 2

    def test_pre_action_refuses_a_spent_intent_budget(self) -> None:
        budget = WriteBudget(per_hour={I: 1.0, A: 100.0}, period_seconds=DAY, tiers=())
        _, guard, agent, _ = space_and_guard(budget)
        for k in range(24):
            decision = guard.pre_action(agent, "a", f"r{k}", "book")
            assert decision.verdict == GuardVerdict.ALLOW
        decision = guard.pre_action(agent, "a", "r24", "book")
        assert decision.verdict == GuardVerdict.DENIED
        assert "spent its write budget for intent" in decision.reason

    def test_a_spent_action_budget_refuses_intents_and_writes_a_need(self) -> None:
        budget = WriteBudget(per_hour={I: 100.0, A: 1.0}, period_seconds=DAY, tiers=())
        space, guard, agent, _ = space_and_guard(budget)
        for k in range(24):
            guard.post_action(agent, "a", f"r{k}", "booked", "ok")
        for k in range(30):
            decision = guard.pre_action(agent, "b", f"s{k}", "book")
            assert decision.verdict == GuardVerdict.DENIED
            assert "spent its write budget for action" in decision.reason
        assert counted(guard, agent, I, T0, DAY) == 0
        assert [n.context["mark_type"] for n in system_needs(space)] == ["action"]

    def test_post_action_records_over_the_budget_and_counts_it(self) -> None:
        space, guard, agent, _ = space_and_guard(
            WriteBudget(per_hour={A: 1.0}, period_seconds=DAY, tiers=())
        )
        for k in range(26):
            guard.post_action(agent, "a", f"r{k}", "booked", "ok")
        assert len(space.read(scope="a", mark_type=A)) == 26
        assert counted(guard, agent, A, T0, DAY) == 26
        assert levels(space) == ["exhausted"]

    def test_a_refused_post_action_is_not_counted(self) -> None:
        _, guard, agent, _ = space_and_guard(
            WriteBudget(per_hour={I: 100.0, A: 100.0}, period_seconds=DAY, tiers=())
        )
        other = Agent(name="o", scopes={"a": ["intent", "action"]})
        guard.post_action(other, "a", "r1", "booked", "ok")
        with pytest.raises(NotGrantedError):
            guard.post_action(agent, "a", "r1", "booked", "ok")
        assert counted(guard, agent, A, T0, DAY) == 0

    def test_execute_counts_one_action_per_tool_run(self) -> None:
        """The tool releases its own lease, so post_action is refused (not
        counted) and execute records the lost lease as a failed action
        (counted): one tool run, one count."""
        budget = WriteBudget(
            per_hour={I: 100.0, A: 100.0}, period_seconds=DAY, tiers=()
        )
        space, guard, agent, token = space_and_guard(budget)
        released: list[bool] = []

        def tool(fencing_token: object) -> str:
            intents = space.read(scope="a", mark_type=I)
            released.append(guard.release(intents[0].id, token))
            return "ok"

        decision, _ = guard.execute(agent, "a", "r1", "book", "booked", tool_fn=tool)
        assert released == [True] and decision.verdict == GuardVerdict.CONFLICT
        assert counted(guard, agent, A, T0, DAY) == 1

    def test_unauthorized_writes_are_not_metered(self) -> None:
        """A write the agent's permissions refuse is not counted and starts
        no exhaustion, also with a budget of 0."""
        space, guard, _, token = space_and_guard(None)
        agent = Agent(name="u", scopes={"a": ["observation"]})
        budget = WriteBudget(per_hour={}, period_seconds=DAY, tiers=())
        assert guard.set_write_budget(agent.id, budget, token)
        for k in range(30):
            with pytest.raises(ScopeError, match="not authorized"):
                observe(guard, agent, "b")
            with pytest.raises(ScopeError, match="not authorized"):
                guard.post_action(agent, "a", f"r{k}", "booked", "ok")
        assert counted(guard, agent, O, T0, DAY) == 0
        assert counted(guard, agent, A, T0, DAY) == 0
        assert system_needs(space) == []

    def test_concurrent_writers_never_exceed_the_budget(self) -> None:
        _, guard, agent, _ = space_and_guard(
            WriteBudget(per_hour={O: 100.0 / 24}, period_seconds=DAY, tiers=())
        )
        stored: list[int] = []

        def worker() -> None:
            n = 0
            for _ in range(50):
                try:
                    observe(guard, agent)
                    n += 1
                except ScopeError:
                    pass
            stored.append(n)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        assert sum(stored) == 100

    def test_a_reservation_checks_and_counts_under_one_lock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The first writer waits after counting until a second writer has
        counted too, or until a timeout. Under the guard's lock the second
        cannot count meanwhile, so a budget of one admits one write."""
        _, guard, agent, _ = space_and_guard(
            WriteBudget(per_hour={O: 1.0 / 24}, period_seconds=DAY, tiers=())
        )
        both_counted = threading.Barrier(2, timeout=0.3)
        count_since = _Times.count_since

        def count_then_wait(times: _Times, after: float) -> int:
            n = count_since(times, after)
            try:
                both_counted.wait()
            except threading.BrokenBarrierError:
                pass
            return n

        monkeypatch.setattr(_Times, "count_since", count_then_wait)
        stored: list[bool] = []

        def worker() -> None:
            try:
                observe(guard, agent)
                stored.append(True)
            except ScopeError:
                stored.append(False)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        assert sorted(stored) == [False, True]

    def test_set_write_budget_clears_the_count_and_needs_the_token(self) -> None:
        _, guard, agent, token = space_and_guard(DAILY)
        for _ in range(24):
            observe(guard, agent)
        assert not guard.set_write_budget(agent.id, DAILY, uuid.uuid4())
        with pytest.raises(ScopeError):
            observe(guard, agent)
        assert guard.set_write_budget(agent.id, DAILY, token)
        observe(guard, agent)  # a new budget starts a new count
        assert guard.set_write_budget(agent.id, None, token)
        for _ in range(100):
            observe(guard, agent)  # no budget and no manifest: not metered

    def test_set_write_budget_keeps_other_agents_state(self) -> None:
        space, guard, agent, token = space_and_guard(DAILY)
        other = Agent(name="o", scopes={"a": ["observation"]})
        assert guard.set_write_budget(other.id, DAILY, token)
        for k in range(24):
            space.set_clock(T0 + k)
            observe(guard, other)
        with pytest.raises(ScopeError):
            observe(guard, other)  # an exhaustion: one need mark
        assert guard.set_write_budget(agent.id, DAILY, token)
        with pytest.raises(ScopeError):
            observe(guard, other)  # the other agent's count is kept
        space.set_clock(T0 + DAY + 0.5)
        observe(guard, other)  # its first write has left the period
        with pytest.raises(ScopeError):
            observe(guard, other)  # a new exhaustion; its need mark is open
        assert [n.context["agent_id"] for n in system_needs(space)] == [str(other.id)]

    def test_manifest_expected_activity_is_the_budget_at_twice_the_rate(self) -> None:
        _, guard, _, _ = space_and_guard(None)
        agent = Agent(
            name="m",
            scopes={"a": ["observation"]},
            manifest=AgentManifest(expected_activity={"observation": 1 / 720}),
        )
        observe(guard, agent)  # budget 2 x (1/720) per hour over 720 h = 2
        observe(guard, agent)
        with pytest.raises(ScopeError, match="spent its write budget"):
            observe(guard, agent)

    def test_the_principal_table_takes_precedence(self) -> None:
        _, guard, _, token = space_and_guard(None)
        agent = Agent(
            name="m",
            scopes={"a": ["observation"]},
            manifest=AgentManifest(expected_activity={"observation": 1 / 720}),
        )
        assert guard.set_write_budget(agent.id, WriteBudget(per_hour={O: 10.0}), token)
        for _ in range(100):
            observe(guard, agent)


class TestActionBudgetAndLeases:
    """An open lease counts against the action budget (Section 9.14)."""

    def budgeted(
        self, actions: float, deferred: bool = False
    ) -> tuple[MarkSpace, Guard, Agent, uuid.UUID]:
        """Room for many intents, and for `actions` actions per day."""
        space = MarkSpace(scopes=[scope("a", deferred=deferred), scope("b")])
        space.set_clock(T0)
        token = uuid.uuid4()
        agent = Agent(name="w", scopes={n: ["need", "intent", "action"] for n in "ab"})
        budget = WriteBudget(
            per_hour={I: 10_000.0, A: actions / 24}, period_seconds=DAY, tiers=()
        )
        guard = Guard(space, principal_token=token, write_budgets={agent.id: budget})
        return space, guard, agent, token

    def test_open_leases_spend_the_action_budget(self) -> None:
        space, guard, agent, _ = self.budgeted(2)
        assert guard.pre_action(agent, "a", "r1", "book").verdict == GuardVerdict.ALLOW
        assert guard.pre_action(agent, "a", "r2", "book").verdict == GuardVerdict.ALLOW
        refused = guard.pre_action(agent, "a", "r3", "book")
        assert refused.verdict == GuardVerdict.DENIED
        assert "spent its write budget for action" in refused.reason
        assert counted(guard, agent, I, T0, DAY) == 2
        assert [n.context["level"] for n in system_needs(space)] == ["exhausted"]

    def test_an_action_under_a_lease_counts_once(self) -> None:
        _, guard, agent, _ = self.budgeted(2)
        d = guard.pre_action(agent, "a", "r1", "book")
        guard.post_action(agent, "a", "r1", "booked", "ok", intent_id=d.intent_id)
        assert guard.pre_action(agent, "a", "r2", "book").verdict == GuardVerdict.ALLOW
        assert guard.pre_action(agent, "a", "r3", "book").verdict == GuardVerdict.DENIED

    def test_an_expired_lease_gives_its_room_back(self) -> None:
        space, guard, agent, _ = self.budgeted(1)
        assert guard.pre_action(agent, "a", "r1", "book").verdict == GuardVerdict.ALLOW
        assert guard.pre_action(agent, "a", "r2", "book").verdict == GuardVerdict.DENIED
        space.set_clock(T0 + minutes(31))
        assert guard.pre_action(agent, "a", "r3", "book").verdict == GuardVerdict.ALLOW

    def test_a_released_lease_gives_its_room_back(self) -> None:
        _, guard, agent, token = self.budgeted(1)
        d = guard.pre_action(agent, "a", "r1", "book")
        assert d.intent_id is not None and guard.release(d.intent_id, token)
        assert guard._leases_by_agent == {}
        assert guard.pre_action(agent, "a", "r2", "book").verdict == GuardVerdict.ALLOW

    def test_a_failed_action_counts_and_closes_its_lease(self) -> None:
        _, guard, agent, _ = self.budgeted(2)
        d = guard.pre_action(agent, "a", "r1", "book")
        guard.post_action(
            agent, "a", "r1", "booked", "boom", intent_id=d.intent_id, failed=True
        )
        assert counted(guard, agent, A, T0, DAY) == 1
        assert guard._leases_by_agent == {}
        assert guard.pre_action(agent, "a", "r2", "book").verdict == GuardVerdict.ALLOW
        assert guard.pre_action(agent, "a", "r3", "book").verdict == GuardVerdict.DENIED

    def test_a_lease_taken_during_pre_action_counts_at_the_grant(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Another call of the agent takes the last room after this call's
        first check: the grant refuses this one, and the need mark follows."""
        space, guard, agent, _ = self.budgeted(1)
        reserve = guard._reserve_write
        inner: list[GuardVerdict] = []

        def racing(*args: Any, **kwargs: Any) -> Any:
            result = reserve(*args, **kwargs)
            if not inner:
                inner.append(GuardVerdict.BLOCKED)
                inner[0] = guard.pre_action(agent, "b", "other", "book").verdict
            return result

        monkeypatch.setattr(guard, "_reserve_write", racing)
        outcome = guard.pre_action(agent, "a", "r", "book")
        assert inner == [GuardVerdict.ALLOW]
        assert outcome.verdict == GuardVerdict.DENIED
        assert outcome.intent_id is not None
        assert guard.claim_state(outcome.intent_id) == ClaimState.LOST
        assert space.get_mark(outcome.intent_id) is not None
        assert counted(guard, agent, I, T0, DAY) == 2  # stored, so it counts
        assert [n.context["level"] for n in system_needs(space)] == ["exhausted"]

    def test_concurrent_pre_actions_never_exceed_the_action_budget(self) -> None:
        _, guard, agent, _ = self.budgeted(10)
        verdicts: list[GuardVerdict] = []

        start = threading.Barrier(8)

        def worker(name: int) -> None:
            start.wait()
            for k in range(50):
                verdicts.append(
                    guard.pre_action(agent, "a", f"r{name}-{k}", "book").verdict
                )

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        assert verdicts.count(GuardVerdict.ALLOW) == 10

    @pytest.mark.parametrize("with_mapping", [True, False])
    def test_a_boundary_refuses_a_lease_when_the_budget_is_spent(
        self, with_mapping: bool
    ) -> None:
        space, guard, agent, _ = self.budgeted(1, deferred=True)
        agents = {agent.id: agent} if with_mapping else None
        first = guard.pre_action(agent, "a", "r1", "book")
        second = guard.pre_action(agent, "a", "r2", "book")
        assert first.verdict == second.verdict == GuardVerdict.BLOCKED
        assert (
            guard.resolve_deferred("a", "r1", agents=agents)[agent.id].verdict
            == GuardVerdict.ALLOW
        )
        refused = guard.resolve_deferred("a", "r2", agents=agents)[agent.id]
        assert refused.verdict == GuardVerdict.DENIED
        assert "spent its write budget for action" in refused.reason
        assert second.intent_id is not None
        assert guard.claim_state(second.intent_id) == ClaimState.LOST
        assert [n.context["level"] for n in system_needs(space)] == ["exhausted"]

    def test_a_boundary_refuses_by_the_manifest_budget_without_the_mapping(
        self,
    ) -> None:
        space = MarkSpace(scopes=[scope("a", deferred=True)])
        space.set_clock(T0)
        hours_in_period = 30 * 24
        agent = Agent(
            name="w",
            scopes={"a": ["intent", "action"]},
            manifest=AgentManifest(
                expected_activity={
                    "intent": 10.0,
                    # Twice this rate over the default period: 1.5 actions.
                    "action": 1.5 / (2 * hours_in_period),
                }
            ),
        )
        guard = Guard(space)
        guard.pre_action(agent, "a", "r1", "book")
        second = guard.pre_action(agent, "a", "r2", "book")
        assert guard.resolve_deferred("a", "r1")[agent.id].verdict == GuardVerdict.ALLOW
        assert (
            guard.resolve_deferred("a", "r2")[agent.id].verdict == GuardVerdict.DENIED
        )
        assert second.intent_id is not None
        assert guard.claim_state(second.intent_id) == ClaimState.LOST

    def test_a_spent_agent_does_not_beat_another_at_a_boundary(self) -> None:
        space, guard, agent, _ = self.budgeted(1, deferred=True)
        other = Agent(name="o", scopes={"a": ["intent", "action"]})
        guard.pre_action(agent, "a", "held", "book")
        guard.resolve_deferred("a", "held")
        # The agent's intent came first, while it had room: it waits for the boundary.
        space.write(
            agent, Intent(scope="a", resource="r", action="book", confidence=0.9)
        )
        guard.pre_action(other, "a", "r", "book", confidence=0.1)
        out = guard.resolve_deferred("a", "r")
        assert out[agent.id].verdict == GuardVerdict.DENIED
        assert out[other.id].verdict == GuardVerdict.ALLOW

    def test_the_principal_can_grant_over_the_budget(self) -> None:
        _, guard, agent, token = self.budgeted(1, deferred=True)
        guard.pre_action(agent, "a", "r1", "book")
        second = guard.pre_action(agent, "a", "r2", "book")
        guard.resolve_deferred("a", "r1")
        assert second.intent_id is not None
        assert guard.grant(second.intent_id, token).verdict == GuardVerdict.ALLOW
        assert guard.pre_action(agent, "b", "r3", "book").verdict == GuardVerdict.DENIED

    def test_an_alert_from_a_boundary_waits_for_the_locks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A need mark takes the need lock and then the space lock, so it must
        not be written while the boundary holds the space and the claim lock."""
        space, guard, agent, _ = self.budgeted(1, deferred=True)
        guard.pre_action(agent, "a", "r1", "book")
        guard.pre_action(agent, "a", "r2", "book")
        guard.resolve_deferred("a", "r1")
        write_need = guard._write_principal_need
        claim_lock_held: list[bool] = []

        def spy(*args: Any, **kwargs: Any) -> Any:
            claim_lock_held.append(getattr(guard._claim_lock, "_is_owned")())
            return write_need(*args, **kwargs)

        monkeypatch.setattr(guard, "_write_principal_need", spy)
        guard.resolve_deferred("a", "r2")
        assert claim_lock_held == [False]
        assert [n.context["level"] for n in system_needs(space)] == ["exhausted"]

    def test_a_refused_post_action_starts_no_exhaustion(self) -> None:
        """Only a write that is refused or recorded starts an exhaustion."""
        space, guard, agent, _ = self.budgeted(0)
        other = Agent(name="o", scopes={"a": ["intent", "action"]})
        assert guard.pre_action(other, "a", "r", "book").verdict == GuardVerdict.ALLOW
        with pytest.raises(NotGrantedError):
            guard.post_action(agent, "a", "r", "booked", "ok")
        assert system_needs(space) == []
        guard.post_action(agent, "a", "free", "booked", "ok")
        assert [n.context["level"] for n in system_needs(space)] == ["exhausted"]

    def test_a_need_mark_of_a_yield_all_conflict_is_metered(self) -> None:
        """The budget meters the need mark, and the principal still gets the
        question: the intent waits for the principal."""
        contested = Scope(
            name="c",
            decay=DecayConfig(
                observation_half_life=minutes(60),
                warning_half_life=minutes(60),
                intent_ttl=minutes(30),
            ),
            conflict_policy=ConflictPolicy.YIELD_ALL,
        )
        space = MarkSpace(scopes=[contested])
        space.set_clock(T0)
        holder = Agent(name="h", scopes={"c": ["intent", "action"]})
        declared = Agent(name="d", scopes={"c": ["intent", "action", "need"]})
        undeclared = Agent(name="u", scopes={"c": ["intent", "action", "need"]})
        guard = Guard(
            space,
            write_budgets={
                declared.id: WriteBudget(
                    per_hour={I: 100.0, A: 100.0, MarkType.NEED: 100.0},
                    period_seconds=DAY,
                    tiers=(),
                ),
                undeclared.id: WriteBudget(
                    per_hour={I: 100.0, A: 100.0}, period_seconds=DAY, tiers=()
                ),
            },
        )
        assert guard.pre_action(holder, "c", "r", "book").verdict == GuardVerdict.ALLOW
        for agent in (undeclared, declared):
            decision = guard.pre_action(agent, "c", "r", "book")
            assert decision.verdict == GuardVerdict.BLOCKED
        needs = space.read(scope="c", mark_type=MarkType.NEED)
        assert [n.agent_id for n in needs] == [declared.id]
        assert counted(guard, declared, MarkType.NEED, T0, DAY) == 1
        by_trigger = {n.context["trigger"]: n for n in system_needs(space)}
        assert by_trigger["write_budget"].context["mark_type"] == "need"
        assert by_trigger["write_budget"].context["agent_id"] == str(undeclared.id)
        question = by_trigger["yield_all"]
        assert question.context["resource"] == "r"
        assert question.context["agent_id"] == str(undeclared.id)
        assert question.blocking

        # One open question per agent, scope and resource: a second conflict on
        # this resource adds none, and one on another resource is asked.
        guard.pre_action(undeclared, "c", "r", "book")
        assert len(system_needs(space)) == 2
        assert guard.pre_action(holder, "c", "s", "book").verdict == GuardVerdict.ALLOW
        guard.pre_action(undeclared, "c", "s", "book")
        asked = [
            n.context["resource"]
            for n in system_needs(space)
            if n.context["trigger"] == "yield_all"
        ]
        assert sorted(asked) == ["r", "s"]

    def test_an_agent_that_may_not_write_a_need_still_reaches_the_principal(
        self,
    ) -> None:
        space = MarkSpace(scopes=[self._yield_all_scope()])
        space.set_clock(T0)
        holder = Agent(name="h", scopes={"c": ["intent", "action"]})
        agent = Agent(name="a", scopes={"c": ["intent", "action"]})
        guard = Guard(space)
        guard.pre_action(holder, "c", "r", "book")
        assert guard.pre_action(agent, "c", "r", "book").verdict == GuardVerdict.BLOCKED
        assert [
            (n.context["trigger"], n.context["resource"]) for n in system_needs(space)
        ] == [("yield_all", "r")]

    def _yield_all_scope(self) -> Scope:
        return Scope(
            name="c",
            decay=DecayConfig(
                observation_half_life=minutes(60),
                warning_half_life=minutes(60),
                intent_ttl=minutes(30),
            ),
            conflict_policy=ConflictPolicy.YIELD_ALL,
        )

    def _conflict_whose_need_write_raises(
        self, monkeypatch: pytest.MonkeyPatch, error: Exception
    ) -> tuple[MarkSpace, Guard, Agent]:
        space = MarkSpace(scopes=[self._yield_all_scope()])
        space.set_clock(T0)
        holder = Agent(name="h", scopes={"c": ["intent", "action"]})
        agent = Agent(name="a", scopes={"c": ["intent", "action", "need"]})
        guard = Guard(
            space,
            write_budgets={
                agent.id: WriteBudget(
                    per_hour={I: 100.0, A: 100.0, MarkType.NEED: 100.0},
                    period_seconds=DAY,
                    tiers=(),
                )
            },
        )
        guard.pre_action(holder, "c", "r", "book")
        write = space.write

        def refuse_agent_need(writer: Agent, mark: Any) -> Any:
            if isinstance(mark, Need) and writer.id == agent.id:
                raise error
            return write(writer, mark)

        monkeypatch.setattr(space, "write", refuse_agent_need)
        return space, guard, agent

    def test_a_need_the_space_refuses_goes_to_the_principal_uncounted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The caller keeps its decision and its intent id."""
        space, guard, agent = self._conflict_whose_need_write_raises(
            monkeypatch, QuotaExceededError("full")
        )
        decision = guard.pre_action(agent, "c", "r", "book")
        assert decision.verdict == GuardVerdict.BLOCKED
        assert decision.intent_id is not None
        assert counted(guard, agent, MarkType.NEED, T0, DAY) == 0
        assert [n.context["trigger"] for n in system_needs(space)] == ["yield_all"]

    def test_a_need_write_that_fails_otherwise_raises_uncounted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, guard, agent = self._conflict_whose_need_write_raises(
            monkeypatch, RuntimeError("broken")
        )
        with pytest.raises(RuntimeError):
            guard.pre_action(agent, "c", "r", "book")
        assert counted(guard, agent, MarkType.NEED, T0, DAY) == 0

    def test_execute_reports_lease_events_and_alerts_at_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sink = InMemorySink()
        space = MarkSpace(scopes=[scope("a"), scope("b")])
        space.set_clock(T0)
        agent = Agent(name="w", scopes={n: ["need", "intent", "action"] for n in "ab"})
        other = Agent(name="o", scopes={"a": ["intent", "action"]})
        guard = Guard(
            space,
            telemetry=sink,
            write_budgets={
                agent.id: WriteBudget(
                    per_hour={I: 10_000.0, A: 1 / 24}, period_seconds=DAY, tiers=()
                )
            },
        )
        assert (
            guard.pre_action(other, "a", "held", "book").verdict == GuardVerdict.ALLOW
        )
        sink.counters.clear()
        decision, _ = guard.execute(
            agent, "a", "held", "book", "booked", lambda: "ok", confidence=0.1
        )
        assert decision.verdict == GuardVerdict.CONFLICT
        assert sink.counters

        reserve = guard._reserve_write
        raced: list[bool] = []

        def racing(*args: Any, **kwargs: Any) -> Any:
            result = reserve(*args, **kwargs)
            if not raced:
                raced.append(True)
                guard.pre_action(agent, "b", "other", "book")
            return result

        monkeypatch.setattr(guard, "_reserve_write", racing)
        decision, _ = guard.execute(agent, "a", "r", "book", "booked", lambda: "ok")
        assert decision.verdict == GuardVerdict.DENIED
        assert [n.context["level"] for n in system_needs(space)] == ["exhausted"]

    def test_pre_action_reports_with_the_resource_lock_released(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A slow telemetry sink must not hold other calls on the resource."""
        _, guard, agent, _ = self.budgeted(1)
        emit = guard._emit_lease_events
        free: list[bool] = []

        def take_the_lock() -> None:
            with guard._resource_lock("a", "r"):
                pass

        def spy() -> None:
            other = threading.Thread(target=take_the_lock, daemon=True)
            other.start()
            other.join(timeout=0.5)
            free.append(not other.is_alive())
            emit()

        monkeypatch.setattr(guard, "_emit_lease_events", spy)
        guard.pre_action(agent, "a", "r", "book")
        assert free == [True]

    def test_post_action_reserves_in_the_hold_that_writes_the_action(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A boundary holds the space, so it counts the action once: as the lease
        before this hold and as the counted action after it."""
        space, guard, agent, _ = self.budgeted(2)
        d = guard.pre_action(agent, "a", "r", "book")
        reserve = guard._reserve_write
        held: list[bool] = []

        def spy(*args: Any, **kwargs: Any) -> Any:
            held.append(getattr(space._lock, "_is_owned")())
            return reserve(*args, **kwargs)

        monkeypatch.setattr(guard, "_reserve_write", spy)
        guard.post_action(agent, "a", "r", "booked", "ok", intent_id=d.intent_id)
        assert held == [True]

    def test_a_recorded_action_does_not_end_an_exhaustion_while_leases_fill_it(
        self,
    ) -> None:
        space, guard, agent, _ = self.budgeted(3)
        principal = Agent(name="p", scopes={"b": ["action"]})
        decisions = [guard.pre_action(agent, "a", f"r{k}", "book") for k in range(3)]
        assert guard.pre_action(agent, "a", "r3", "book").verdict == GuardVerdict.DENIED
        alert = system_needs(space)[0]
        answer = space.write(
            principal, Action(scope="b", resource="answer", action="ok", result="ok")
        )
        space.resolve(alert.id, answer)
        guard.post_action(
            agent, "a", "r0", "booked", "ok", intent_id=decisions[0].intent_id
        )
        assert guard.pre_action(agent, "a", "r3", "book").verdict == GuardVerdict.DENIED
        assert system_needs(space) == []

    def test_a_refused_post_action_does_not_end_an_exhaustion(self) -> None:
        space, guard, agent, _ = self.budgeted(1)
        other = Agent(name="o", scopes={"a": ["intent", "action"]})
        principal = Agent(name="p", scopes={"b": ["action"]})
        assert guard.pre_action(agent, "a", "r1", "book").verdict == GuardVerdict.ALLOW
        assert guard.pre_action(agent, "a", "r2", "book").verdict == GuardVerdict.DENIED
        answer = space.write(
            principal, Action(scope="b", resource="answer", action="ok", result="ok")
        )
        space.resolve(system_needs(space)[0].id, answer)
        assert guard.pre_action(other, "a", "x", "book").verdict == GuardVerdict.ALLOW
        with pytest.raises(NotGrantedError):
            guard.post_action(agent, "a", "x", "booked", "ok")
        assert guard.pre_action(agent, "a", "r3", "book").verdict == GuardVerdict.DENIED
        assert system_needs(space) == []

    def test_a_boundary_charges_nothing_for_a_lease_it_does_not_grant(self) -> None:
        """The agent holds its one lease and another agent is the candidate: the
        agent is refused nothing, and its lease still names the conflict."""
        space, guard, agent, _ = self.budgeted(1, deferred=True)
        other = Agent(name="o", scopes={"a": ["intent", "action"]})
        guard.pre_action(agent, "a", "r", "book", confidence=0.9)
        assert guard.resolve_deferred("a", "r")[agent.id].verdict == GuardVerdict.ALLOW
        guard.pre_action(other, "a", "r", "book", confidence=0.1)
        out = guard.resolve_deferred("a", "r")
        assert out[other.id].verdict == GuardVerdict.BLOCKED
        assert len(out[other.id].conflicting_intents) == 1
        assert system_needs(space) == []

    def test_a_principal_budget_keeps_the_manifest_budget_for_later(self) -> None:
        space = MarkSpace(scopes=[scope("a", deferred=True)])
        space.set_clock(T0)
        token = uuid.uuid4()
        agent = Agent(
            name="w",
            scopes={"a": ["intent", "action"]},
            manifest=AgentManifest(
                expected_activity={"intent": 10.0, "action": 1.5 / (2 * 30 * 24)}
            ),
        )
        guard = Guard(space, principal_token=token)
        large = WriteBudget(per_hour={I: 100.0, A: 100.0}, period_seconds=DAY, tiers=())
        assert guard.set_write_budget(agent.id, large, token)
        guard.pre_action(agent, "a", "r1", "book")
        guard.pre_action(agent, "a", "r2", "book")
        assert guard.set_write_budget(agent.id, None, token)
        assert guard.resolve_deferred("a", "r1")[agent.id].verdict == GuardVerdict.ALLOW
        assert (
            guard.resolve_deferred("a", "r2")[agent.id].verdict == GuardVerdict.DENIED
        )

    def test_a_manifest_without_a_budget_ends_the_manifest_budget(self) -> None:
        space = MarkSpace(scopes=[scope("a", deferred=True)])
        space.set_clock(T0)
        agent = Agent(
            name="w",
            scopes={"a": ["intent", "action"]},
            manifest=AgentManifest(
                expected_activity={"intent": 10.0, "action": 1.5 / (2 * 30 * 24)}
            ),
        )
        guard = Guard(space)
        guard.pre_action(agent, "a", "r1", "book")
        assert guard.resolve_deferred("a", "r1")[agent.id].verdict == GuardVerdict.ALLOW
        unmetered = agent.model_copy(update={"manifest": None})
        guard.pre_action(unmetered, "a", "r2", "book")
        assert guard.resolve_deferred("a", "r2")[agent.id].verdict == GuardVerdict.ALLOW

    def test_a_boundary_with_the_mapping_reads_the_agents_own_budget(self) -> None:
        """The intent reached the space without the guard, so the guard has not
        seen this agent's manifest: the mapping supplies it."""
        space = MarkSpace(scopes=[scope("a", deferred=True)])
        space.set_clock(T0)
        agent = Agent(
            name="w",
            scopes={"a": ["intent", "action"]},
            manifest=AgentManifest(expected_activity={"intent": 10.0, "action": 0.0}),
        )
        guard = Guard(space)
        space.write(
            agent, Intent(scope="a", resource="r", action="book", confidence=0.5)
        )
        out = guard.resolve_deferred("a", "r", agents={agent.id: agent})
        assert out[agent.id].verdict == GuardVerdict.DENIED

    def test_room_from_an_ended_lease_ends_the_exhaustion(self) -> None:
        """The principal resolved the first alert. An agent that takes leases and
        never acts must alert again, so a later refusal cannot be silent."""
        space, guard, agent, _ = self.budgeted(1)
        principal = Agent(name="p", scopes={"a": ["action"]})
        assert guard.pre_action(agent, "a", "r1", "book").verdict == GuardVerdict.ALLOW
        assert guard.pre_action(agent, "a", "r2", "book").verdict == GuardVerdict.DENIED
        first = system_needs(space)
        assert len(first) == 1
        answer = space.write(
            principal, Action(scope="a", resource="answer", action="ok", result="ok")
        )
        space.resolve(first[0].id, answer)
        space.set_clock(T0 + minutes(31))
        assert guard.pre_action(agent, "a", "r3", "book").verdict == GuardVerdict.ALLOW
        assert guard.pre_action(agent, "a", "r4", "book").verdict == GuardVerdict.DENIED
        assert len(system_needs(space)) == 1
        assert system_needs(space)[0].id != first[0].id

    def _spend_at_a_boundary_and_resolve_the_alert(
        self, space: MarkSpace, guard: Guard, agent: Agent, *resources: str
    ) -> uuid.UUID:
        """Budget 1: the first resource takes the lease, the second is refused,
        and the principal resolves the alert. Returns the first lease's intent."""
        first = guard.pre_action(agent, "a", resources[0], "book")
        for resource in resources[1:]:
            guard.pre_action(agent, "a", resource, "book")
        assert (
            guard.resolve_deferred("a", resources[0])[agent.id].verdict
            == GuardVerdict.ALLOW
        )
        assert (
            guard.resolve_deferred("a", resources[1])[agent.id].verdict
            == GuardVerdict.DENIED
        )
        alert = system_needs(space)[0]
        principal = Agent(name="p", scopes={"b": ["action"]})
        answer = space.write(
            principal, Action(scope="b", resource="answer", action="ok", result="ok")
        )
        space.resolve(alert.id, answer)
        assert first.intent_id is not None
        return first.intent_id

    def test_a_boundary_that_finds_room_ends_the_exhaustion(self) -> None:
        space, guard, agent, token = self.budgeted(1, deferred=True)
        lease = self._spend_at_a_boundary_and_resolve_the_alert(
            space, guard, agent, "r1", "r2", "r3"
        )
        assert guard.release(lease, token)
        assert guard.resolve_deferred("a", "r3")[agent.id].verdict == GuardVerdict.ALLOW
        assert system_needs(space) == []
        assert guard.pre_action(agent, "b", "r4", "book").verdict == GuardVerdict.DENIED
        assert len(system_needs(space)) == 1

    def test_a_pre_action_that_finds_room_ends_the_exhaustion(self) -> None:
        """The principal grants the next lease, so no check of the grant ends it."""
        space, guard, agent, token = self.budgeted(1, deferred=True)
        self._spend_at_a_boundary_and_resolve_the_alert(space, guard, agent, "r1", "r2")
        space.set_clock(T0 + minutes(31))
        pending = guard.pre_action(agent, "a", "r3", "book")
        assert pending.verdict == GuardVerdict.BLOCKED
        assert pending.intent_id is not None
        assert guard.grant(pending.intent_id, token).verdict == GuardVerdict.ALLOW
        assert system_needs(space) == []
        assert guard.pre_action(agent, "b", "r4", "book").verdict == GuardVerdict.DENIED
        assert len(system_needs(space)) == 1

    def test_a_retry_under_an_open_lease_counts_and_says_so(self) -> None:
        _, guard, agent, _ = self.budgeted(1)
        assert guard.pre_action(agent, "a", "r", "book").verdict == GuardVerdict.ALLOW
        retry = guard.pre_action(agent, "a", "r", "book")
        assert retry.verdict == GuardVerdict.DENIED
        assert "open leases" in retry.reason

    def test_an_expiry_found_by_a_deferred_pre_action_is_reported_at_once(self) -> None:
        space = MarkSpace(scopes=[scope("a", deferred=True), scope("b")])
        space.set_clock(T0)
        sink = InMemorySink()
        agent = Agent(name="w", scopes={n: ["intent", "action"] for n in "ab"})
        guard = Guard(
            space,
            telemetry=sink,
            write_budgets={
                agent.id: WriteBudget(
                    per_hour={I: 100.0, A: 100.0}, period_seconds=DAY, tiers=()
                )
            },
        )
        assert guard.pre_action(agent, "b", "r", "book").verdict == GuardVerdict.ALLOW
        space.set_clock(T0 + minutes(31))
        assert guard.pre_action(agent, "a", "s", "book").verdict == GuardVerdict.BLOCKED
        assert any(c[0] == METRIC_LEASES_EXPIRED for c in sink.counters)

    @pytest.mark.parametrize("stated", [1, 7, 13, 26, 29, 58, 100])
    def test_a_whole_budget_allows_that_many_writes(self, stated: int) -> None:
        """Float error in rate * period must not cost the last write (P78)."""
        budget = WriteBudget(per_hour={O: stated / 720}, tiers=())
        assert budget.budget(O) == stated
        meter = WriteBudgetMeter()
        agent = uuid.uuid4()
        taken = [write(meter, agent, budget, T0 + k)[0] for k in range(stated + 1)]
        assert taken == [False] * stated + [True]

    def test_a_rate_whose_budget_is_not_finite_is_refused_at_the_manifest(self) -> None:
        with pytest.raises(PydanticValidationError):
            AgentManifest(expected_activity={"action": 1e308})

    @pytest.mark.parametrize("rate", [1e307, 1e303, 1e302])
    def test_a_budget_too_large_to_count_is_never_spent(self, rate: float) -> None:
        space = MarkSpace(scopes=[scope("a")])
        space.set_clock(T0)
        agent = Agent(
            name="w",
            scopes={"a": ["intent", "action"]},
            manifest=AgentManifest(expected_activity={"intent": rate, "action": rate}),
        )
        decision, _ = Guard(space).execute(
            agent, "a", "r", "book", "booked", lambda: "ok"
        )
        assert decision.verdict == GuardVerdict.ALLOW

    def test_a_retry_while_blocked_spends_no_budget_for_need_marks(self) -> None:
        space = MarkSpace(scopes=[self._yield_all_scope()])
        space.set_clock(T0)
        holder = Agent(name="h", scopes={"c": ["intent", "action"]})
        agent = Agent(name="a", scopes={"c": ["intent", "action", "need"]})
        guard = Guard(
            space,
            write_budgets={
                agent.id: WriteBudget(
                    per_hour={I: 100.0, A: 100.0, MarkType.NEED: 3 / 24},
                    period_seconds=DAY,
                    tiers=(),
                )
            },
        )
        guard.pre_action(holder, "c", "r", "book")
        for _ in range(6):
            assert (
                guard.pre_action(agent, "c", "r", "book").verdict
                == GuardVerdict.BLOCKED
            )
        assert counted(guard, agent, MarkType.NEED, T0, DAY) == 1
        assert len(space.read(scope="c", mark_type=MarkType.NEED)) == 1

    def test_an_agent_whose_barrier_revokes_need_marks_reaches_the_principal(
        self,
    ) -> None:
        space = MarkSpace(scopes=[self._yield_all_scope()])
        space.set_clock(T0)
        holder = Agent(name="h", scopes={"c": ["intent", "action"]})
        agent = Agent(name="a", scopes={"c": ["intent", "action", "need"]})
        guard = Guard(space)
        guard.get_or_create_barrier(agent.id).narrow("c", "need")
        guard.pre_action(holder, "c", "r", "book")
        assert guard.pre_action(agent, "c", "r", "book").verdict == GuardVerdict.BLOCKED
        assert space.read(scope="c", mark_type=MarkType.NEED) == []
        assert [n.context["trigger"] for n in system_needs(space)] == ["yield_all"]

    def test_a_metered_need_of_a_conflict_fires_the_burn_tiers(self) -> None:
        space = MarkSpace(scopes=[self._yield_all_scope()])
        space.set_clock(T0)
        holder = Agent(name="h", scopes={"c": ["intent", "action"]})
        agent = Agent(name="a", scopes={"c": ["intent", "action", "need"]})
        guard = Guard(
            space,
            write_budgets={
                agent.id: WriteBudget(
                    per_hour={I: 100.0, A: 100.0, MarkType.NEED: 1.0},
                    period_seconds=DAY,
                    tiers=(PAGE,),
                )
            },
        )
        for resource in ("r1", "r2"):
            guard.pre_action(holder, "c", resource, "book")
            guard.pre_action(agent, "c", resource, "book")
        assert [n.context["level"] for n in system_needs(space)] == ["page"]

    def _writes_refused_for_actions(
        self, monkeypatch: pytest.MonkeyPatch, space: MarkSpace
    ) -> None:
        write = space.write

        def refuse_action(writer: Agent, mark: Any) -> Any:
            if isinstance(mark, Action):
                raise QuotaExceededError("full")
            return write(writer, mark)

        monkeypatch.setattr(space, "write", refuse_action)

    def test_an_action_the_space_refuses_is_not_counted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        space, guard, agent, _ = self.budgeted(5)
        self._writes_refused_for_actions(monkeypatch, space)
        with pytest.raises(QuotaExceededError):
            guard.post_action(agent, "a", "r", "booked", "ok")
        assert counted(guard, agent, A, T0, DAY) == 0

    def test_an_action_the_space_refuses_starts_no_exhaustion(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        space, guard, agent, _ = self.budgeted(0)
        with monkeypatch.context() as patch:
            self._writes_refused_for_actions(patch, space)
            with pytest.raises(QuotaExceededError):
                guard.post_action(agent, "a", "r", "booked", "ok")
        assert system_needs(space) == []
        guard.post_action(agent, "a", "r", "booked", "ok")
        assert [n.context["level"] for n in system_needs(space)] == ["exhausted"]

    def test_need_marks_for_an_action_are_written_outside_the_space_hold(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A need mark takes the need lock and then the space lock."""
        space, guard, agent, _ = self.budgeted(0)
        write_need = guard._write_principal_need
        space_held: list[bool] = []

        def spy(*args: Any, **kwargs: Any) -> Any:
            space_held.append(getattr(space._lock, "_is_owned")())
            return write_need(*args, **kwargs)

        monkeypatch.setattr(guard, "_write_principal_need", spy)
        guard.post_action(agent, "a", "r", "booked", "ok")
        assert space_held == [False]

    @pytest.mark.parametrize("method", ["pre_action", "resolve_deferred", "grant"])
    def test_lease_events_are_reported_when_a_call_raises(
        self, monkeypatch: pytest.MonkeyPatch, method: str
    ) -> None:
        space, guard, agent, token = self.budgeted(5, deferred=method != "pre_action")
        pending = guard.pre_action(agent, "a", "r", "book")
        assert pending.intent_id is not None

        def raising(*args: Any, **kwargs: Any) -> Any:
            guard._lease_events.append(("queued", {}))
            raise RuntimeError("broken")

        inner = {
            "pre_action": "_pre_action_inner",
            "resolve_deferred": "_resolve_locked",
            "grant": "_grant_checked",
        }[method]
        monkeypatch.setattr(guard, inner, raising)
        with pytest.raises(RuntimeError):
            if method == "pre_action":
                guard.pre_action(agent, "a", "s", "book")
            elif method == "resolve_deferred":
                guard.resolve_deferred("a", "r")
            else:
                guard.grant(pending.intent_id, token)
        assert guard._lease_events == []

    def test_a_large_budget_near_a_whole_number_is_not_rounded_to_it(self) -> None:
        stated = 1_000_000.4
        budget = WriteBudget(per_hour={O: stated / 720}, tiers=()).budget(O)
        assert budget == pytest.approx(stated, rel=1e-12)
        assert budget != round(stated)

    def test_a_large_whole_budget_also_allows_that_many(self) -> None:
        stated = 4_194_334
        assert WriteBudget(per_hour={O: stated / 720}, tiers=()).budget(O) == stated

    def test_a_budget_that_is_not_whole_stays_as_stated(self) -> None:
        assert WriteBudget(per_hour={O: 2.5 / 720}, tiers=()).budget(
            O
        ) == pytest.approx(2.5, rel=1e-15)
        tiny = WriteBudget(per_hour={O: 1e-15}, tiers=()).budget(O)
        assert 0.0 < tiny < 1e-9

    def test_a_tier_threshold_of_whole_writes_is_not_low_by_float_error(self) -> None:
        budget = WriteBudget(per_hour={O: 3050 / 720})
        tier = budget.tiers[0]
        assert budget.threshold(tier, O, tier.long_window_seconds) == 61.0

    def test_the_meter_counts_open_writes_and_records_nothing(self) -> None:
        meter = WriteBudgetMeter()
        agent = uuid.uuid4()
        assert not meter.spent(agent, DAILY, O, T0, open_writes=23)
        assert meter.spent(agent, DAILY, O, T0, open_writes=24)
        assert counted_meter(meter, agent, O, T0) == 0


class TestP78WriteBudgetRoom:
    def test_writes_at_the_budgeted_rate_are_never_refused(self) -> None:
        meter = WriteBudgetMeter()
        agent = uuid.uuid4()
        for k in range(24 * 10):
            assert not write(meter, agent, DAILY, T0 + 3600 * k)[0]

    def test_an_agent_without_a_budget_is_not_metered(self) -> None:
        _, guard, agent, _ = space_and_guard(None)
        for _ in range(500):
            observe(guard, agent)


# One write an hour budgeted over a day; a page tier at burn rate 1:
# thresholds 1 per hour and 1/12 per 5 min.
PAGE = BurnTier(
    level="page",
    long_window_seconds=3600,
    short_window_seconds=300,
    budget_fraction=1 / 24,
)
TICKET = BurnTier(
    level="ticket",
    long_window_seconds=6 * 3600,
    short_window_seconds=1800,
    budget_fraction=6 / 24,
)
ALERTING = WriteBudget(per_hour={O: 1.0}, period_seconds=DAY, tiers=(PAGE,))


class TestP79WriteBudgetAlerts:
    def test_a_tier_fires_when_both_windows_exceed_and_refuses_nothing(self) -> None:
        meter = WriteBudgetMeter()
        agent = uuid.uuid4()
        assert write(meter, agent, ALERTING, T0) == (False, ())
        refused, started = write(meter, agent, ALERTING, T0 + 1)
        assert not refused
        assert [b.tier.level for b in started] == ["page"]
        assert (started[0].long_count, started[0].short_count) == (2, 2)

    def test_one_notification_per_episode(self) -> None:
        meter = WriteBudgetMeter()
        agent = uuid.uuid4()
        write(meter, agent, ALERTING, T0)
        assert write(meter, agent, ALERTING, T0 + 1)[1]
        assert not write(meter, agent, ALERTING, T0 + 2)[1]  # still firing
        assert not write(meter, agent, ALERTING, T0 + 7200)[1]  # stops
        assert write(meter, agent, ALERTING, T0 + 7201)[1]  # a new episode

    def test_a_quiet_short_window_stops_the_tier(self) -> None:
        """Thresholds 12 per hour and 1 per 300 s: 13 writes fire the tier; a
        14th 400 s later makes 14 in the hour but 1 in the last 300 s, and the
        tier stops."""
        budget = WriteBudget(per_hour={O: 12.0}, period_seconds=DAY, tiers=(PAGE,))
        meter = WriteBudgetMeter()
        agent = uuid.uuid4()
        for k in range(13):
            write(meter, agent, budget, T0 + k)
        assert (agent, O, PAGE) in meter._firing
        write(meter, agent, budget, T0 + 400)
        assert (agent, O, PAGE) not in meter._firing

    def test_guard_writes_page_and_ticket_needs(self) -> None:
        budget = WriteBudget(
            per_hour={O: 1.0}, period_seconds=DAY, tiers=(PAGE, TICKET)
        )
        space, guard, agent, _ = space_and_guard(budget)
        for _ in range(8):
            observe(guard, agent)
        needs = {n.context["level"]: n for n in system_needs(space)}
        assert set(needs) == {"page", "ticket"}
        assert needs["page"].priority == 0.9 and needs["ticket"].priority == 0.5
        for _ in range(8):  # still firing: no new needs, no refusal
            observe(guard, agent)
        assert len(system_needs(space)) == 2

    def test_an_open_need_suppresses_a_new_episode(self) -> None:
        """The tier stops and starts again, but the first page is still open."""
        space, guard, agent, _ = space_and_guard(ALERTING)
        observe(guard, agent)
        observe(guard, agent)  # page
        space.set_clock(T0 + 7200)
        observe(guard, agent)  # stops
        observe(guard, agent)  # starts again
        assert levels(space) == ["page"]

    def test_set_write_budget_clears_the_alert_state(self) -> None:
        space, guard, agent, token = space_and_guard(ALERTING)
        observe(guard, agent)
        observe(guard, agent)  # page, tier firing
        assert guard.set_write_budget(agent.id, ALERTING, token)
        observe(guard, agent)
        observe(guard, agent)  # fires anew; the open-need record was cleared
        assert levels(space) == ["page", "page"]

    def test_need_mark_hooks_run_with_the_guard_locks_released(self) -> None:
        """A write hook on one agent's need mark can wait for another
        agent's write that writes a need mark too (Section 8.6)."""
        space = MarkSpace(scopes=[scope("a")])
        space.set_clock(T0)
        x = Agent(name="x", scopes={"a": ["observation"]})
        y = Agent(name="y", scopes={"a": ["observation"]})
        zero = WriteBudget(per_hour={}, tiers=())
        guard = Guard(space, write_budgets={x.id: zero, y.id: zero})
        done: list[bool] = []

        def refused_write() -> None:
            try:
                observe(guard, y)
            except ScopeError:
                pass

        def hook(_agent_id: uuid.UUID, mark: object) -> None:
            if isinstance(mark, Need) and mark.context["agent_id"] == str(x.id):
                other = threading.Thread(target=refused_write, daemon=True)
                other.start()
                other.join(timeout=2)
                done.append(not other.is_alive())

        space.add_write_hook(hook)
        with pytest.raises(ScopeError):
            observe(guard, x)
        assert done == [True]
        assert sorted(
            str(n.context["agent_id"]) for n in system_needs(space)
        ) == sorted([str(x.id), str(y.id)])

    def test_two_agents_alert_apart(self) -> None:
        space, guard, agent, token = space_and_guard(ALERTING)
        other = Agent(name="o", scopes={"a": ["observation"]})
        assert guard.set_write_budget(other.id, ALERTING, token)
        observe(guard, agent)
        observe(guard, other)
        assert system_needs(space) == []
        observe(guard, agent)
        assert [n.context["agent_id"] for n in system_needs(space)] == [str(agent.id)]
