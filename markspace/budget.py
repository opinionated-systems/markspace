# -*- coding: utf-8 -*-
"""
Markspace Coordination Protocol - Token Budgets

Per-agent token budget tracking and enforcement. The guard uses these
to limit how many tokens an agent consumes (reads) and generates (output)
per round and over its lifetime.

Budgets are optional. An AgentManifest with no budget field behaves
identically to the pre-budget protocol (P59).

Spec Section 9.10.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class BudgetStatus(str, Enum):
    """Result of a budget check."""

    OK = "ok"  # within budget
    WARNING_INPUT = "warning_input"  # crossed warning threshold (input)
    WARNING_OUTPUT = "warning_output"  # crossed warning threshold (output)
    EXHAUSTED_INPUT = "exhausted_input"  # hard stop (input)
    EXHAUSTED_OUTPUT = "exhausted_output"  # hard stop (output)


class TokenBudget(BaseModel):
    """Per-agent token budget configuration.

    All fields are optional. Omitted fields mean no limit for that dimension.

    The warning_fraction controls when the guard emits a non-blocking Need
    mark before the hard stop. The structurally correct value is:

        warning_fraction = 1 - (R * T / B)

    where R is the expected number of rounds before the principal can
    respond, T is the typical tokens consumed per round, and B is the
    total budget. This leaves exactly enough runway for the principal to
    act. The default (4/5) assumes approximately one round's worth of
    budget as runway - appropriate when the agent consumes roughly 1/5
    of its total budget per round and the principal responds within one
    round. Deployments where the principal is slower or rounds are more
    expensive should lower the fraction; deployments with many small
    cheap rounds can raise it.

    Configurable per-agent because the inputs to the formula differ
    per agent: an agent that runs once daily needs a different fraction
    than one that runs every minute.

    Spec Section 9.10.
    """

    model_config = ConfigDict(frozen=True)

    # Per-round limits are caller-level concerns: the caller passes
    # max_input_tokens_per_round to MarkSpace.read() for truncation,
    # and max_output_tokens_per_round to the LLM's max_tokens parameter.
    # The guard does not enforce these - it enforces lifetime totals.
    max_input_tokens_per_round: int | None = None
    max_output_tokens_per_round: int | None = None
    max_input_tokens_total: int | None = None
    max_output_tokens_total: int | None = None
    # Default 4/5: assumes ~1 round of runway before hard stop.
    # See class docstring for the derivation.
    warning_fraction: float = Field(default=4 / 5, gt=0.0, lt=1.0)


@dataclass
class BudgetTracker:
    """Mutable per-agent budget state. One instance per tracked agent.

    The guard creates a tracker when it first sees an agent with a budget.
    Token counts are monotonically non-decreasing (P63).

    Thread safety: the guard holds its own lock when calling these methods.
    """

    total_input_consumed: int = 0
    total_output_consumed: int = 0
    warning_emitted_input: bool = False
    warning_emitted_output: bool = False
    exhausted: bool = False

    def record_input(self, tokens: int) -> None:
        """Record input tokens consumed. Monotonic (P63)."""
        if tokens < 0:
            raise ValueError("Token count must be non-negative")
        self.total_input_consumed += tokens

    def record_output(self, tokens: int) -> None:
        """Record output tokens generated. Monotonic (P63)."""
        if tokens < 0:
            raise ValueError("Token count must be non-negative")
        self.total_output_consumed += tokens

    def check_lifetime(self, budget: TokenBudget) -> BudgetStatus:
        """Check lifetime budget status.

        Returns the most severe status across both dimensions.
        Warning is checked first so it fires even when usage jumps
        past the warning threshold and exhaustion in the same call.
        The guard handles warning emission before the hard stop.
        """
        # Check warnings first. If usage jumps from below warning to
        # above exhaustion in a single call, the warning must still fire
        # (P60) before the hard stop (P61). The guard emits the Need
        # mark and then handles exhaustion on the next check.
        if (
            budget.max_input_tokens_total is not None
            and not self.warning_emitted_input
            and self.total_input_consumed
            >= budget.max_input_tokens_total * budget.warning_fraction
        ):
            return BudgetStatus.WARNING_INPUT
        if (
            budget.max_output_tokens_total is not None
            and not self.warning_emitted_output
            and self.total_output_consumed
            >= budget.max_output_tokens_total * budget.warning_fraction
        ):
            return BudgetStatus.WARNING_OUTPUT

        # Check exhaustion
        if (
            budget.max_input_tokens_total is not None
            and self.total_input_consumed >= budget.max_input_tokens_total
        ):
            self.exhausted = True
            return BudgetStatus.EXHAUSTED_INPUT
        if (
            budget.max_output_tokens_total is not None
            and self.total_output_consumed >= budget.max_output_tokens_total
        ):
            self.exhausted = True
            return BudgetStatus.EXHAUSTED_OUTPUT

        return BudgetStatus.OK

    def is_exhausted(self, budget: TokenBudget) -> bool:
        """Check if any lifetime budget dimension is exhausted.

        P61: once exhausted, stays exhausted until principal increases budget.
        """
        if self.exhausted:
            return True
        if (
            budget.max_input_tokens_total is not None
            and self.total_input_consumed >= budget.max_input_tokens_total
        ):
            self.exhausted = True
            return True
        if (
            budget.max_output_tokens_total is not None
            and self.total_output_consumed >= budget.max_output_tokens_total
        ):
            self.exhausted = True
            return True
        return False

    def try_clear_exhaustion(self, budget: TokenBudget) -> bool:
        """Clear exhaustion if the budget has been increased past consumption.

        P62: principal MAY increase budget to resume the agent.
        Returns True if exhaustion was cleared.
        """
        if not self.exhausted:
            return False
        input_ok = (
            budget.max_input_tokens_total is None
            or self.total_input_consumed < budget.max_input_tokens_total
        )
        output_ok = (
            budget.max_output_tokens_total is None
            or self.total_output_consumed < budget.max_output_tokens_total
        )
        if input_ok and output_ok:
            self.exhausted = False
            return True
        return False
