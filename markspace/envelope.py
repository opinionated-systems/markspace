# -*- coding: utf-8 -*-
"""
Statistical Envelope - Behavioral anomaly detection for agents.

Each agent gets its own AnomalyDetector instance that learns the agent's
individual baseline. The envelope orchestrates windowing, per-agent
detector lifecycle, cross-agent concentration checks, and the monotonic
RESTRICTED state machine (P40, P41).

Detection algorithms are pluggable via the AnomalyDetector protocol.
The default WelfordDetector uses streaming mean/variance (Welford's
online algorithm) to flag rate anomalies and type distribution shifts.

Spec Section 9.7.
"""

from __future__ import annotations

import math
import threading
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from markspace.core import AnyMark, MarkType, Observation, Warning


class EnvelopeVerdict(str, Enum):
    NORMAL = "normal"
    FLAGGED = "flagged"  # suspicious, not blocked
    RESTRICTED = "restricted"  # triggers barrier narrowing


# -----------------------------------------------------------------------
# AnomalyDetector protocol
# -----------------------------------------------------------------------


class AnomalyDetector(ABC):
    """Per-agent anomaly detector. One instance per tracked agent.

    The envelope feeds completed window counts via observe(), then asks
    is_anomalous() with the in-progress window to decide whether to
    restrict the agent. Each detector maintains its own baseline -
    no assumption that agents share the same activity pattern.
    """

    @abstractmethod
    def observe(self, window_counts: dict[MarkType, int]) -> None:
        """Feed one completed window's counts into the baseline model."""
        ...

    @abstractmethod
    def is_ready(self) -> bool:
        """Has enough baseline data been collected to detect anomalies?

        Returns False during cold start. The envelope returns NORMAL
        for agents whose detector is not yet ready (P41).
        """
        ...

    @abstractmethod
    def is_anomalous(self, current_counts: dict[MarkType, int]) -> bool:
        """Check if current (in-progress) window counts are anomalous
        relative to this agent's established baseline."""
        ...

    @abstractmethod
    def export_state(self) -> dict[str, Any]:
        """Serialize detector state for persistence/transfer."""
        ...

    @abstractmethod
    def import_state(self, data: dict[str, Any]) -> None:
        """Restore detector state from serialized form."""
        ...

    def seed_baseline(self, expected: dict[MarkType, float]) -> None:  # noqa: ARG002
        """Pre-seed baseline from declared expected activity.

        Optional - detectors that don't support seeding ignore this.
        Called by the envelope when an agent declares expected_activity
        in its manifest.
        """


# -----------------------------------------------------------------------
# WelfordDetector - k-sigma rate anomaly + type shift detection
# -----------------------------------------------------------------------


@dataclass
class WelfordConfig:
    """Configuration for WelfordDetector."""

    k_sigma: float = 3.5  # standard deviations for rate anomaly
    min_samples: int = 10  # minimum windows before flagging
    type_shift_threshold: float = 0.5  # fraction deviation for type shift
    tracked_types: frozenset[MarkType] = frozenset(
        {MarkType.OBSERVATION, MarkType.WARNING}
    )


