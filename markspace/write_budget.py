# -*- coding: utf-8 -*-
"""
Markspace Coordination Protocol - Write Budgets

A write budget is a per-agent, per-mark-type allowance of writes over a
trailing period (default 30 days, the budget period of the alerting chapter
of Google's Site Reliability Workbook), stated as a rate per hour: set by the
principal, or taken from the agent manifest's expected_activity at twice the
expected rate, so that the budget sits above normal use as an error budget
sits above a service's normal error rate. A type the budget does not list has
a budget of 0.

The guard follows that workbook as written:

- Error-budget policy ("Example Error Budget Policy"): when an agent has
  spent its budget of a type over the trailing period, the guard refuses its
  further writes of that type until the count over the trailing period is
  back below the budget. This is the only automatic action. A write is
  reserved before it is made and released if another check refuses it, so
  the count holds the writes that were stored.
- Burn-rate alerts (chapter 5, "Alerting on SLOs", Table 5-8): a tier fires
  when the agent's writes of a type exceed the tier's burn rate over both its
  long and its short window. A firing tier only notifies the principal
  through a need mark ("pages and tickets are the only valid ways to get a
  human to take action"). Default tiers, the workbook's starting values: 2 %
  of the budget in 1 h (short window 5 min) and 5 % in 6 h (30 min) page;
  10 % in 3 days (6 h) opens a ticket.

The burn rate is the speed at which the budget is spent, relative to the
period: a tier that spends `budget_fraction` of the budget in its long window
has burn rate budget_fraction x period / long window.

Write budgets operate independently of the token budgets, the scope rate
limits and the statistical envelope.

Spec Section 9.14.
"""

from __future__ import annotations

import bisect
import math
import uuid
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from markspace.core import MarkType

DAY = 86_400.0
EXPECTED_ACTIVITY_HEADROOM = 2.0


class BurnTier(BaseModel):
    """A burn-rate alert: page or ticket when the writes of a type exceed the
    tier's burn rate over both windows. Spec Section 9.14."""

    model_config = ConfigDict(frozen=True)

    level: Literal["page", "ticket"]
    long_window_seconds: float = Field(gt=0, allow_inf_nan=False)
    short_window_seconds: float = Field(gt=0, allow_inf_nan=False)
    budget_fraction: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def _check(self) -> BurnTier:
        if self.short_window_seconds >= self.long_window_seconds:
            raise ValueError("the short window must be shorter than the long window")
        return self


DEFAULT_TIERS: tuple[BurnTier, ...] = (
    BurnTier(
        level="page",
        long_window_seconds=3600,
        short_window_seconds=300,
        budget_fraction=0.02,
    ),
    BurnTier(
        level="page",
        long_window_seconds=6 * 3600,
        short_window_seconds=1800,
        budget_fraction=0.05,
    ),
    BurnTier(
        level="ticket",
        long_window_seconds=3 * DAY,
        short_window_seconds=6 * 3600,
        budget_fraction=0.10,
    ),
)


class WriteBudget(BaseModel):
    """A write budget for one agent.

    per_hour: the agent's budgeted writes per hour for each mark type; the
    budget of a type is per_hour x period / 3600. A type that is not listed
    has a budget of 0.

    Spec Section 9.14.
    """

    model_config = ConfigDict(frozen=True)

    per_hour: dict[MarkType, float]
    period_seconds: float = Field(default=30 * DAY, gt=0, allow_inf_nan=False)
    tiers: tuple[BurnTier, ...] = DEFAULT_TIERS

    @model_validator(mode="after")
    def _check(self) -> WriteBudget:
        for rate in self.per_hour.values():
            if not math.isfinite(rate) or rate < 0:
                raise ValueError("per_hour rates must be finite and >= 0")
        if any(t.long_window_seconds > self.period_seconds for t in self.tiers):
            raise ValueError("a tier's long window cannot exceed the period")
        return self

    @classmethod
    def from_expected_activity(cls, expected: dict[MarkType, float]) -> WriteBudget:
        """The budget for an agent that declares its expected (average) writes
        per hour: EXPECTED_ACTIVITY_HEADROOM times that, with the default
        period and tiers."""
        return cls(
            per_hour={
                mt: EXPECTED_ACTIVITY_HEADROOM * rate for mt, rate in expected.items()
            }
        )

    def budget(self, mark_type: MarkType) -> float:
        """Writes of mark_type allowed over the trailing period."""
        return self.per_hour.get(mark_type, 0.0) * self.period_seconds / 3600.0

    def threshold(
        self, tier: BurnTier, mark_type: MarkType, window_seconds: float
    ) -> float:
        """Writes of mark_type in a window at the tier's burn rate."""
        burn = tier.budget_fraction * self.period_seconds / tier.long_window_seconds
        return burn * self.per_hour.get(mark_type, 0.0) * window_seconds / 3600.0


@dataclass(frozen=True)
class Burn:
    """A burn-rate tier that fired on a write."""

    tier: BurnTier
    mark_type: MarkType
    long_count: int
    long_threshold: float
    short_count: int
    short_threshold: float


