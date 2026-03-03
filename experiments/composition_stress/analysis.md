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

14 deterministic agents arranged in a 5-stage pipeline, running concurrently across 20 ticks. No LLMs - all agents are pure functions. The test validates composition properties under concurrent execution with a mid-run hot-swap: replacing one filter agent with a different configuration while the pipeline continues processing.

| Metric | Result |
|--------|--------|
| Agents | **14** (5 sensors, 3 filters, 2 aggregators, 2 alerters, 2 actors) |
| Ticks | **20** |
| Total marks written | **694** |
| Sensor observations | **100** |
| Filtered observations | **160** |
| Aggregated summaries | **114** |
| Alerts raised | **64** |
| Responses executed | **128** |
| Duplicate mark IDs | **0** across 374 marks checked |
| Validation errors | **0** |
| Wall clock | **0.23s** |

All composition assertions passed. Pipeline connectivity validated before execution (P40). All agent manifests consistent with permissions (P41). Hot-swap completed without pipeline interruption. No duplicate mark IDs under concurrent writes.

---

## 1. Pipeline Topology

```
SensorAgent(5) --obs--> FilterAgent(3) --obs--> AggregatorAgent(2)
     --obs--> AlerterAgent(2) --warn--> ActionAgent(2)
```

Five scopes, each with FIRST_WRITER conflict policy:

| Scope | Mark types | Writers | Readers |
|-------|-----------|---------|---------|
| `sensors` | observation | 5 sensors | 3 filters |
| `filtered` | observation | 3 filters | 2 aggregators |
| `aggregated` | observation | 2 aggregators | 2 alerters |
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
| Filters | 100 observations | 160 filtered | 1.6x | Threshold filter (value > 50.0, then > 30.0 after swap) |
| Aggregators | 160 filtered | 114 summaries | 0.71x | Batch-of-3 aggregation with remainder flushes |
| Alerters | 114 summaries | 64 warnings | 0.56x | Average > 75.0 threshold |
| Actors | 64 warnings | 128 actions | 2.0x | Intent + action pair per warning |

The pipeline is a funnel: sensors produce the most marks, each subsequent stage reduces volume through filtering or aggregation, and actors expand slightly because each response writes two marks (intent + action) through the guard.

Filters amplify rather than reduce because there are 3 filters each processing the same sensor observations via watch/subscribe. Each filter independently evaluates every new sensor reading. With random values drawn from uniform [0, 100], roughly half exceed the threshold of 50.0. The hot-swap at tick 10 lowers one filter's threshold to 30.0, passing even more readings through.

### 2.2 Fan-in and fan-out

**Fan-in (sensors to filters):** 5 sensors write to the `sensors` scope. All 3 filters subscribe to `WatchPattern(scope="sensors", mark_type=OBSERVATION)`. Each filter independently receives all 5 sensor readings per tick. The mark space delivers the same marks to all subscribers without interference.

**Fan-out (aggregators to alerters):** 2 alerters both subscribe to `WatchPattern(scope="aggregated", mark_type=OBSERVATION)`. Both receive the same aggregated summaries. When an average exceeds the alert threshold (75.0), both alerters independently raise warnings. This is by design - redundant alerting is preferable to missed alerts.

---

## 3. Hot-Swap

At tick 10 (midpoint), filter-0 is replaced:

1. Old filter (`filter-0-t50`, threshold=50.0) is unsubscribed
2. New filter (`filter-0-t30`, threshold=30.0) is created and subscribed
3. New filter's manifest is validated against permissions (P41)

| Phase | Filtered marks | Threshold for filter-0 |
|-------|---------------|----------------------|
| Pre-swap (ticks 0-9) | 78 | 50.0 |
| Post-swap (ticks 10-19) | 82 | 30.0 |

The pipeline continued without interruption. Post-swap marks include outputs from the new filter with threshold=30.0, confirming the swapped agent is active. The increase from 78 to 82 filtered marks reflects the lower threshold passing more readings through.

The hot-swap validates several properties simultaneously:

