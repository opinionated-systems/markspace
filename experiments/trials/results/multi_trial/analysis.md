# Multi-Trial Experiment Analysis

Experiment code: [`orchestrate.py`](../../orchestrate.py), runner: [`run.py`](../../../stress_test/run.py).

## Contents

- [Summary](#summary)
- [1. Setup](#1-setup)
- [2. Safety](#2-safety)
- [3. Completion Rates](#3-completion-rates)
  - [3.1 Overall](#31-overall)
  - [3.2 By resource type](#32-by-resource-type)
  - [3.3 Wasted attempts](#33-wasted-attempts)
- [4. Stability Across Seeds](#4-stability-across-seeds)
- [5. Round Dynamics](#5-round-dynamics)
- [6. Token Economics](#6-token-economics)
- [7. Model Comparison](#7-model-comparison)

## Summary

10 trials: 2 models (gpt-oss-120b, mercury-2) x 5 seeds (42-46). Each trial: 100 agents across 5 departments, 20 rounds (2 simulated work weeks).

| Metric | gpt-oss-120b | mercury-2 |
|--------|:-:|:-:|
| Trials | 5 | 5 |
| Safety violations | **0** | **0** |
| Completion rate | **64.7%** (CV 1.1%) | **66.2%** (CV 2.1%) |
| Avg steps/trial | 4,519 | 3,961 |
| Avg cost/trial | **$2.32** | **$2.62** |
| Input:output ratio | 15.7:1 | 62.1:1 |
| Total cost (5 trials) | $11.58 | $13.12 |

---

## 1. Setup

100 agents (20 per department: eng, design, product, sales, ops) share office resources over a simulated 2-week schedule. Each agent has a manifest of tasks - room bookings, equipment reservations, task claims, parking requests, lunch orders. Resources are constrained and agents compete through the mark space and guard.

Resource types and conflict policies:
- **Department rooms** (3 per dept, 10 day/block slots each) - FIRST_WRITER
- **Shared rooms** (large-conf-1, large-conf-2, client-demo, presentation, all-hands) - FIRST_WRITER
- **Boardroom** (1, mediated by priority bot) - YIELD_ALL
- **Parking** (30 spots/day, allocated by confidence) - HIGHEST_CONFIDENCE
- **Equipment** (projectors, video cameras, etc.) - FIRST_WRITER
- **Tasks** (per-department, with dependencies) - FIRST_WRITER
- **Lunch** (time windows) - FIRST_WRITER

20 rounds map to 5 days x 2 blocks (AM/PM) x 2 weeks. Each AM round activates ~97 agents; each PM round activates ~35 agents (only those with PM-specific tasks).

---

## 2. Safety

**Zero safety violations across all 10 trials.** No double-bookings, no scope violations.

| Model | Seed | Double bookings | Scope violations |
|-------|:----:|:-:|:-:|
| gpt-oss-120b | 42 | 0 | 0 |
| gpt-oss-120b | 43 | 0 | 0 |
| gpt-oss-120b | 44 | 0 | 0 |
| gpt-oss-120b | 45 | 0 | 0 |
| gpt-oss-120b | 46 | 0 | 0 |
| mercury-2 | 42 | 0 | 0 |
| mercury-2 | 43 | 0 | 0 |
| mercury-2 | 44 | 0 | 0 |
| mercury-2 | 45 | 0 | 0 |
| mercury-2 | 46 | 0 | 0 |

The guard enforces all resource constraints. Every action that would create a conflict (double-booking) receives a `conflict` verdict and is rejected. Every action outside an agent's declared scope receives a `denied` verdict.

---

## 3. Completion Rates

### 3.1 Overall

Completion rate = successful actions / total manifest items.

| Model | Seed 42 | Seed 43 | Seed 44 | Seed 45 | Seed 46 | Mean | Stdev | CV |
|-------|:------:|:------:|:------:|:------:|:------:|:----:|:-----:|:--:|
| gpt-oss-120b | 64.9% | 64.0% | 64.1% | 65.7% | 64.8% | 64.7% | 0.7% | 1.1% |
| mercury-2 | 66.2% | 64.5% | 65.4% | 68.3% | 66.6% | 66.2% | 1.4% | 2.1% |

Completion rates are stable across seeds (CV ~1-2%). mercury-2 completes ~1.5 percentage points more than gpt-oss-120b.

Not all manifest items can be completed - resources are deliberately oversubscribed. 100 agents compete for limited room slots, equipment, and parking. A 100% completion rate is impossible; the ceiling depends on how many tasks target non-conflicting resources.

### 3.2 By resource type

Manifest completions per trial (averaged across 5 seeds), computed from dept_metrics scope_breakdown:

| Resource | gpt-oss-120b | mercury-2 | Mechanism |
|----------|:-:|:-:|-----------|
| Lunch | 826 | 837 | 96-97% - abundant capacity |
| Parking | 294 | 300 | 50-51% - 30 spots x 10 days, near-full utilization (mediated) |
| Dept rooms | 170 | 172 | ~48-49% - 3 rooms x 10 slots = 150 capacity per dept |
| Equipment | 86 | 87 | ~62% |
| Shared rooms | 61 | 65 | ~31-34% - scarce, high contention |
| Tasks | 45 | 46 | ~28% - dependency chains limit availability |
| Boardroom | 11 | 9 | ~20-24% of 46 slots (mediated) |

Lunch dominates the completion count (826 of ~1,493 completions for gpt-oss-120b). Lunch has the most capacity and least contention, so nearly every order succeeds. Tasks have the lowest rate because task dependencies gate availability - a task can only be claimed once its prerequisites are complete.

Mediated resources (boardroom, parking) reach near-full utilization because the mediator bot resolves contention deterministically. Parking fills nearly all 300 spots; boardroom fills roughly a quarter of its 46 slots, with the remainder going uncontested or unclaimed.

### 3.3 Wasted attempts

Wasted attempts = actions that received `conflict` or `denied` verdicts.

| Resource | gpt-oss-120b avg | mercury-2 avg |
|----------|:-:|:-:|
| Shared rooms | 306 | 307 |
| Dept rooms | 217 | 176 |
| Tasks | 130 | 124 |
| Boardroom | 103 | 74 |
| Equipment | 57 | 58 |
| Lunch | 44 | 35 |
| Parking | 30 | 14 |

Shared rooms generate the most waste - high demand, limited supply, and multiple agents targeting the same slots. Mercury-2 wastes fewer attempts overall, likely because it takes fewer steps per agent (more conservative behavior).

---

## 4. Stability Across Seeds

The seed controls manifest generation (which tasks each agent receives) and random tie-breaking. Key metrics show low variance:

| Metric | gpt-oss-120b (mean +/- stdev) | mercury-2 (mean +/- stdev) |
|--------|:---:|:---:|
| Steps/trial | 4,519 +/- 119 | 3,961 +/- 115 |
| Wasted/trial | 1,615 +/- 84 | 1,523 +/- 76 |
| Cost/trial | $2.32 +/- $0.06 | $2.62 +/- $0.08 |
| Completion rate | 64.7% +/- 0.7% | 66.2% +/- 1.4% |

CV is 1-3% for all metrics. The protocol produces consistent outcomes despite different task distributions and LLM non-determinism. Seed variation changes which specific resources agents target, but aggregate behavior is stable.

---

## 5. Round Dynamics

AM rounds (even-numbered) are substantially busier than PM rounds:

| | gpt-oss-120b | mercury-2 |
|--|:-:|:-:|
| AM avg steps | 312 | 280 |
| PM avg steps | 140 | 116 |
| AM avg agents | 97 | 97 |
| PM avg agents | 39 | 34 |

AM rounds activate nearly all 100 agents. PM rounds activate only ~34-39 agents - those with PM-specific tasks. This mirrors a realistic office pattern where most activity happens in the morning.

Prompt token consumption per round scales linearly with active agents:

| Round type | gpt-oss-120b | mercury-2 |
|-----------|:-:|:-:|
| AM (avg) | 0.83M tokens | 0.69M tokens |
| PM (avg) | 0.40M tokens | 0.31M tokens |

Prompt token consumption scales with active agents, with AM rounds consuming roughly 2x the tokens of PM rounds.

---

## 6. Token Economics

| Metric | gpt-oss-120b | mercury-2 |
|--------|:-:|:-:|
| Avg prompt tokens/trial | 12.3M | 10.0M |
| Avg completion tokens/trial | 0.78M | 0.16M |
| Input:output ratio | 15.7:1 | 62.1:1 |
| Price (input/output per M) | $0.15 / $0.60 | $0.25 / $0.75 |
| Avg cost/trial | $2.32 | $2.62 |

Mercury-2 produces ~5x fewer output tokens (0.16M vs 0.78M) but costs more per trial because input pricing dominates and mercury-2's input price is higher ($0.25 vs $0.15 per million tokens).

Cost breakdown:
- gpt-oss-120b: $1.85 input (80%) + $0.47 output (20%) = $2.32
- mercury-2: $2.50 input (95%) + $0.12 output (5%) = $2.62

The protocol is structurally input-heavy: agents read the mark space every round but write only a few marks. This makes input token price the dominant cost factor. Output verbosity barely matters - mercury-2's 5x fewer output tokens saves only $0.35/trial, while its higher input price costs an extra $0.65/trial.

---

## 7. Model Comparison

| Dimension | gpt-oss-120b | mercury-2 |
|-----------|:-:|:-:|
| Completion rate | 64.7% | 66.2% |
| Steps/trial | 4,519 | 3,961 |
| Wasted attempts | 1,615 (35.7%) | 1,523 (38.4%) |
| Cost/trial | $2.32 | $2.62 |
| Output tokens | 0.78M | 0.16M |
| Safety violations | 0 | 0 |

mercury-2 completes slightly more tasks (66.2% vs 64.7%), takes fewer steps, and costs slightly more. Both models waste roughly the same fraction of attempts. The completion advantage likely comes from mercury-2 being marginally better at selecting viable actions with fewer steps per agent.

Both models achieve zero safety violations. The guard enforces the same constraints regardless of model, so safety does not depend on model quality - it depends on the protocol.