@dataclass(frozen=True)
class Reservation:
    """One write reserved against a budget: refused (budget spent), or counted
    at `at` until it is released or confirmed."""

    refused: bool  # not counted; a forced write is never refused
    period_count: int
    budget: float
    at: float | None = None  # the counted time; None when refused
    exhaustion_started: bool = False  # the first refusal of this episode


@dataclass
class _Times:
    """Write times in order; times[start:] are inside the period."""

    times: list[float] = field(default_factory=list)
    start: int = 0

    def prune(self, now: float, keep_seconds: float) -> None:
        self.start = bisect.bisect_right(self.times, now - keep_seconds, lo=self.start)
        if self.start > 1024 and self.start > len(self.times) // 2:
            del self.times[: self.start]
            self.start = 0

    def count_since(self, after: float) -> int:
        """Writes in (after, now]: the window is half-open."""
        return len(self.times) - bisect.bisect_right(self.times, after, lo=self.start)


@dataclass
class WriteBudgetMeter:
    """Accepted write times per agent and mark type, and which tiers fire.

    Thread safety: the guard holds its own lock when calling these methods.
    """

    _times: dict[tuple[uuid.UUID, MarkType], _Times] = field(default_factory=dict)
    # (agent, mark type, tier) for tiers firing on the agent's last write
    _firing: set[tuple[uuid.UUID, MarkType, BurnTier]] = field(default_factory=set)
    # (agent, mark type) whose last write was refused for a spent budget
    _exhausted: set[tuple[uuid.UUID, MarkType]] = field(default_factory=set)

    def reserve(
        self,
        agent_id: uuid.UUID,
        budget: WriteBudget,
        mark_type: MarkType,
        now: float,
        force: bool = False,
    ) -> Reservation:
        """Reserve one write of mark_type at time now. A write that would
        bring the count over the trailing period above the budget is refused
        and not counted, unless `force` (a write that has already happened,
        such as an action whose tool has run): then it is counted and reported
        as over the budget."""
        times = self._times.setdefault((agent_id, mark_type), _Times())
        times.prune(now, budget.period_seconds)
        period_count = times.count_since(now - budget.period_seconds)
        allowed = budget.budget(mark_type)
        over = period_count + 1 > allowed
        started_now = over and self.note_refusal(agent_id, mark_type)
        if over and not force:
            return Reservation(True, period_count, allowed, None, started_now)
        if not over:
            self._exhausted.discard((agent_id, mark_type))
        bisect.insort(times.times, now, lo=times.start)
        return Reservation(False, period_count + 1, allowed, now, started_now)

    def note_refusal(self, agent_id: uuid.UUID, mark_type: MarkType) -> bool:
        """Mark the type as refused for the agent; True if this starts an
        exhaustion episode."""
        key = (agent_id, mark_type)
        if key in self._exhausted:
            return False
        self._exhausted.add(key)
        return True

    def release(self, agent_id: uuid.UUID, mark_type: MarkType, at: float) -> None:
        """Remove a reserved write that was not made."""
        times = self._times.get((agent_id, mark_type))
        if times is None:
            return
        i = bisect.bisect_left(times.times, at, lo=times.start)
        if i < len(times.times) and times.times[i] == at:
            del times.times[i]

    def confirm(
        self,
        agent_id: uuid.UUID,
        budget: WriteBudget,
        mark_type: MarkType,
        now: float,
    ) -> tuple[Burn, ...]:
        """After a reserved write was made: the tiers that started firing on
        it (a tier that was already firing is not named again until it stops
        at a write that does not exceed both thresholds)."""
        times = self._times.get((agent_id, mark_type))
        if times is None:
            return ()
        started: list[Burn] = []
        for tier in budget.tiers:
            long_count = times.count_since(now - tier.long_window_seconds)
            short_count = times.count_since(now - tier.short_window_seconds)
            long_threshold = budget.threshold(tier, mark_type, tier.long_window_seconds)
            short_threshold = budget.threshold(
                tier, mark_type, tier.short_window_seconds
            )
            key = (agent_id, mark_type, tier)
            if long_count > long_threshold and short_count > short_threshold:
                if key not in self._firing:
                    self._firing.add(key)
                    started.append(
                        Burn(
                            tier,
                            mark_type,
                            long_count,
                            long_threshold,
                            short_count,
                            short_threshold,
                        )
                    )
            else:
                self._firing.discard(key)
        return tuple(started)

    def spent(
        self, agent_id: uuid.UUID, budget: WriteBudget, mark_type: MarkType, now: float
    ) -> bool:
        """Whether one more write of mark_type would exceed the budget; counts
        nothing."""
        times = self._times.get((agent_id, mark_type))
        count = times.count_since(now - budget.period_seconds) if times else 0
        return count + 1 > budget.budget(mark_type)

    def forget(self, agent_id: uuid.UUID) -> None:
        """Clear the agent's counted writes (the principal set a new budget)."""
        for key in [k for k in self._times if k[0] == agent_id]:
            del self._times[key]
        self._firing = {k for k in self._firing if k[0] != agent_id}
        self._exhausted = {k for k in self._exhausted if k[0] != agent_id}
