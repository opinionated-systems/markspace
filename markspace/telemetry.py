# -*- coding: utf-8 -*-
"""
Markspace Coordination Protocol - Telemetry

OpenTelemetry-compatible observability interface for the guard layer.
Emits structured events on every guard decision, plus metrics (counters,
gauges, histograms) for operational dashboards.

Telemetry is informational only - sink failures MUST NOT affect guard
verdicts, mark storage, or coordination semantics (P57).

The interface is OTel-compatible but not OTel-dependent. Deployments
may use any sink implementation: OTel SDK, Prometheus, structured JSON
logs, or a no-op NullSink for deployments that don't need telemetry.

Spec Section 9.11.
"""

from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Metric names (OpenTelemetry naming convention: dot-separated, lowercase)
#
# Guard-emitted (emitted automatically by Guard methods):
#   MARKS_WRITTEN   - write_mark(), execute()
#   TOKENS_INPUT    - record_round_tokens()
#   TOKENS_OUTPUT   - record_round_tokens()
#   CONFLICTS       - _pre_action_inner() when conflict resolution runs
#   BUDGET_REMAINING - record_round_tokens() when budget is set
#
# Caller-emitted (the caller has the data, guard does not):
#   MARKS_READ       - caller instruments space.read() calls
#   SPACE_ACTIVE/TOTAL - caller polls mark space size periodically
#   ROUND_DURATION   - caller times each agent round
#   NEEDS_PENDING    - caller polls aggregate_needs()
# ---------------------------------------------------------------------------

METRIC_MARKS_WRITTEN = "markspace.marks.written"
METRIC_MARKS_READ = "markspace.marks.read"
METRIC_TOKENS_INPUT = "markspace.tokens.input"
METRIC_TOKENS_OUTPUT = "markspace.tokens.output"
METRIC_CONFLICTS_RESOLVED = "markspace.conflicts.resolved"
METRIC_SPACE_ACTIVE_MARKS = "markspace.space.active_marks"
METRIC_SPACE_TOTAL_MARKS = "markspace.space.total_marks"
METRIC_BUDGET_REMAINING = "markspace.agent.budget.remaining"
METRIC_ROUND_DURATION = "markspace.agent.round.duration"
METRIC_NEEDS_PENDING = "markspace.needs.pending"


# ---------------------------------------------------------------------------
# Structured telemetry event
# ---------------------------------------------------------------------------


@dataclass
class TelemetryEvent:
    """Structured log event emitted by the guard on every decision.

    P58: Every write_mark() and execute() call MUST emit an event,
    regardless of whether the operation was accepted or rejected.
    """

    timestamp: float = field(default_factory=time.time)
    agent_id: str = ""
    operation: str = ""  # "write_mark", "pre_action", "post_action", "execute"
    scope: str = ""
    mark_type: str = ""
    verdict: str = ""  # "accepted", "rejected", "conflict", "denied", "allowed"
    conflict_check: bool = False
    conflict_found: bool = False
    envelope_status: str = ""
    barrier_restricted: bool = False
    input_tokens_this_round: int = 0
    output_tokens_this_round: int = 0
    budget_remaining_input: int = -1  # -1 = no budget
    budget_remaining_output: int = -1
    reason: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Sink interface
# ---------------------------------------------------------------------------


class TelemetrySink(ABC):
    """Abstract telemetry sink. One per Guard instance.

    Implementations MUST be safe to call from any thread.
    Implementations SHOULD NOT raise exceptions - the guard wraps
    all calls in try/except, but defensive sinks are better.
    """

    @abstractmethod
    def emit_event(self, event: TelemetryEvent) -> None:
        """Emit a structured log event."""
        ...

    @abstractmethod
    def record_counter(self, name: str, value: float, labels: dict[str, str]) -> None:
        """Increment a counter metric."""
        ...

    @abstractmethod
    def record_gauge(self, name: str, value: float, labels: dict[str, str]) -> None:
        """Set a gauge metric."""
        ...

    @abstractmethod
    def record_histogram(self, name: str, value: float, labels: dict[str, str]) -> None:
        """Record a histogram observation."""
        ...

    def flush(self) -> None:
        """Flush any buffered data. Optional."""