class WelfordDetector(AnomalyDetector):
    """Streaming mean/variance detector using Welford's online algorithm.

    Establishes per-agent baseline from completed windows, then flags
    when the current window exceeds mean + k*stddev for any tracked
    mark type, or when the type distribution shifts significantly.
    """

    def __init__(self, config: WelfordConfig | None = None) -> None:
        self._config = config or WelfordConfig()
        self._n: dict[MarkType, int] = {}
        self._mean: dict[MarkType, float] = {}
        self._m2: dict[MarkType, float] = {}
        self._completed_windows: int = 0
        self._active_windows: int = 0  # windows with actual marks (excludes idle zeros)
        self._previous_counts: dict[MarkType, int] = {}
        # Cold-start buffer: raw window counts stored until min_samples reached.
        # Once ready, the baseline is initialized from the median (robust to
        # outlier poisoning) instead of from the raw Welford accumulators
        # which an adversary can inflate by bursting during cold start.
        self._cold_buffer: list[dict[MarkType, int]] | None = []
        self._seeded: bool = False  # True if seed_baseline() was called

    def seed_baseline(
        self, expected: dict[MarkType, float], synthetic_windows: int = 0
    ) -> None:
        """Pre-seed Welford accumulators from declared expected activity.

        If synthetic_windows is 0, uses min_samples so the detector starts
        ready immediately. The variance is set to a reasonable default
        (25% of the mean squared) to avoid zero-variance false positives.

        This lets agent creators declare "I expect ~N observations per window"
        and the detector uses that as the starting baseline, refined by
        real observations over time.
        """
        n = synthetic_windows or self._config.min_samples
        for mt, expected_count in expected.items():
            if mt not in self._config.tracked_types:
                continue
            self._n[mt] = n
            self._mean[mt] = expected_count
            # Synthetic variance: 25% of mean squared, gives stddev = 0.5 * mean.
            # Real observations will refine this. Avoids zero-variance traps
            # where any deviation from the exact declared count triggers.
            self._m2[mt] = (0.25 * expected_count**2) * (n - 1) if n > 1 else 0.0
        self._completed_windows = max(self._completed_windows, n)
        self._active_windows = max(self._active_windows, n)
        self._seeded = True
        self._cold_buffer = None  # no need for robust init when seeded

    def observe(self, window_counts: dict[MarkType, int]) -> None:
        has_marks = any(
            window_counts.get(mt, 0) > 0 for mt in self._config.tracked_types
        )

        # During cold start (not seeded), buffer observations for robust init
        if self._cold_buffer is not None:
            self._cold_buffer.append(dict(window_counts))
            self._previous_counts = dict(window_counts)
            self._completed_windows += 1
            if has_marks:
                self._active_windows += 1
            # When buffer reaches min_samples active windows, init from median
            active_in_buffer = sum(
                1
                for w in self._cold_buffer
                if any(w.get(mt, 0) > 0 for mt in self._config.tracked_types)
            )
            if active_in_buffer >= self._config.min_samples:
                self._init_from_buffer()
            return

        # Normal streaming update (post cold start)
        for mt in self._config.tracked_types:
            count = float(window_counts.get(mt, 0))
            n = self._n.get(mt, 0) + 1
            self._n[mt] = n
            old_mean = self._mean.get(mt, 0.0)
            new_mean = old_mean + (count - old_mean) / n
            self._mean[mt] = new_mean
            old_m2 = self._m2.get(mt, 0.0)
            self._m2[mt] = old_m2 + (count - old_mean) * (count - new_mean)
        self._previous_counts = dict(window_counts)
        self._completed_windows += 1
        if has_marks:
            self._active_windows += 1

    def is_ready(self) -> bool:
        return self._active_windows >= self._config.min_samples

    def is_anomalous(self, current_counts: dict[MarkType, int]) -> bool:
        return self._check_rate(current_counts) or self._check_type_shift(
            current_counts
        )

    def export_state(self) -> dict[str, Any]:
        return {
            "completed_windows": self._completed_windows,
            "active_windows": self._active_windows,
            "welford_n": {k.value: v for k, v in self._n.items()},
            "welford_mean": {k.value: v for k, v in self._mean.items()},
            "welford_m2": {k.value: v for k, v in self._m2.items()},
        }

    def import_state(self, data: dict[str, Any]) -> None:
        self._completed_windows = max(
            self._completed_windows, data.get("completed_windows", 0)
        )
        self._active_windows = max(
            self._active_windows,
            data.get("active_windows", data.get("completed_windows", 0)),
        )
        for k, v in data.get("welford_n", {}).items():
            self._n[MarkType(k)] = v
        for k, v in data.get("welford_mean", {}).items():
            self._mean[MarkType(k)] = v
        for k, v in data.get("welford_m2", {}).items():
            self._m2[MarkType(k)] = v
        # Imported state is already initialized - skip cold buffer
        self._cold_buffer = None

    # -- Private --

    def _init_from_buffer(self) -> None:
        """Initialize Welford accumulators from buffered cold-start windows
        using median and MAD (median absolute deviation).

        Why not use the Welford accumulators directly? During cold start,
        an adversary can burst (e.g., 15 marks in round 3 of 8) to inflate
        its own mean and variance. By the time the detector is ready, the
        inflated baseline makes adversarial activity look normal. A single
        outlier window shifts the mean by (outlier - mean) / n.

        The median's breakdown point is 50% (Hampel, 1974) - an adversary
        must control more than half the cold-start windows to shift it.
        One burst among 8 windows cannot move the median at all. After
        cold start, streaming Welford takes over for O(1) updates.
        """
        assert self._cold_buffer is not None  # noqa: S101
        buf = self._cold_buffer

        for mt in self._config.tracked_types:
            values = sorted(float(w.get(mt, 0)) for w in buf)
            n = len(values)
            if n == 0:
                continue

            # Median
            if n % 2 == 1:
                median = values[n // 2]
            else:
                median = (values[n // 2 - 1] + values[n // 2]) / 2.0

            # MAD (median absolute deviation), scaled to match stddev.
            # 1.4826 = 1/Phi^{-1}(3/4): the factor that makes MAD a
            # consistent estimator of stddev under normality. Not a
            # tuning parameter - derived from the normal distribution.
            deviations = sorted(abs(v - median) for v in values)
            if n % 2 == 1:
                mad = deviations[n // 2]
            else:
                mad = (deviations[n // 2 - 1] + deviations[n // 2]) / 2.0
            robust_std = 1.4826 * mad

            # Initialize Welford accumulators from robust estimates
            self._n[mt] = n
            self._mean[mt] = median
            # Reconstruct M2 so _stddev() returns robust_std:
            # stddev = sqrt(M2 / (n-1)), so M2 = robust_std^2 * (n-1)
            self._m2[mt] = (robust_std**2) * (n - 1) if n > 1 else 0.0

        # Cold start complete - switch to streaming mode
        self._cold_buffer = None

    def _stddev(self, mt: MarkType) -> float:
        n = self._n.get(mt, 0)
        if n < 2:
            return 0.0
        m2 = self._m2.get(mt, 0.0)
        return math.sqrt(max(0.0, m2 / (n - 1)))

    def _check_rate(self, current_counts: dict[MarkType, int]) -> bool:
        for mt in self._config.tracked_types:
            current = current_counts.get(mt, 0)
            mean = self._mean.get(mt, 0.0)
            sample_std = self._stddev(mt)
            # Poisson variance floor. Count data (discrete events per window)
            # has inherent variance >= mean - a property of Poisson processes.
            # Welford can learn sample variance below this for consistent
            # agents (e.g., always ~1 mark/window -> sample stddev ~0.3,
            # but Poisson stddev = sqrt(1) = 1.0). Without this floor,
            # threshold = 1.0 + 3.5*0.3 = 2.05, flagging a normal agent
            # who happens to write 3 marks. With the floor: 1.0 + 3.5*1.0
            # = 4.5, which correctly tolerates normal fluctuation.
            # This is a structural bound, not a tuning parameter.
            poisson_std = math.sqrt(max(mean, 0.0))
            stddev = max(sample_std, poisson_std)
            threshold = mean + self._config.k_sigma * stddev
            if threshold > 0 and current > threshold:
                return True
        return False

    def _check_type_shift(self, current_counts: dict[MarkType, int]) -> bool:
        total_current = sum(
            current_counts.get(mt, 0) for mt in self._config.tracked_types
        )
        if total_current == 0:
            return False
        total_historical = sum(
            self._mean.get(mt, 0.0) for mt in self._config.tracked_types
        )
        if total_historical == 0:
            return False
        for mt in self._config.tracked_types:
            current_frac = current_counts.get(mt, 0) / total_current
            hist_mean = self._mean.get(mt, 0.0)
            hist_frac = hist_mean / total_historical
            if abs(current_frac - hist_frac) > self._config.type_shift_threshold:
                return True
        return False

    @property
    def completed_windows(self) -> int:
        return self._completed_windows

    def diagnostic_stats(self) -> dict[str, Any]:
        """Return a snapshot of detector internals for diagnostics.

        Provides a public API for inspecting detector state without
        accessing private attributes directly.
        """
        return {
            "n": dict(self._n),
            "mean": dict(self._mean),
            "m2": dict(self._m2),
            "completed_windows": self._completed_windows,
            "active_windows": self._active_windows,
        }


# -----------------------------------------------------------------------
# WindowTracker - per-agent windowing (internal to envelope)
# -----------------------------------------------------------------------


@dataclass
class _WindowCounts:
    """Counts for a single tumbling window."""

    counts: dict[MarkType, int] = field(default_factory=dict)
    start: float = 0.0


@dataclass
class _AgentState:
    """Per-agent state managed by the envelope."""

    detector: AnomalyDetector
    current_window: _WindowCounts = field(default_factory=_WindowCounts)
    restricted: bool = False


# -----------------------------------------------------------------------
# EnvelopeConfig and StatisticalEnvelope
# -----------------------------------------------------------------------


@dataclass
class EnvelopeConfig:
    """Configuration for the StatisticalEnvelope.

    The detector_factory creates a fresh AnomalyDetector for each new
    agent. Each agent's detector learns its own baseline independently.
    """

    window_seconds: float = 300.0  # tumbling window duration
    detector_factory: Callable[[uuid.UUID], AnomalyDetector] = field(
        default_factory=lambda: lambda _agent_id: WelfordDetector()
    )
    tracked_types: frozenset[MarkType] = frozenset(
        {MarkType.OBSERVATION, MarkType.WARNING}
    )
    concentration_threshold: int = 3  # agents on same scope+topic = FLAGGED
    global_escalation_threshold: int = 3  # flags before global restriction
    exempt_agents: set[uuid.UUID] = field(default_factory=set)


# Backwards-compatible alias: AgentStats is used by tests and run.py
# for diagnostics. Wraps the new internal state.
@dataclass
class AgentStats:
    """Diagnostic view of per-agent envelope state.

    Provides backwards-compatible access to detector internals for
    tests and experiment analysis. Not used internally by the envelope.
    """

    current_window: _WindowCounts = field(default_factory=_WindowCounts)
    previous_window: _WindowCounts = field(default_factory=_WindowCounts)
    # Welford fields (populated from WelfordDetector if available)
    welford_n: dict[MarkType, int] = field(default_factory=dict)
    welford_mean: dict[MarkType, float] = field(default_factory=dict)
    welford_m2: dict[MarkType, float] = field(default_factory=dict)
    completed_windows: int = 0
    restricted: bool = False


class StatisticalEnvelope:
    """
    Behavioral anomaly detection for agent write patterns.

    Each agent gets its own AnomalyDetector instance created by the
    configured detector_factory. The envelope handles windowing,
    monotonic restriction (P40), cold start delegation (P41), and
    cross-agent concentration detection.

    P40: Envelope Monotonicity - RESTRICTED is sticky until principal reset.
    P41: Cold Start Safety - NORMAL while detector.is_ready() is False.
    """

    def __init__(
        self,
        config: EnvelopeConfig | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._config = config or EnvelopeConfig()
        self._clock = clock or time.time
        self._lock = threading.RLock()
        self._agents: dict[uuid.UUID, _AgentState] = {}
        # Cross-agent: (scope, topic) -> set of agent_ids in current window
        self._recent_writers: dict[tuple[str, str], set[uuid.UUID]] = {}

    # -- Backwards compatibility: _stats property for run.py snapshot --
    @property
    def _stats(self) -> dict[uuid.UUID, AgentStats]:
        """Backwards-compatible access for experiment code that reads _stats."""
        result: dict[uuid.UUID, AgentStats] = {}
        for agent_id, state in self._agents.items():
            result[agent_id] = self._make_agent_stats(state)
        return result

    def _make_agent_stats(self, state: _AgentState) -> AgentStats:
        """Build an AgentStats view from internal state."""
        stats = AgentStats(
            current_window=state.current_window,
            restricted=state.restricted,
        )
        if isinstance(state.detector, WelfordDetector):
            diag = state.detector.diagnostic_stats()
            stats.welford_n = diag["n"]
            stats.welford_mean = diag["mean"]
            stats.welford_m2 = diag["m2"]
            stats.completed_windows = diag["completed_windows"]
        return stats

    @property
    def global_escalation_threshold(self) -> int:
        """Number of flags before global restriction is applied."""
        return self._config.global_escalation_threshold

    def add_exempt_agent(self, agent_id: uuid.UUID) -> None:
        """Add an agent ID to the exempt set. record() ignores exempt agents."""
        with self._lock:
            self._config.exempt_agents.add(agent_id)

    def seed_baseline(
        self,
        agent_id: uuid.UUID,
        expected_per_hour: dict[MarkType, float],
    ) -> None:
        """Pre-seed an agent's detector from declared expected activity.

        expected_per_hour: MarkType -> expected marks per hour. The envelope
        converts to per-window counts using window_seconds, so agent creators
        don't need to know the window size.

        Call this at agent registration time, before the agent starts writing.
        The detector starts with a baseline matching the declared activity,
        so deviations are flagged immediately (no cold-start blind spot).
        """
        # Convert per-hour rates to per-window counts
        window_hours = self._config.window_seconds / 3600.0
        per_window = {mt: rate * window_hours for mt, rate in expected_per_hour.items()}
        with self._lock:
            state = self._agents.get(agent_id)
            if state is None:
                state = _AgentState(
                    detector=self._config.detector_factory(agent_id),
                    current_window=_WindowCounts(start=self._clock()),
                )
                self._agents[agent_id] = state
            state.detector.seed_baseline(per_window)

    def record(self, agent_id: uuid.UUID, mark: AnyMark) -> None:
        """Called via post-write hook on every space.write()."""
        if agent_id in self._config.exempt_agents:
            return
        if mark.mark_type not in self._config.tracked_types:
            return

        with self._lock:
            now = self._clock()
            state = self._agents.get(agent_id)
            if state is None:
                state = _AgentState(
                    detector=self._config.detector_factory(agent_id),
                    current_window=_WindowCounts(start=now),
                )
                self._agents[agent_id] = state

            self._maybe_rotate(state, now, agent_id)

            state.current_window.counts[mark.mark_type] = (
                state.current_window.counts.get(mark.mark_type, 0) + 1
            )

            # Track concentration (scope, topic) for observations/warnings
            key = self._concentration_key(mark)
            if key is not None:
                self._recent_writers.setdefault(key, set()).add(agent_id)

    def record_attempt(self, agent_id: uuid.UUID, mark_type: MarkType) -> None:
        """Record a rejected action attempt (e.g., conflict) in the envelope.

        When guard._pre_action_inner() rejects an action due to resource
        conflict, the agent produced no marks - making repeated conflict
        attempts invisible to anomaly detection. This method makes those
        attempts visible by incrementing the window count and tracking the
        target, without writing an actual mark to the space.
        """
        if agent_id in self._config.exempt_agents:
            return
        if mark_type not in self._config.tracked_types:
            return

        with self._lock:
            now = self._clock()
            state = self._agents.get(agent_id)
            if state is None:
                state = _AgentState(
                    detector=self._config.detector_factory(agent_id),
                    current_window=_WindowCounts(start=now),
                )
                self._agents[agent_id] = state

            self._maybe_rotate(state, now, agent_id)

            state.current_window.counts[mark_type] = (
                state.current_window.counts.get(mark_type, 0) + 1
            )

    def check(self, agent_id: uuid.UUID) -> EnvelopeVerdict:
        """Check if an agent's behavior is anomalous."""
        with self._lock:
            state = self._agents.get(agent_id)
            if state is None:
                return EnvelopeVerdict.NORMAL

            # P40: Monotonicity - once RESTRICTED, stays RESTRICTED
            if state.restricted:
                return EnvelopeVerdict.RESTRICTED

            # Drain stale windows
            self._maybe_rotate(state, self._clock(), agent_id)

            # P41: Cold start - delegate to detector's readiness
            if not state.detector.is_ready():
                return EnvelopeVerdict.NORMAL

            # Ask detector if current window is anomalous
            if state.detector.is_anomalous(dict(state.current_window.counts)):
                state.restricted = True
                return EnvelopeVerdict.RESTRICTED

            # Cross-agent concentration check (FLAGGED only)
            if self._check_concentration(agent_id):
                return EnvelopeVerdict.FLAGGED

            return EnvelopeVerdict.NORMAL

    def get_stats(self, agent_id: uuid.UUID) -> AgentStats | None:
        """Read-only diagnostic view of per-agent state."""
        with self._lock:
            state = self._agents.get(agent_id)
            if state is None:
                return None
            return self._make_agent_stats(state)

    def reset(self, agent_id: uuid.UUID, principal_token: uuid.UUID) -> bool:
        """Reset an agent's RESTRICTED status. Requires principal token.

        Note: principal_token validation is the caller's responsibility.
        """
        with self._lock:
            state = self._agents.get(agent_id)
            if state is None or not state.restricted:
                return False
            state.restricted = False
            return True

    def export_stats(self) -> dict[str, Any]:
        """Export all agent detector states as a serializable dict."""
        with self._lock:
            result: dict[str, Any] = {}
            for agent_id, state in self._agents.items():
                exported = state.detector.export_state()
                exported["restricted"] = state.restricted
                result[str(agent_id)] = exported
            return result

    def import_stats(self, data: dict[str, Any]) -> None:
        """Import previously exported stats. Additive."""
        with self._lock:
            for agent_id_str, stat_data in data.items():
                agent_id = uuid.UUID(agent_id_str)
                state = self._agents.get(agent_id)
                if state is None:
                    state = _AgentState(
                        detector=self._config.detector_factory(agent_id),
                        current_window=_WindowCounts(start=self._clock()),
                    )
                    self._agents[agent_id] = state
                state.detector.import_state(stat_data)
                state.restricted = stat_data.get("restricted", False)

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _maybe_rotate(
        self, state: _AgentState, now: float, agent_id: uuid.UUID | None = None
    ) -> None:
        """Rotate window if expired. Feed completed window into detector.

        Clock gaps (agent idle or time jump) are ignored - the baseline
        reflects what the agent does when active, not how often it is
        idle. Feeding zero-count windows would deflate the mean and
        create false positives when the agent resumes normal activity.
        """
        ws = self._config.window_seconds
        window_end = state.current_window.start + ws
        if now < window_end:
            return

        # Feed current window into detector (only if it had activity)
        counts = dict(state.current_window.counts)
        if counts:
            state.detector.observe(counts)

        # Start new current window
        state.current_window = _WindowCounts(start=now)

        # Remove this agent from concentration tracking
        if agent_id is not None:
            for writers in self._recent_writers.values():
                writers.discard(agent_id)

    def _check_concentration(self, agent_id: uuid.UUID) -> bool:
        """Check if 3+ agents wrote to the same (scope, topic) in this window."""
        for _key, writers in self._recent_writers.items():
            if (
                agent_id in writers
                and len(writers) >= self._config.concentration_threshold
            ):
                return True
        return False

    @staticmethod
    def _concentration_key(mark: AnyMark) -> tuple[str, str] | None:
        """Extract (scope, topic) for concentration tracking."""
        if isinstance(mark, (Observation, Warning)):
            return (mark.scope, mark.topic)
        return None