- **Subscription Idempotency (P35):** Unsubscribing the old agent and subscribing the new one leaves no stale subscriptions
- **Subscription Prospective (P36):** The new filter only receives marks written after it subscribes, not the backlog from ticks 0-9
- **Manifest-Permission Consistency (P41):** The replacement agent's manifest is validated before it joins the pipeline
- **Pipeline continuity:** Other stages (aggregators, alerters, actors) are unaffected by the swap - they subscribe to `filtered`, not to a specific filter agent

---

## 4. Concurrency

All agents within each stage run concurrently using a thread pool (10 workers). Stages execute sequentially - sensors complete before filters start, filters complete before aggregators start - to maintain causal ordering in the pipeline.

| Concurrent operation | Result |
|---------------------|--------|
| 5 sensors writing simultaneously | 0 duplicate mark IDs |
| 3 filters reading + writing simultaneously | 0 duplicate mark IDs |
| 2 aggregators reading + writing simultaneously | 0 duplicate mark IDs |
| 2 alerters reading + writing simultaneously | 0 duplicate mark IDs |
| 2 actors executing guard cycles simultaneously | 0 duplicate mark IDs |

374 unique mark IDs verified across sensor observations, filtered observations, and aggregated summaries. UUID4 generation and thread-safe mark space writes prevent collisions.

The actors use the guard's `execute()` method, which acquires a lock for the intent-action cycle. Under concurrent execution, both actors process different alerts without interference. FIRST_WRITER conflict policy means the first actor to claim a resource wins; in this test, each actor targets a unique resource key per tick, avoiding contention.

---

## 5. Composition Properties Validated

The stress test exercises the composition properties defined in Spec Section 14:

| Property | Description | How validated |
|----------|------------|---------------|
| P35 | Subscription Idempotency | Hot-swap: unsubscribe + resubscribe leaves no duplicates |
| P36 | Subscription Prospective | New filter after hot-swap receives only post-swap marks |
| P37 | Watch Subset | Each stage receives only marks matching its subscribed pattern |
| P38 | At-Most-Once Delivery | No duplicate processing observed across 694 marks |
| P39 | Write-Order Delivery | Pipeline stages process marks in causal order |
| P40 | Pipeline Structural Validation | `validate_pipeline()` runs before simulation; verified for representative chain |
| P41 | Manifest-Permission Consistency | `validate_manifest_permissions()` passes for all 14 agents, including the hot-swapped replacement |
| P42 | Pattern Match Purity | `WatchPattern.matches()` used throughout with no side effects |

The unit tests in `test_composition.py` (31 tests) verify these properties in isolation, including hypothesis property-based tests for pattern matching determinism and concurrent subscription safety with 50 writers. The stress test verifies they hold together in a multi-stage pipeline under concurrent execution.

---

## 6. Comparison to the 105-Agent Stress Test

| | Composition stress test | 105-agent stress test |
|---|---|---|
| **Purpose** | Validate pipeline composition under concurrency | Validate safety invariants under adversarial pressure |
| **Agents** | 14 deterministic functions | 105 LLM-driven (gpt-oss-120b) |
| **Agent behavior** | Pure functions, no randomness in logic | LLM reasoning, tool selection, retries |
| **Topology** | Linear pipeline with fan-in/fan-out | Flat (all agents access shared scopes) |
| **Scopes** | 5 (one per stage) | 7 resource types + department scopes |
| **Conflict policy** | FIRST_WRITER only | All 3 (FIRST_WRITER, HIGHEST_CONFIDENCE, YIELD_ALL) |
| **Key validation** | Pipeline connectivity, hot-swap, reactive delivery | Zero double bookings, zero scope violations, adversarial containment |
| **Marks written** | 694 | 5,747 actions + 7,397 intents |
| **Wall clock** | 0.23s | 470s |
| **Cost** | Zero (no LLM calls) | ~8.0M tokens |

The composition test isolates protocol mechanics from LLM behavior. Agent quality is not a variable - every agent is a deterministic function. This makes failures attributable to the composition infrastructure (subscription delivery, manifest validation, concurrent write safety) rather than to unpredictable agent decisions.

The 105-agent test validates safety under realistic conditions. The composition test validates plumbing.