# ---------------------------------------------------------------------------
# NullSink - no-op (default when telemetry is not configured)
# ---------------------------------------------------------------------------


class NullSink(TelemetrySink):
    """No-op sink. All calls are ignored."""

    def emit_event(self, event: TelemetryEvent) -> None:
        pass

    def record_counter(self, name: str, value: float, labels: dict[str, str]) -> None:
        pass

    def record_gauge(self, name: str, value: float, labels: dict[str, str]) -> None:
        pass

    def record_histogram(self, name: str, value: float, labels: dict[str, str]) -> None:
        pass


# ---------------------------------------------------------------------------
# StructuredLogSink - JSON logs via Python logging
# ---------------------------------------------------------------------------


class StructuredLogSink(TelemetrySink):
    """Emits events as structured JSON via Python's logging module.

    Suitable for deployments that pipe logs to a collector (Fluentd,
    Logstash, CloudWatch, etc.) without requiring the OTel SDK.
    """

    def __init__(self, logger_name: str = "markspace.telemetry") -> None:
        self._logger = logging.getLogger(logger_name)

    def emit_event(self, event: TelemetryEvent) -> None:
        data = asdict(event)
        self._logger.info(json.dumps(data, default=str))

    def record_counter(self, name: str, value: float, labels: dict[str, str]) -> None:
        self._logger.debug(
            json.dumps({"metric": name, "type": "counter", "value": value, **labels})
        )

    def record_gauge(self, name: str, value: float, labels: dict[str, str]) -> None:
        self._logger.debug(
            json.dumps({"metric": name, "type": "gauge", "value": value, **labels})
        )

    def record_histogram(self, name: str, value: float, labels: dict[str, str]) -> None:
        self._logger.debug(
            json.dumps({"metric": name, "type": "histogram", "value": value, **labels})
        )


# ---------------------------------------------------------------------------
# InMemorySink - for testing
# ---------------------------------------------------------------------------


class InMemorySink(TelemetrySink):
    """Captures all events and metrics in memory for test assertions."""

    def __init__(self) -> None:
        self.events: list[TelemetryEvent] = []
        self.counters: list[tuple[str, float, dict[str, str]]] = []
        self.gauges: list[tuple[str, float, dict[str, str]]] = []
        self.histograms: list[tuple[str, float, dict[str, str]]] = []

    def emit_event(self, event: TelemetryEvent) -> None:
        self.events.append(event)

    def record_counter(self, name: str, value: float, labels: dict[str, str]) -> None:
        self.counters.append((name, value, labels))

    def record_gauge(self, name: str, value: float, labels: dict[str, str]) -> None:
        self.gauges.append((name, value, labels))

    def record_histogram(self, name: str, value: float, labels: dict[str, str]) -> None:
        self.histograms.append((name, value, labels))

    def clear(self) -> None:
        self.events.clear()
        self.counters.clear()
        self.gauges.clear()
        self.histograms.clear()


# ---------------------------------------------------------------------------
# FailingSink - for testing P57 (non-interference)
# ---------------------------------------------------------------------------


class FailingSink(TelemetrySink):
    """Always raises. Used to test that guard decisions are unaffected."""

    def emit_event(self, event: TelemetryEvent) -> None:
        raise RuntimeError("FailingSink: deliberate failure")

    def record_counter(self, name: str, value: float, labels: dict[str, str]) -> None:
        raise RuntimeError("FailingSink: deliberate failure")

    def record_gauge(self, name: str, value: float, labels: dict[str, str]) -> None:
        raise RuntimeError("FailingSink: deliberate failure")

    def record_histogram(self, name: str, value: float, labels: dict[str, str]) -> None:
        raise RuntimeError("FailingSink: deliberate failure")
