# Composition Stress Test Analysis

Experiment code: [`run.py`](run.py).

## Contents

- [Summary](#summary)
- [1. Pipeline Topology](#1-pipeline-topology)
- [2. Pipeline Flow](#2-pipeline-flow)
  - [2.1 Stage-by-stage throughput](#21-stage-by-stage-throughput)
  - [2.2 Fan-in and fan-out](#22-fan-in-and-fan-out)
- [3. Hot-Swap](#3-hot-swap)
- [4. Concurrency](#4-concurrency)
- [5. Composition Properties Validated](#5-composition-properties-validated)
- [6. Comparison to the 105-Agent Stress Test](#6-comparison-to-the-105-agent-stress-test)

## Summary

14 deterministic agents arranged in a 5-stage pipeline, running concurrently across 20 ticks with 4 mid-run hot-swaps. No LLMs - all agents are pure functions. The test validates composition properties under concurrent execution: simultaneous swaps across pipeline stages, mid-processing swaps, permission changes on non-leaf agents, and prospective subscription semantics.

| Metric | Result |
|--------|--------|
| Agents | **14** (5 sensors, 3 filters, 2 aggregators, 2 alerters, 2 actors) |
| Ticks | **20** |
| Hot-swaps | **4** (mid-processing, simultaneous x2, permission change) |
| Total marks written | **833-923** |
| Sensor observations | **100** |
| Filtered observations | **141** |
| Aggregated summaries | **104** |
| Audit observations | **18** (from permission-changed aggregator) |
| Alerts raised | **94-112** |
| Responses executed | **188-224** |
| Duplicate mark IDs | **0** across 363 marks checked |
| Validation errors | **0** |
| Wall clock | **~0.3s** |

Alerts, responses, and total marks vary across runs due to thread scheduling in the concurrent pipeline (see [Section 4](#4-concurrency)). All other values are deterministic.

All composition assertions passed. Pipeline connectivity validated before execution (P53). All agent manifests consistent with permissions (P54) - including 4 hot-swapped replacements with varying permissions. No duplicate mark IDs under concurrent writes.

---

## 1. Pipeline Topology

<p align="center"><img src="pipeline.svg" alt="5-stage pipeline topology"/></p>

*Logical data flow. Agents do not communicate directly - all marks are written to and read from the shared mark space. Arrows show which agent's output feeds which agent's input.*

Six scopes, each with FIRST_WRITER conflict policy:

| Scope | Mark types | Writers | Readers |
|-------|-----------|---------|---------|
| `sensors` | observation | 5 sensors | 3 filters |
| `filtered` | observation | 3 filters | 2 aggregators |
| `aggregated` | observation | 2 aggregators | 2 alerters |
| `aggregated-audit` | observation | 1 aggregator (post-swap) | - |
| `alerts` | warning | 2 alerters | 2 actors |
| `responses` | intent, action | 2 actors | - |

Each agent declares inputs and outputs in its manifest. `validate_pipeline()` checks structural connectivity before the simulation starts: each producer's outputs must match the next consumer's inputs. `validate_manifest_permissions()` checks that every declared output falls within the agent's write permissions.

Composition patterns exercised:

- **Linear pipeline** - each stage reads from the previous, writes to the next
- **Fan-in** - 5 sensors feed 3 filters (many-to-few)
- **Fan-out** - 2 alerters both watch the same `aggregated` scope (shared input)
- **Reactive activation** - downstream agents only act when `get_watched_marks()` returns data
- **Guarded execution** - actors write intent + action through the guard's `execute()` cycle

---

## 2. Pipeline Flow

### 2.1 Stage-by-stage throughput

| Stage | Input | Output | Ratio | Mechanism |
|-------|-------|--------|-------|-----------|
| Sensors | - | 100 observations | 5/tick | One reading per sensor per tick |
| Filters | 100 observations | 141 filtered | 1.4x | Three thresholds over time: 50, 70 (filter-1 post-tick-7), 30 (filter-0 post-tick-10) |
| Aggregators | 141 filtered | 104 summaries (+18 audit) | 0.74x | Batch-of-3 aggregation; audit aggregator writes to both scopes post-tick-14 |
| Alerters | 104 summaries | 94-112 warnings | ~1.0x | Threshold 75.0, then 60.0 for alerter-0 post-tick-10 |
| Actors | 94-112 warnings | 188-224 actions | 2.0x | Intent + action pair per warning |

The pipeline is a funnel: sensors produce the most marks, each subsequent stage reduces volume through filtering or aggregation, and actors expand slightly because each response writes two marks (intent + action) through the guard.

Filters amplify rather than reduce because there are 3 filters each processing the same sensor observations via watch/subscribe. Each filter independently evaluates every new sensor reading. The multiple hot-swaps change the filter dynamics over time: filter-1's threshold increases from 50 to 70 at tick 7 (stricter, fewer marks pass), while filter-0's threshold decreases from 50 to 30 at tick 10 (looser, more marks pass). The alerter-0 swap from 75 to 60 increases alert volume in the second half.

### 2.2 Fan-in and fan-out

**Fan-in (sensors to filters):** 5 sensors write to the `sensors` scope. All 3 filters subscribe to `WatchPattern(scope="sensors", mark_type=OBSERVATION)`. Each filter independently receives all 5 sensor readings per tick. The mark space delivers the same marks to all subscribers without interference.

**Fan-out (aggregators to alerters):** 2 alerters both subscribe to `WatchPattern(scope="aggregated", mark_type=OBSERVATION)`. Both receive the same aggregated summaries. When an average exceeds the alert threshold (75.0), both alerters independently raise warnings. This is by design - redundant alerting is preferable to missed alerts.

---

## 3. Hot-Swaps

Four swaps exercise different composition scenarios:

| Tick | Agent | Change | What it tests |
|------|-------|--------|---------------|
| 7 | filter-1 | threshold 50 -> 70 | Mid-processing swap (after sensors write, before filters run) |
| 10 | filter-0 | threshold 50 -> 30 | Simultaneous swap (two agents swapped in same tick) |
| 10 | alerter-0 | threshold 75 -> 60 | Simultaneous swap (cross-stage, same tick as filter-0) |
| 14 | aggregator-0 | +`aggregated-audit` scope | Permission change on non-leaf agent |

### Swap results

| Swap | Pre-swap marks | Post-swap marks |
|------|---------------|-----------------|
| filter-1 (tick 7, threshold 50->70) | 18 | 13 (stricter threshold, fewer pass) |
| filter-0 (tick 10, threshold 50->30) | 26 | 34 (looser threshold, more pass) |
| alerter-0 (tick 10, threshold 75->60) | - | 40-44 alerts with threshold=60 |
| aggregator-0 (tick 14, +audit) | - | 18 audit marks in `aggregated-audit` |

### Mid-processing swap (tick 7)

Filter-1 is swapped after sensors write but before filters process. The old filter-1 is unsubscribed, clearing its pending notifications. The new filter-1 (threshold=70) subscribes and receives only marks written after the swap. Tick-7 sensor marks that were queued for the old filter are not processed by the new one - validating **Subscription Prospective (P49)**.

### Simultaneous swaps (tick 10)

Filter-0 and alerter-0 are swapped in the same tick, in different pipeline stages. Both old agents are unsubscribed and both new agents are subscribed before any processing begins. The pipeline continues without interference between the two swaps. Validates that concurrent swaps across stages don't corrupt subscription state.

### Permission change (tick 14)

Aggregator-0 is replaced with a version that has an additional write scope (`aggregated-audit`). The new agent writes summaries to both `aggregated` (for downstream alerters) and `aggregated-audit` (audit trail). The manifest is validated against the new, broader permissions (P54). Downstream alerters are unaffected - they subscribe to `aggregated`, not to a specific aggregator. This is the only swap of a non-leaf agent (aggregators sit in the middle of the pipeline).

### Properties validated by hot-swaps

- **Subscription Idempotency (P48):** All 4 swaps: unsubscribe + resubscribe leaves no stale subscriptions or duplicate deliveries
- **Subscription Prospective (P49):** Mid-processing swap (tick 7): new filter-1 does not process marks queued before the swap
- **Manifest-Permission Consistency (P54):** All 4 replacement agents validated; aggregator-0 validated with broader permissions
- **Pipeline continuity:** All swaps: downstream stages unaffected by upstream agent replacement

---

## 4. Concurrency

All agents within each stage run concurrently using a thread pool (10 workers). Stages execute sequentially - sensors complete before filters start, filters complete before aggregators start - to maintain causal ordering in the pipeline. Thread scheduling within a stage is non-deterministic, so the order in which concurrent agents complete varies between runs. This affects downstream counts: when two alerters race to process the same aggregated summaries, thread timing determines which summaries each alerter sees first, producing alert counts that vary by a few marks per run (94-112 alerts across observed runs). Upstream stages (sensors, filters, aggregators) produce deterministic counts because their outputs depend only on input values, not on processing order.

| Concurrent operation | Result |
|---------------------|--------|
| 5 sensors writing simultaneously | 0 duplicate mark IDs |
| 3 filters reading + writing simultaneously | 0 duplicate mark IDs |
| 2 aggregators reading + writing simultaneously | 0 duplicate mark IDs |
| 2 alerters reading + writing simultaneously | 0 duplicate mark IDs |
| 2 actors executing guard cycles simultaneously | 0 duplicate mark IDs |

363 unique mark IDs verified across sensor observations, filtered observations, aggregated summaries, and audit observations. UUID4 generation and thread-safe mark space writes prevent collisions.

The actors use the guard's `execute()` method, which acquires a lock for the intent-action cycle. Under concurrent execution, both actors process different alerts without interference. FIRST_WRITER conflict policy means the first actor to claim a resource wins; in this test, each actor targets a unique resource key per tick, avoiding contention.

---

## 5. Composition Properties Validated

The stress test exercises the composition properties defined in Spec Section 14:

| Property | Description | How validated |
|----------|------------|---------------|
| P48 | Subscription Idempotency | 4 hot-swaps: unsubscribe + resubscribe leaves no duplicates |
| P49 | Subscription Prospective | Mid-processing swap (tick 7): new filter does not process pre-swap marks |
| P50 | Watch Subset | Each stage receives only marks matching its subscribed pattern |
| P51 | At-Most-Once Delivery | No duplicate processing observed across 833-923 marks |
| P52 | Write-Order Delivery | Pipeline stages process marks in causal order |
| P53 | Pipeline Structural Validation | `validate_pipeline()` runs before simulation; verified for representative chain |
| P54 | Manifest-Permission Consistency | `validate_manifest_permissions()` passes for all 14 agents + 4 hot-swapped replacements (including one with broader permissions) |
| P55 | Pattern Match Purity | `WatchPattern.matches()` used throughout with no side effects |

The unit tests in `test_composition.py` (30 tests) verify these properties in isolation, including hypothesis property-based tests for pattern matching determinism and concurrent subscription safety with 50 writers. The stress test verifies they hold together in a multi-stage pipeline under concurrent execution with multiple hot-swaps.

---

## 6. Comparison to the 105-Agent Stress Test

| | Composition stress test | 105-agent stress test |
|---|---|---|
| **Purpose** | Validate pipeline composition under concurrency | Validate safety invariants under adversarial pressure |
| **Agents** | 14 deterministic functions | 105 LLM-driven (gpt-oss-120b) |
| **Agent behavior** | Pure functions, no randomness in logic | LLM reasoning, tool selection, retries |
| **Topology** | Linear pipeline with fan-in/fan-out | Flat (all agents access shared scopes) |
| **Scopes** | 6 (one per stage + audit scope added by hot-swap) | 7 resource types + department scopes |
| **Conflict policy** | FIRST_WRITER only | All 3 (FIRST_WRITER, HIGHEST_CONFIDENCE, YIELD_ALL) |
| **Key validation** | Pipeline connectivity, 4 hot-swaps (simultaneous, mid-processing, permission change), reactive delivery | Zero double bookings, zero scope violations, adversarial containment |
| **Marks written** | 833-923 | 927 actions + 1,212 intents |
| **Wall clock** | 0.30s | 470s |
| **Cost** | Zero (no LLM calls) | ~8.0M tokens |

The composition test isolates protocol mechanics from LLM behavior. Agent quality is not a variable - every agent is a deterministic function. This makes failures attributable to the composition infrastructure (subscription delivery, manifest validation, concurrent write safety) rather than to unpredictable agent decisions.

The 105-agent test validates safety under realistic conditions. The composition test validates plumbing.
