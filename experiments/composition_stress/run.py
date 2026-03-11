#!/usr/bin/env python3
"""
Composition Stress Test - Deterministic Pipeline Network

Validates composition properties under concurrent execution.
No LLMs - all agents are deterministic functions.

Scenario: Sensor data pipeline network
  SensorAgent(5) --obs--> FilterAgent(3) --obs--> AggregatorAgent(2)
       --obs--> AlertAgent(2) --warn--> ActionAgent(2)

Composition patterns validated:
  1. Linear pipeline - each stage reads from previous, writes to next
  2. Fan-in - 5 sensors feed 3 filters
  3. Fan-out - 2 alert agents both watch the same scope
  4. Reactive activation - agents only act when get_watched_marks() returns data
  5. Manifest validation - validate_pipeline() runs before simulation starts
  6. Hot-swap - mid-run, replace one FilterAgent with different threshold
  7. Concurrency - all agents in each stage run in parallel

Usage: python -m experiments.composition_stress.run
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from random import Random

from markspace import (
    Action,
    Agent,
    AgentManifest,
    ConflictPolicy,
    DecayConfig,
    Guard,
    MarkSpace,
    MarkType,
    Observation,
    Scope,
    Source,
    Warning,
    WatchPattern,
    hours,
    validate_manifest_permissions,
    validate_pipeline,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

N_TICKS = 20
N_SENSORS = 5
N_FILTERS = 3
N_AGGREGATORS = 2
N_ALERTERS = 2
N_ACTORS = 2
FILTER_THRESHOLD = 50.0
AGGREGATE_BATCH_SIZE = 3
ALERT_THRESHOLD = 75.0
SEED = 42
WORKERS = 10

# ---------------------------------------------------------------------------
# Agent factory functions
# ---------------------------------------------------------------------------


def make_sensor_agent(idx: int) -> Agent:
    return Agent(
        name=f"sensor-{idx}",
        scopes={"sensors": ["observation"]},
        manifest=AgentManifest(
            inputs=(),
            outputs=(("sensors", MarkType.OBSERVATION),),
        ),
    )


def make_filter_agent(idx: int, threshold: float = FILTER_THRESHOLD) -> Agent:
    """Create a filter agent. threshold is stored as part of agent name for tracing."""
    return Agent(
        name=f"filter-{idx}-t{threshold:.0f}",
        scopes={"filtered": ["observation"]},
        read_scopes=frozenset({"sensors"}),
        manifest=AgentManifest(
            inputs=(WatchPattern(scope="sensors", mark_type=MarkType.OBSERVATION),),
            outputs=(("filtered", MarkType.OBSERVATION),),
        ),
    )


def make_aggregator_agent(idx: int) -> Agent:
    return Agent(
        name=f"aggregator-{idx}",
        scopes={"aggregated": ["observation"]},
        read_scopes=frozenset({"filtered"}),
        manifest=AgentManifest(
            inputs=(WatchPattern(scope="filtered", mark_type=MarkType.OBSERVATION),),
            outputs=(("aggregated", MarkType.OBSERVATION),),
        ),
    )


def make_alerter_agent(idx: int, threshold: float = ALERT_THRESHOLD) -> Agent:
    """Create an alerter agent. threshold stored in name for tracing."""
    return Agent(
        name=f"alerter-{idx}-t{threshold:.0f}",
        scopes={"alerts": ["warning"]},
        read_scopes=frozenset({"aggregated"}),
        manifest=AgentManifest(
            inputs=(WatchPattern(scope="aggregated", mark_type=MarkType.OBSERVATION),),
            outputs=(("alerts", MarkType.WARNING),),
        ),
    )


def make_audit_aggregator_agent(idx: int) -> Agent:
    """Aggregator with additional aggregated-audit write scope (for permission-change swap)."""
    return Agent(
        name=f"aggregator-{idx}-audit",
        scopes={
            "aggregated": ["observation"],
            "aggregated-audit": ["observation"],
        },
        read_scopes=frozenset({"filtered"}),
        manifest=AgentManifest(
            inputs=(WatchPattern(scope="filtered", mark_type=MarkType.OBSERVATION),),
            outputs=(
                ("aggregated", MarkType.OBSERVATION),
                ("aggregated-audit", MarkType.OBSERVATION),
            ),
        ),
    )


def make_actor_agent(idx: int) -> Agent:
    return Agent(
        name=f"actor-{idx}",
        scopes={"responses": ["intent", "action"]},
        read_scopes=frozenset({"alerts"}),
        manifest=AgentManifest(
            inputs=(WatchPattern(scope="alerts", mark_type=MarkType.WARNING),),
            outputs=(
                ("responses", MarkType.INTENT),
                ("responses", MarkType.ACTION),
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Scope definitions
# ---------------------------------------------------------------------------


def make_scopes() -> list[Scope]:
    decay = DecayConfig(
        observation_half_life=hours(6),
        warning_half_life=hours(2),
        intent_ttl=hours(2),
    )
    return [
        Scope(
            name="sensors",
            observation_topics=("reading",),
            decay=decay,
            conflict_policy=ConflictPolicy.FIRST_WRITER,
        ),
        Scope(
            name="filtered",
            observation_topics=("reading",),
            decay=decay,
            conflict_policy=ConflictPolicy.FIRST_WRITER,
        ),
        Scope(
            name="aggregated",
            observation_topics=("summary",),
            decay=decay,
            conflict_policy=ConflictPolicy.FIRST_WRITER,
        ),
        Scope(
            name="aggregated-audit",
            observation_topics=("summary",),
            decay=decay,
            conflict_policy=ConflictPolicy.FIRST_WRITER,
        ),
        Scope(
            name="alerts",
            warning_topics=("threshold_exceeded",),
            decay=decay,
            conflict_policy=ConflictPolicy.FIRST_WRITER,
        ),
        Scope(
            name="responses",
            allowed_intent_verbs=("respond",),
            allowed_action_verbs=("responded",),
            decay=decay,
            conflict_policy=ConflictPolicy.FIRST_WRITER,
        ),
    ]


# ---------------------------------------------------------------------------
# Agent tick functions (deterministic, no LLMs)
# ---------------------------------------------------------------------------


@dataclass
class Metrics:
    """Mutable metrics collector."""

    marks_written: int = 0
    sensor_observations: int = 0
    filtered_observations: int = 0
    aggregated_summaries: int = 0
    alerts_raised: int = 0
    actions_taken: int = 0
    duplicate_deliveries: int = 0
    hot_swap_pre_marks: int = 0
    hot_swap_post_marks: int = 0

    # Per-tick tracking for filter thresholds
    filter_thresholds_used: list[float] = field(default_factory=list)


def sensor_tick(
    agent: Agent,
    space: MarkSpace,
    rng: Random,
    tick: int,
    metrics: Metrics,
) -> None:
    """One sensor produces one reading per tick."""
    value = rng.uniform(0, 100)
    space.write(
        agent,
        Observation(
            scope="sensors",
            topic="reading",
            content={"sensor": agent.name, "value": value, "tick": tick},
            source=Source.FLEET,
            confidence=0.9,
        ),
    )
    metrics.sensor_observations += 1
    metrics.marks_written += 1


def filter_tick(
    agent: Agent,
    space: MarkSpace,
    threshold: float,
    metrics: Metrics,
) -> None:
    """Filter checks watched marks and forwards qualifying ones."""
    marks = space.get_watched_marks(agent)
    for mark in marks:
        if isinstance(mark, Observation) and mark.content.get("value", 0) > threshold:
            space.write(
                agent,
                Observation(
                    scope="filtered",
                    topic="reading",
                    content={
                        **mark.content,
                        "filtered_by": agent.name,
                        "threshold": threshold,
                    },
                    source=Source.FLEET,
                    confidence=mark.confidence,
                ),
            )
            metrics.filtered_observations += 1
            metrics.marks_written += 1


def aggregator_tick(
    agent: Agent,
    space: MarkSpace,
    batch_size: int,
    metrics: Metrics,
) -> None:
    """Aggregator combines N filtered readings into a summary."""
    marks = space.get_watched_marks(agent)
    if not marks:
        return

    # Process in batches
    batch: list[float] = []
    batch_ticks: list[int] = []
    for mark in marks:
        if isinstance(mark, Observation):
            batch.append(mark.content.get("value", 0))
            batch_ticks.append(mark.content.get("tick", -1))

            if len(batch) >= batch_size:
                avg = sum(batch) / len(batch)
                space.write(
                    agent,
                    Observation(
                        scope="aggregated",
                        topic="summary",
                        content={
                            "avg_value": avg,
                            "count": len(batch),
                            "ticks": batch_ticks[:],
                            "aggregator": agent.name,
                        },
                        source=Source.FLEET,
                        confidence=0.85,
                    ),
                )
                metrics.aggregated_summaries += 1
                metrics.marks_written += 1
                batch.clear()
                batch_ticks.clear()

    # Flush remaining partial batch
    if batch:
        avg = sum(batch) / len(batch)
        space.write(
            agent,
            Observation(
                scope="aggregated",
                topic="summary",
                content={
                    "avg_value": avg,
                    "count": len(batch),
                    "ticks": batch_ticks[:],
                    "aggregator": agent.name,
                },
                source=Source.FLEET,
                confidence=0.85,
            ),
        )
        metrics.aggregated_summaries += 1
        metrics.marks_written += 1


def audit_aggregator_tick(
    agent: Agent,
    space: MarkSpace,
    batch_size: int,
    metrics: Metrics,
) -> None:
    """Aggregator that writes summaries to both aggregated AND aggregated-audit."""
    marks = space.get_watched_marks(agent)
    if not marks:
        return

    batch: list[float] = []
    batch_ticks: list[int] = []
    for mark in marks:
        if isinstance(mark, Observation):
            batch.append(mark.content.get("value", 0))
            batch_ticks.append(mark.content.get("tick", -1))

            if len(batch) >= batch_size:
                avg = sum(batch) / len(batch)
                content = {
                    "avg_value": avg,
                    "count": len(batch),
                    "ticks": batch_ticks[:],
                    "aggregator": agent.name,
                }
                # Write to primary scope
                space.write(
                    agent,
                    Observation(
                        scope="aggregated",
                        topic="summary",
                        content=content,
                        source=Source.FLEET,
                        confidence=0.85,
                    ),
                )
                # Write audit copy
                space.write(
                    agent,
                    Observation(
                        scope="aggregated-audit",
                        topic="summary",
                        content=content,
                        source=Source.FLEET,
                        confidence=0.85,
                    ),
                )
                metrics.aggregated_summaries += 1
                metrics.marks_written += 2  # both scopes
                batch.clear()
                batch_ticks.clear()

    if batch:
        avg = sum(batch) / len(batch)
        content = {
            "avg_value": avg,
            "count": len(batch),
            "ticks": batch_ticks[:],
            "aggregator": agent.name,
        }
        space.write(
            agent,
            Observation(
                scope="aggregated",
                topic="summary",
                content=content,
                source=Source.FLEET,
                confidence=0.85,
            ),
        )
        space.write(
            agent,
            Observation(
                scope="aggregated-audit",
                topic="summary",
                content=content,
                source=Source.FLEET,
                confidence=0.85,
            ),
        )
        metrics.aggregated_summaries += 1
        metrics.marks_written += 2


def alerter_tick(
    agent: Agent,
    space: MarkSpace,
    threshold: float,
    metrics: Metrics,
) -> None:
    """Alerter checks aggregated summaries and raises warnings."""
    marks = space.get_watched_marks(agent)
    for mark in marks:
        if isinstance(mark, Observation):
            avg = mark.content.get("avg_value", 0)
            if avg > threshold:
                space.write(
                    agent,
                    Warning(
                        scope="alerts",
                        topic="threshold_exceeded",
                        reason=f"Average {avg:.1f} exceeds threshold {threshold}",
                    ),
                )
                metrics.alerts_raised += 1
                metrics.marks_written += 1


def actor_tick(
    agent: Agent,
    space: MarkSpace,
    guard: Guard,
    metrics: Metrics,
    tick: int,
) -> None:
    """Actor responds to alerts by writing intent + action through guard."""
    marks = space.get_watched_marks(agent)
    for mark in marks:
        if isinstance(mark, Warning):
            resource = f"alert-response-tick-{tick}-{agent.name}"
            decision, _result = guard.execute(
                agent=agent,
                scope="responses",
                resource=resource,
                intent_action="respond",
                result_action="responded",
                tool_fn=lambda: {"responded_to": str(mark.id), "by": agent.name},
                confidence=0.9,
            )
            if decision.verdict.value == "allow":
                metrics.actions_taken += 1
                metrics.marks_written += 2  # intent + action


# ---------------------------------------------------------------------------
# Main stress test
# ---------------------------------------------------------------------------


def run_stress_test(seed: int = SEED) -> dict:
    """Run the composition stress test. Returns metrics dict."""
    rng = Random(seed)
    t0 = time.monotonic()

    # 1. Create environment
    scopes = make_scopes()
    space = MarkSpace(scopes=scopes)
    space.set_clock(1_000_000.0)
    guard = Guard(space)

    # 2. Create agents with manifests
    sensors = [make_sensor_agent(i) for i in range(N_SENSORS)]
    filters = [make_filter_agent(i) for i in range(N_FILTERS)]
    aggregators = [make_aggregator_agent(i) for i in range(N_AGGREGATORS)]
    alerters = [make_alerter_agent(i) for i in range(N_ALERTERS)]
    actors = [make_actor_agent(i) for i in range(N_ACTORS)]

    # Per-agent thresholds (mutable, updated on hot-swap)
    filter_thresholds = [FILTER_THRESHOLD] * N_FILTERS
    alerter_thresholds = [ALERT_THRESHOLD] * N_ALERTERS
    # Track which aggregators use audit tick (indices swapped to audit version)
    aggregator_is_audit: list[bool] = [False] * N_AGGREGATORS

    # 3. Validate manifests against permissions (P54)
    all_agents = sensors + filters + aggregators + alerters + actors
    for agent in all_agents:
        errors = validate_manifest_permissions(agent)
        assert not errors, f"Manifest validation failed for {agent.name}: {errors}"

    # 4. Validate pipeline structure (P53)
    # Validate representative pipeline: sensor -> filter -> aggregator -> alerter -> actor
    pipeline_errors = validate_pipeline(
        [sensors[0], filters[0], aggregators[0], alerters[0], actors[0]]
    )
    assert not pipeline_errors, f"Pipeline validation failed: {pipeline_errors}"

    # 5. Subscribe agents to their input patterns
    for agent in filters + aggregators + alerters + actors:
        assert agent.manifest is not None
        space.subscribe(agent, list(agent.manifest.inputs))

    # 6. Run simulation
    metrics = Metrics()
    # Swap schedule:
    #   tick 7:  filter-1 swapped mid-processing (after sensors, before filters)
    #   tick 10: filter-0 + alerter-0 swapped simultaneously (before any processing)
    #   tick 14: aggregator-0 swapped to audit version (different permissions, non-leaf)
    SWAP_FILTER1_TICK = 7
    SWAP_SIMULTANEOUS_TICK = 10
    SWAP_AGGREGATOR_TICK = 14

    print(f"Running {N_TICKS} ticks with {len(all_agents)} agents...")
    print(
        f"Pipeline: {N_SENSORS} sensors -> {N_FILTERS} filters -> "
        f"{N_AGGREGATORS} aggregators -> {N_ALERTERS} alerters -> {N_ACTORS} actors"
    )
    print(
        f"Hot-swaps: tick {SWAP_FILTER1_TICK} (filter-1, mid-processing), "
        f"tick {SWAP_SIMULTANEOUS_TICK} (filter-0 + alerter-0 simultaneous), "
        f"tick {SWAP_AGGREGATOR_TICK} (aggregator-0, permission change)"
    )
    print()

    for tick in range(N_TICKS):
        space.set_clock(1_000_000.0 + tick * 60.0)

        # --- Simultaneous swap at tick 10: filter-0 + alerter-0 ---
        if tick == SWAP_SIMULTANEOUS_TICK:
            # Swap filter-0: threshold 50 -> 30
            old_filter = filters[0]
            space.unsubscribe(old_filter)
            new_filter = make_filter_agent(0, threshold=30.0)
            assert new_filter.manifest is not None
            space.subscribe(new_filter, list(new_filter.manifest.inputs))
            filters[0] = new_filter
            filter_thresholds[0] = 30.0
            assert not validate_manifest_permissions(new_filter)

            # Swap alerter-0: threshold 75 -> 60
            old_alerter = alerters[0]
            space.unsubscribe(old_alerter)
            new_alerter = make_alerter_agent(0, threshold=60.0)
            assert new_alerter.manifest is not None
            space.subscribe(new_alerter, list(new_alerter.manifest.inputs))
            alerters[0] = new_alerter
            alerter_thresholds[0] = 60.0
            assert not validate_manifest_permissions(new_alerter)

        # --- Aggregator swap at tick 14: different permissions (non-leaf) ---
        if tick == SWAP_AGGREGATOR_TICK:
            old_agg = aggregators[0]
            space.unsubscribe(old_agg)
            new_agg = make_audit_aggregator_agent(0)
            assert new_agg.manifest is not None
            space.subscribe(new_agg, list(new_agg.manifest.inputs))
            aggregators[0] = new_agg
            aggregator_is_audit[0] = True
            assert not validate_manifest_permissions(new_agg)

        # Phase 1: Sensors write (concurrent)
        with ThreadPoolExecutor(max_workers=WORKERS) as executor:
            futures = [
                executor.submit(
                    sensor_tick, s, space, Random(rng.randint(0, 2**32)), tick, metrics
                )
                for s in sensors
            ]
            for f in as_completed(futures):
                f.result()

        # --- Mid-processing swap at tick 7: after sensors, before filters ---
        if tick == SWAP_FILTER1_TICK:
            old_filter1 = filters[1]
            space.unsubscribe(old_filter1)
            new_filter1 = make_filter_agent(1, threshold=70.0)
            assert new_filter1.manifest is not None
            space.subscribe(new_filter1, list(new_filter1.manifest.inputs))
            filters[1] = new_filter1
            filter_thresholds[1] = 70.0
            assert not validate_manifest_permissions(new_filter1)

        # Phase 2: Filters process (concurrent)
        with ThreadPoolExecutor(max_workers=WORKERS) as executor:
            futures = [
                executor.submit(
                    filter_tick, filters[i], space, filter_thresholds[i], metrics
                )
                for i in range(N_FILTERS)
            ]
            for f in as_completed(futures):
                f.result()

        # Phase 3: Aggregators process (concurrent)
        with ThreadPoolExecutor(max_workers=WORKERS) as executor:
            futures = []
            for i in range(N_AGGREGATORS):
                if aggregator_is_audit[i]:
                    futures.append(
                        executor.submit(
                            audit_aggregator_tick,
                            aggregators[i],
                            space,
                            AGGREGATE_BATCH_SIZE,
                            metrics,
                        )
                    )
                else:
                    futures.append(
                        executor.submit(
                            aggregator_tick,
                            aggregators[i],
                            space,
                            AGGREGATE_BATCH_SIZE,
                            metrics,
                        )
                    )
            for f in as_completed(futures):
                f.result()

        # Phase 4: Alerters process (concurrent)
        with ThreadPoolExecutor(max_workers=WORKERS) as executor:
            futures = [
                executor.submit(
                    alerter_tick, alerters[i], space, alerter_thresholds[i], metrics
                )
                for i in range(N_ALERTERS)
            ]
            for f in as_completed(futures):
                f.result()

        # Phase 5: Actors process (concurrent)
        with ThreadPoolExecutor(max_workers=WORKERS) as executor:
            futures = [
                executor.submit(actor_tick, a, space, guard, metrics, tick)
                for a in actors
            ]
            for f in as_completed(futures):
                f.result()

    wall_clock = time.monotonic() - t0

    # 7. Validate results
    print("--- Pipeline Flow ---")
    # Type-narrow reads: space.read returns AnyMark but we know the types from mark_type filter
    all_sensor_obs: list[Observation] = [
        m
        for m in space.read(
            scope="sensors", mark_type=MarkType.OBSERVATION, min_strength=0.0
        )
        if isinstance(m, Observation)
    ]
    all_filtered: list[Observation] = [
        m
        for m in space.read(
            scope="filtered", mark_type=MarkType.OBSERVATION, min_strength=0.0
        )
        if isinstance(m, Observation)
    ]
    all_aggregated: list[Observation] = [
        m
        for m in space.read(
            scope="aggregated", mark_type=MarkType.OBSERVATION, min_strength=0.0
        )
        if isinstance(m, Observation)
    ]
    all_alerts: list[Warning] = [
        m
        for m in space.read(
            scope="alerts", mark_type=MarkType.WARNING, min_strength=0.0
        )
        if isinstance(m, Warning)
    ]
    all_responses: list[Action] = [
        m
        for m in space.read(
            scope="responses", mark_type=MarkType.ACTION, min_strength=0.0
        )
        if isinstance(m, Action)
    ]

    print(f"  Sensor observations:   {len(all_sensor_obs)}")
    print(f"  Filtered observations: {len(all_filtered)}")
    print(f"  Aggregated summaries:  {len(all_aggregated)}")
    print(f"  Alerts raised:         {len(all_alerts)}")
    print(f"  Responses executed:    {len(all_responses)}")
    print(f"  Total marks written:   {metrics.marks_written}")
    print(f"  Wall clock:            {wall_clock:.2f}s")
    print()

    # Read audit marks
    all_audit: list[Observation] = [
        m
        for m in space.read(
            scope="aggregated-audit", mark_type=MarkType.OBSERVATION, min_strength=0.0
        )
        if isinstance(m, Observation)
    ]

    print(f"  Audit observations:    {len(all_audit)}")
    print(f"  Total marks written:   {metrics.marks_written}")
    print(f"  Wall clock:            {wall_clock:.2f}s")
    print()

    # Validation assertions
    errors: list[str] = []

    # 1. Pipeline completeness: sensors must have produced exactly N_SENSORS * N_TICKS
    expected_sensor_obs = N_SENSORS * N_TICKS
    if len(all_sensor_obs) != expected_sensor_obs:
        errors.append(
            f"Expected {expected_sensor_obs} sensor observations, got {len(all_sensor_obs)}"
        )

    # 2. Filter correctness: all filtered marks must exceed the threshold
    #    that was active when they were filtered.
    for fm in all_filtered:
        threshold_used = fm.content.get("threshold", FILTER_THRESHOLD)
        if fm.content.get("value", 0) <= threshold_used:
            errors.append(
                f"Filtered mark with value {fm.content['value']} "
                f"<= threshold {threshold_used}"
            )

    # 3. Pipeline flow: filtered > 0, aggregated > 0 (with random data, ~50% pass filter)
    if len(all_filtered) == 0:
        errors.append("No marks passed through filter stage")
    if len(all_aggregated) == 0:
        errors.append("No marks passed through aggregator stage")

    # 4. Filter-0 swap (tick 10): marks before and after, post-swap includes threshold=30
    pre_swap_f0 = [
        m for m in all_filtered if m.content.get("tick", 0) < SWAP_SIMULTANEOUS_TICK
    ]
    post_swap_f0 = [
        m for m in all_filtered if m.content.get("tick", 0) >= SWAP_SIMULTANEOUS_TICK
    ]
    if len(pre_swap_f0) == 0:
        errors.append("No filtered marks before filter-0 swap (tick 10)")
    if len(post_swap_f0) == 0:
        errors.append("No filtered marks after filter-0 swap (tick 10)")
    post_swap_thresholds = {m.content.get("threshold") for m in post_swap_f0}
    if 30.0 not in post_swap_thresholds:
        errors.append("Swapped filter-0 (threshold=30.0) did not produce any marks")

    # 5. Filter-1 mid-processing swap (tick 7): post-swap marks should have threshold=70
    post_swap_f1 = [
        m
        for m in all_filtered
        if m.content.get("tick", 0) >= SWAP_FILTER1_TICK
        and m.content.get("filtered_by", "").startswith("filter-1")
    ]
    if not post_swap_f1:
        # filter-1 with threshold=70 may produce fewer marks (stricter), but across
        # 13 ticks with 5 sensor readings each, at least some should pass 70
        errors.append("Swapped filter-1 produced no marks after tick 7")
    else:
        for fm in post_swap_f1:
            if fm.content.get("threshold") != 70.0:
                errors.append(
                    f"Filter-1 mark after swap has threshold={fm.content.get('threshold')}, expected 70.0"
                )
                break

    # 6. Filter-1 prospective (P49): no tick-7 sensor marks processed by new filter-1
    #    The new filter-1 was subscribed AFTER sensors wrote at tick 7, so it should
    #    not have received those marks. Any filter-1 marks from tick 7 should have
    #    threshold=50 (old) not 70 (new).
    tick7_f1_marks = [
        m
        for m in all_filtered
        if m.content.get("tick") == SWAP_FILTER1_TICK
        and m.content.get("filtered_by", "").startswith("filter-1")
    ]
    for fm in tick7_f1_marks:
        if fm.content.get("threshold") == 70.0:
            errors.append(
                "New filter-1 (threshold=70) processed tick-7 marks that were "
                "queued before swap - P49 (prospective subscription) violated"
            )
            break

    # 7. Alerter-0 swap (tick 10): post-swap alerts should reflect threshold=60
    post_swap_alerts = [m for m in all_alerts if "60" in (m.reason or "")]
    # With threshold=60, more aggregated values should trigger alerts
    if len(post_swap_alerts) == 0:
        errors.append("Swapped alerter-0 (threshold=60) did not produce any alerts")

    # 8. Aggregator-0 audit swap (tick 14): audit scope should have marks
    if len(all_audit) == 0:
        errors.append("Audit aggregator produced no marks in aggregated-audit scope")
    else:
        # All audit marks should come from the audit aggregator
        for am in all_audit:
            if "audit" not in am.content.get("aggregator", ""):
                errors.append(
                    f"Audit mark from non-audit aggregator: {am.content.get('aggregator')}"
                )
                break

    # 9. Unique mark IDs (concurrent safety)
    all_mark_ids = [
        m.id for m in all_sensor_obs + all_filtered + all_aggregated + all_audit
    ]
    if len(all_mark_ids) != len(set(all_mark_ids)):
        errors.append("Duplicate mark IDs detected")

    # Report
    print("--- Validation ---")
    if errors:
        for e in errors:
            print(f"  FAIL: {e}")
        raise AssertionError(f"{len(errors)} validation error(s)")
    else:
        print("  All composition stress test assertions passed.")

    print()
    print("--- Swap Results ---")
    # Filter-1 swap (tick 7, mid-processing)
    f1_pre = len(
        [
            m
            for m in all_filtered
            if m.content.get("tick", 0) < SWAP_FILTER1_TICK
            and m.content.get("filtered_by", "").startswith("filter-1")
        ]
    )
    f1_post = len(post_swap_f1)
    print(
        f"  filter-1 (tick {SWAP_FILTER1_TICK}, threshold 50->70): {f1_pre} pre, {f1_post} post"
    )

    # Filter-0 swap (tick 10, simultaneous)
    f0_pre = len(
        [
            m
            for m in all_filtered
            if m.content.get("tick", 0) < SWAP_SIMULTANEOUS_TICK
            and m.content.get("filtered_by", "").startswith("filter-0")
        ]
    )
    f0_post = len(
        [
            m
            for m in all_filtered
            if m.content.get("tick", 0) >= SWAP_SIMULTANEOUS_TICK
            and m.content.get("filtered_by", "").startswith("filter-0")
        ]
    )
    print(
        f"  filter-0 (tick {SWAP_SIMULTANEOUS_TICK}, threshold 50->30): {f0_pre} pre, {f0_post} post"
    )

    # Alerter-0 swap (tick 10, simultaneous)
    print(
        f"  alerter-0 (tick {SWAP_SIMULTANEOUS_TICK}, threshold 75->60): {len(post_swap_alerts)} post-swap alerts with threshold=60"
    )

    # Aggregator-0 audit swap (tick 14, permission change)
    print(
        f"  aggregator-0 (tick {SWAP_AGGREGATOR_TICK}, +audit scope): {len(all_audit)} audit marks"
    )

    print()
    print("--- Summary ---")
    summary = {
        "seed": seed,
        "n_ticks": N_TICKS,
        "n_agents": len(all_agents),
        "n_swaps": 4,
        "sensor_observations": len(all_sensor_obs),
        "filtered_observations": len(all_filtered),
        "aggregated_summaries": len(all_aggregated),
        "audit_observations": len(all_audit),
        "alerts_raised": len(all_alerts),
        "responses_executed": len(all_responses),
        "total_marks_written": metrics.marks_written,
        "swap_filter1_pre": f1_pre,
        "swap_filter1_post": f1_post,
        "swap_filter0_pre": f0_pre,
        "swap_filter0_post": f0_post,
        "swap_alerter0_post_alerts": len(post_swap_alerts),
        "swap_aggregator0_audit_marks": len(all_audit),
        "unique_mark_ids": len(set(all_mark_ids)),
        "wall_clock_seconds": round(wall_clock, 3),
        "validation_errors": 0,
    }
    for k, v in summary.items():
        print(f"  {k}: {v}")

    return summary


if __name__ == "__main__":
    run_stress_test()
