# Stress Test Analysis: Office Coordination Week

Scenario design and agent configuration: [`design.md`](design.md).

## Contents

- [Summary](#summary)
- [1. Safety](#1-safety)
- [2. Protocol Coverage](#2-protocol-coverage)
- [3. Resource Allocation Results](#3-resource-allocation-results)
  - [3.1 Overview by resource type](#31-overview-by-resource-type)
  - [3.2 Department completion rates](#32-department-completion-rates)
  - [3.3 Parking: HIGHEST_CONFIDENCE with deferred resolution](#33-parking-highest_confidence-with-deferred-resolution)
  - [3.4 Lunch: preference satisfaction](#34-lunch-preference-satisfaction)
  - [3.5 Shared rooms: the hardest resource](#35-shared-rooms-the-hardest-resource)
  - [3.6 Boardroom: YIELD_ALL in action](#36-boardroom-yield_all-in-action)
  - [3.7 Tasks: dependency chains working](#37-tasks-dependency-chains-working)
- [4. Agent Behavior](#4-agent-behavior)
  - [4.1 Tool usage patterns](#41-tool-usage-patterns)
  - [4.2 Steps per agent](#42-steps-per-agent)
  - [4.3 Most and least active agents](#43-most-and-least-active-agents)
  - [4.4 Waste rate increases over time](#44-waste-rate-increases-over-time)
  - [4.5 Heads vs regular agents](#45-heads-vs-regular-agents)
- [5. Temporal Dynamics](#5-temporal-dynamics)
  - [5.1 Mark accumulation](#51-mark-accumulation)
  - [5.2 Clock and decay](#52-clock-and-decay)
- [6. What We Learned](#6-what-we-learned)
  - [6.1 The protocol works where architecture matches decomposition](#61-the-protocol-works-where-architecture-matches-decomposition)
  - [6.2 Fairness requires explicit design](#62-fairness-requires-explicit-design)
  - [6.3 Lock-based guards break HIGHEST_CONFIDENCE](#63-lock-based-guards-break-highest_confidence)
  - [6.4 YIELD_ALL is the right pattern for scarce shared resources](#64-yield_all-is-the-right-pattern-for-scarce-shared-resources)
  - [6.5 Projected reads enable coordination without full disclosure](#65-projected-reads-enable-coordination-without-full-disclosure)
  - [6.6 Observation decay models information freshness](#66-observation-decay-models-information-freshness)
  - [6.7 Where the protocol is weakest: sequential work](#67-where-the-protocol-is-weakest-sequential-work)
  - [6.8 Token economics](#68-token-economics)
- [7. Adversarial Robustness](#7-adversarial-robustness)
  - [7.1 Adversarial agent activity](#71-adversarial-agent-activity)
  - [7.2 Cross-department booking attempts](#72-cross-department-booking-attempts)
  - [7.3 Warning injection attempts](#73-warning-injection-attempts)
  - [7.4 Key finding](#74-key-finding)
- [8. Strengths and Limitations](#8-strengths-and-limitations)

## Summary

105 AI agents (100 normal + 5 adversarial, one per department) coordinated through a shared mark space over a simulated 5-day work week (10 rounds). No central scheduler. No direct communication between agents. All coordination happened indirectly through stigmergic marks: intents, actions, observations, warnings, and needs. The adversarial agents had normal permissions but adversarial prompts, and were given extra tools (`book_other_dept_room`, `issue_warning`) to attempt cross-department violations.

The protocol enforces invariants regardless of agent quality or intent. The guard rejects unauthorized writes, scope visibility prevents information leakage, trust weights attenuate untrusted marks, and decay ensures stale signals fade. All four properties verified: unauthorized writes rejected, scope visibility enforced, trust weights applied, and adversarial agents contained. 5 adversarial agents attempted 121 cross-department bookings and 37 warning injections. All denied.

Zero double bookings across 927 resource claims. Zero scope violations across 8,197 projected reads. The guard enforces these structurally, under concurrent LLM-driven access at 105-agent scale across 7 resource types, 3 visibility levels, 3 conflict resolution policies, and active adversarial pressure.

**Protocol coverage: 9/9 (100%).** Every feature in the spec produced non-trivial output: all 5 mark types, all 4 verdict types, and projected reads.

**Cost: ~8.0M tokens and 470 seconds of wall clock time.** Each agent consumed an average of 76K tokens across 6.8 active rounds.

All agents ran gpt-oss-120b. No model comparison; the experiment targets protocol behavior.

---

## 1. Safety

The protocol's central promise is that concurrent agents cannot create conflicting state. No violations in this trial.

| Safety metric | Result |
|---|---|
| Double bookings | **0** across 927 action marks |
| Scope violations | **0** across 8,197 projected reads |
| Unauthorized access | **0** (PROTECTED dept rooms invisible to outsiders; CLASSIFIED tasks invisible) |
| Data leakage through projection | **0** (PROTECTED content properly redacted) |
| Adversarial attempts blocked | **171** DENIED verdicts across 5 adversarial agents |

The guard's lock-based serialization (for standard resources), the deferred resolution pattern (for parking and boardroom), and the mark space's scope visibility system all prevented conflicting writes. Department rooms at PROTECTED visibility remained invisible in content to outsiders while projected reads gave other departments enough structural information to coordinate without disclosure. CLASSIFIED tasks remained fully invisible outside the owning department. The 5 adversarial agents generated 171 DENIED verdicts (121 cross-department bookings, 37 warning injections, 13 lunch orders at full capacity): the guard rejected every cross-department booking attempt and every unauthorized warning injection. An additional 19 DENIED verdicts came from normal agents whose lunch orders were denied when both meal types were fully booked.

---

## 2. Protocol Coverage

Every protocol feature produced measurable output:

**Mark type distribution (tool-level events, not total marks in space):**

| Mark type | Count | Notes |
|---|---|---|
| intent | 1,212 | All booking/claiming attempts that passed authorization |
| action | 927 | Successful bookings |
| observation | 5 | Parking bot capacity reports, 1 per day |
| warning | 7 | Building ops maintenance alerts, current-day only |
| need | 126 | Boardroom YIELD_ALL + lunch exhaustion |

The cumulative mark space totals are higher ([Section 5.1](#51-mark-accumulation)) because the guard writes additional intent and action marks internally during the pre_action/post_action cycle, and deferred resolution writes marks during end-of-round allocation.

**Verdict distribution:**

| Verdict | Count | Notes |
|---|---|---|
| allow | 767 | Successful actions |
| conflict | 475 | Resource already taken |
| blocked | 431 | Boardroom + parking deferred to resolution |
| denied | 190 | Adversarial attempts + lunch fully exhausted |

**Projected reads:** 8,197 (PROTECTED scope reads, including cross-dept room viewing)

### What each feature demonstrated

**INTENT + ACTION marks:** 1,212 intents written, 927 converted to actions. The 285-intent gap reflects boardroom and parking intents consumed by end-of-round resolution rather than direct agent action, plus adversarial intents that were denied. Intents are the protocol's mechanism for declaring interest before commitment; they expire (TTL=2h) between rounds, so stale interest doesn't accumulate.

**OBSERVATION marks (5):** Parking bot wrote one capacity observation per day ("28 of 30 spots remaining on mon"). These decayed over the week. A Monday observation read on Friday had near-zero strength (100h age, 6h half-life = ~0.00001 strength). Agents saw stale observations correctly flagged as decayed.

**WARNING marks (7):** Building ops bot issued maintenance warnings on shared rooms, only for rooms with current-day bookings. With a 2h half-life, warnings were potent within their round but dead by the next AM. A maintenance alert for the current round is irrelevant by the next morning.

**NEED marks (126):** Two sources:
- **Boardroom YIELD_ALL:** Every boardroom request generated a NEED mark. The simulated principal resolved each at end-of-round, picking the highest-confidence claimant. All 10 slots were successfully allocated.
- **Lunch exhaustion:** When both meal types were fully booked for a time window, the protocol generated a NEED mark signaling capacity problems to a principal.

**Projected reads (8,197):** Agents made thousands of decisions based on incomplete information via the `view_all_rooms` tool, which provides cross-department room availability as projected reads. Department rooms are PROTECTED, so outsiders see that a room is booked at a time but not by whom or for what. Shared room projected reads continue as before: 80 of 100 normal agents lack content-level access and see redacted marks. Ops agents (as facilities management) still see full content for shared rooms. PROTECTED visibility working as specified across both room types.

**BLOCKED verdicts (431):** The deferred resolution pattern generated substantial BLOCKED traffic. Parking and boardroom requests are all initially BLOCKED pending priority resolution. The blocked verdicts represent queued requests waiting for multi-agent priority comparison, not failures.

**DENIED verdicts (190):** 121 from cross-department booking attempts (`book_other_dept_room`), 37 from warning injection attempts (`issue_warning`), and 32 from lunch orders when both types were fully booked for a time window.

---

## 3. Resource Allocation Results

### 3.1 Overview by resource type

| Resource | Supply/wk | Demand | Success rate | Policy |
|---|---|---|---|---|
| Lunch | 500 | 493 | 94% | FIRST_WRITER |
| Equipment | 80 | 90 | 66% | HIGHEST_CONFIDENCE |
| Dept rooms | 150 | 285 | 49% | HIGHEST_CONFIDENCE |
| Parking | 150 | 342 | 44%\* | HIGHEST_CONFIDENCE (deferred) |
| Tasks | 75 | 102 | 51% | FIRST_WRITER |
| Shared rooms | 50 | 241 | 23% | HIGHEST_CONFIDENCE |
| Boardroom | 10 | 152 | 0%\*\* | YIELD_ALL |

\* Parking: 150 allocated (head=17, regular=119, visitor=14) from 342 requests.

\*\* Boardroom: 0% at agent level (always BLOCKED), 100% at system level (all 10 slots resolved by principal).

### 3.2 Department completion rates

| Department | Rate |
|---|---|
| ops | 64.8% |
| eng | 61.6% |
| sales | 58.8% |
| design | 56.0% |
| product | 52.7% |

Completion rates cluster in a 12pp band (53-65%). Ops leads due to lighter room demand. Product trails because they generate the most shared room demand (35 attempts) and have the heaviest dept room contention (46 attempts for 3 rooms).

### 3.3 Parking: HIGHEST_CONFIDENCE with deferred resolution

Parking uses deferred resolution: all agents write intents during the round, then end-of-round resolution sorts by confidence and allocates the top-N requests to available spots.

| Priority | Tier | Conf | Trust | Spots/week |
|---|---|---|---|---|
| 1 | Visitor pre-allocation (parking bot) | 0.7 | 0.7 | 14 |
| 2 | Department heads | 0.95 | 1.0 | 17 |
| 3 | Regular employees | 0.5 | 1.0 | 119 |

Department heads always get their spots first. Confidence values have real meaning in the deferred allocation.

**Parking success by department:**

| Department | Demand | Allocated | Rate |
|---|---|---|---|
| product | 56 | 25 | 45% |
| eng | 57 | 25 | 44% |
| sales | 74 | 32 | 43% |
| ops | 73 | 30 | 41% |
| design | 60 | 16 | 27% |

Most departments cluster around 41-45%, reflecting the fundamental constraint: 136 employee spots (after 14 visitor pre-allocs) for 320 manifest items. Design is an outlier at 27%, likely due to scheduling order effects in this trial. Agents are shuffled each round to prevent systematic ordering bias.

### 3.4 Lunch: preference satisfaction

[No real office needs 100 AI agents racing to book salads. Lunch is a synthetic high-volume, low-stakes resource designed to stress-test FIRST_WRITER at scale and generate NEED marks on capacity exhaustion.]

| Department | Preferred type rate |
|---|---|
| eng | 79.3% |
| product | 75.6% |
| ops | 70.2% |
| sales | 67.6% |
| design | 61.1% |
| spread | 18.3pp |

Most agents get lunch (94% success, 32 of 493 requests denied when both types filled a time window). The meaningful metric is preferred type satisfaction: Type A (hot meal, 32/day capacity) is wanted by ~65% of agents. The 18.3pp spread reflects ordering advantages: eng and product agents tend to order earlier in the round, securing more Type A slots.


### 3.5 Shared rooms: the hardest resource

| Department | Attempts | Success | Rate |
|---|---|---|---|
| ops | 26 | 10 | 38% |
| sales | 58 | 16 | 28% |
| eng | 45 | 12 | 27% |
| design | 45 | 7 | 16% |
| product | 67 | 11 | 16% |

50 total shared room slots, 241 booking attempts = 4.8:1 contention, the highest of any resource type. Ops has the highest success rate (38%). Product generates the most demand (67 attempts) but converts at only 16%.

Agents adapt when their preferred room is taken by booking alternative rooms at the same time slot, which still satisfies the underlying need for a shared meeting room.


### 3.6 Boardroom: YIELD_ALL in action

All 10 boardroom time slots were resolved through the YIELD_ALL -> NEED -> principal resolution flow:

| Round | Slot | Winner | Role |
|---|---|---|---|
| 0 | Mon AM | eng-lead | head |
| 1 | Mon PM | eng-lead | head |
| 2 | Tue AM | ops-lead | head |
| 3 | Tue PM | product-12 | regular |
| 4 | Wed AM | design-lead | head |
| 5 | Wed PM | design-02 | regular |
| 6 | Thu AM | product-15 | regular |
| 7 | Thu PM | sales-11 | regular |
| 8 | Fri AM | eng-04 | regular |
| 9 | Fri PM | sales-lead | head |

Winners span all 5 departments. 5 of 10 winners were department heads or leads (eng-lead x2, ops-lead, design-lead, sales-lead), who had elevated confidence (0.95 vs 0.8). The remaining 5 were regular agents who won when no head competed for that slot or when they had the highest confidence among claimants.

### 3.7 Tasks: dependency chains working

| Department | Claimed/Attempted | Rate |
|---|---|---|
| sales | 5/5 | 100% |
| ops | 8/10 | 80% |
| product | 11/16 | 69% |
| eng | 15/38 | 39% |
| design | 13/33 | 39% |

Five departments have 15 abstract work units each (`eng/1` through `eng/15`, etc.). These are generic claimable items used to test the FIRST_WRITER conflict policy and dependency gating under the CLASSIFIED visibility level. Only department members can see or claim their own tasks. Sales' 5/5 (100%) comes entirely from adv-sales-03, the adversarial agent claiming within-scope tasks.

12 of 15 are immediately claimable. 3 have dependencies forming two separate chains:

```
2 -> 6           (depth 2, task 6 requires task 2)
5 -> 10 -> 15    (depth 3, task 10 requires 5, task 15 requires 10)
```

Example: `eng/10` cannot be claimed until some eng agent has successfully claimed `eng/5`. Once `eng/5` has an ACTION mark, `eng/10` becomes available. Then `eng/15` waits for `eng/10`. This creates a serial bottleneck: 20 eng agents may want `eng/10`, but they're all blocked until one of them finishes `eng/5`.

With FIRST_WRITER policy, the first agent to claim a task wins. Eng has the most attempts (38 for 15 tasks) because 20 eng agents each want 1-2 tasks, creating significant contention. The dependency chains serialize a small fraction of work.

---

## 4. Agent Behavior

### 4.1 Tool usage patterns

| Tool | Calls | Success | Rate | Notes |
|---|---|---|---|---|
| order_lunch | 493 | 461 | 94% | 32 denied (both types full) |
| request_parking | 342 | - | - | All BLOCKED, resolved by confidence |
| book_dept_room | 285 | 139 | 49% | Within-dept contention |
| book_shared_room | 241 | 56 | 23% | Highest contention resource |
| view_dept_rooms | 212 | - | - | Read-only reconnaissance |
| view_shared_rooms | 188 | - | - | Projected reads (PROTECTED) |
| my_status | 179 | - | - | Self-monitoring + warnings |
| book_boardroom | 152 | - | - | All BLOCKED, resolved by principal |
| book_other_dept_room | 121 | 0 | 0% | Adversarial only, all 121 denied |
| claim_task | 102 | 52 | 51% | Dependency bottlenecks |
| view_equipment | 99 | - | - | Read-only before booking |
| reserve_equipment | 90 | 59 | 66% | Lowest contention |
| view_tasks | 40 | - | - | Read-only task board checks |
| view_all_rooms | 39 | - | - | Cross-dept projected reads |
| issue_warning | 37 | 0 | 0% | Adversarial only, all denied |

Parking and boardroom use deferred resolution: every tool call returns BLOCKED, then end-of-round resolution allocates by confidence. All 121 `book_other_dept_room` attempts were denied by the guard's scope check (cross-department) or by the tool's own-department precondition. The tool rejects same-department targets before reaching the guard, ensuring the "other department" semantics are enforced at both layers.

Behavioral patterns:
- **View before book:** 212 dept room views preceded 285 booking attempts. 188 shared room views preceded 241 attempts. Agents check availability before acting.
- **Status checking:** 179 `my_status` calls. Agents monitor their bookings and check for warnings.
- **Low task browsing:** Only 40 view_tasks calls vs 102 claim attempts. Agents know their task list from the prompt and claim directly.

### 4.2 Steps per agent

| Department | Avg steps | Avg wasted | Efficiency | Avg rounds |
|---|---|---|---|---|
| design | 25.8 | 9.1 | 64.7% | 7.2 |
| product | 24.9 | 10.6 | 57.6% | 6.7 |
| sales | 21.6 | 9.3 | 56.8% | 6.3 |
| eng | 20.2 | 7.3 | 63.7% | 6.4 |
| ops | 18.6 | 6.2 | 66.3% | 6.7 |

Design agents are the busiest (~25.8 steps) because they target both shared rooms and equipment/dept rooms. Product agents have the lowest efficiency (57.6%) because they target the most contested resources. Ops agents are the most efficient (66.3%), targeting lower-contention resources.

Efficiency ranges from 57-66% across departments. The deferred parking/boardroom pattern counts every request as "wasted" at the tool level (BLOCKED), pulling efficiency down. Actual allocation happens in resolution, which isn't captured in per-tool efficiency.

### 4.3 Most and least active agents

**Most active:**

| Agent | Steps | Rounds | Wasted | Efficiency |
|---|---|---|---|---|
| adv-eng-00 | 97 | 10 | 59 | 39% |
| adv-product-02 | 92 | 10 | 58 | 37% |
| adv-design-01 | 72 | 10 | 52 | 28% |

**Least active:**

| Agent | Steps | Rounds | Wasted | Efficiency |
|---|---|---|---|---|
| eng-11 | 13 | 5 | 5 | 62% |
| ops-09 | 13 | 6 | 4 | 69% |
| sales-04 | 11 | 5 | 3 | 73% |

Adversarial agents dominate the most-active list because they attempt cross-department bookings every round (all denied) in addition to their normal manifest items. adv-design-01 has the worst efficiency (28%): 52 of 72 steps were wasted, mostly from denied cross-department attempts. The least active agents have small manifests and complete or fail quickly.

### 4.4 Waste rate increases over time

| Round | Slot | Waste rate | Notes |
|---|---|---|---|
| 0 | Mon AM | 41% | Initial rush + all parking BLOCKED |
| 1 | Mon PM | 27% | Fewer agents, less contention |
| 2 | Tue AM | 38% | Fresh day, new parking round |
| 3 | Tue PM | 40% | PM slots contested |
| 4 | Wed AM | 41% | Midweek, resources thinning |
| 5 | Wed PM | 53% | Shared rooms depleted |
| 6 | Thu AM | 44% | Similar pattern |
| 7 | Thu PM | 40% | Continuing contention |
| 8 | Fri AM | 45% | Most resources taken |
| 9 | Fri PM | 53% | Last round, depleted resources |

The baseline waste rate sits around 40% because every parking and boardroom request is counted as BLOCKED (wasted). Wed PM and Fri PM reach 53% because most resources for the week are booked and remaining agents are retrying failed items.

### 4.5 Heads vs regular agents

| Group | Count | Avg steps | Efficiency |
|---|---|---|---|
| Heads | 5 | 26.0 | 56.9% |
| Regulars | 95 | 22.0 | 62.0% |
| Adversarial | 5 | 79.6 | 38.7% |

Heads and regulars are similar in volume and efficiency. Heads have slightly more steps (26.0 vs 22.0) from boardroom requests (always BLOCKED). Their higher parking confidence (0.95) guarantees spots in deferred resolution, but this doesn't show in per-tool efficiency since all parking requests are counted as BLOCKED. Adversarial agents are dramatically more active (79.6 steps) with far lower efficiency (38.7%) because they waste steps on denied cross-department attempts every round.

---

## 5. Temporal Dynamics

### 5.1 Mark accumulation

| Round | Actions | Intents | Needs | Observations | Warnings |
|---|---|---|---|---|---|
| 0 | 192 | 244 | 9 | 1 | 1 |
| 1 | 223 | 277 | 13 | 1 | 3 |
| 2 | 391 | 486 | 25 | 2 | 4 |
| 3 | 417 | 516 | 32 | 2 | 5 |
| 4 | 578 | 743 | 51 | 3 | 5 |
| 5 | 597 | 773 | 63 | 3 | 6 |
| 6 | 741 | 958 | 72 | 4 | 6 |
| 7 | 769 | 1,003 | 90 | 4 | 7 |
| 8 | 912 | 1,185 | 109 | 5 | 7 |
| 9 | 927 | 1,212 | 126 | 5 | 7 |

Actions grow in a staircase pattern: AM rounds add ~150-170 actions, PM rounds add ~15-30. The cumulative total reaches 927 by round 9. The intent-action gap widens over time (from 52 in round 0 to 285 by round 9) because deferred intents (parking + boardroom) accumulate across rounds. Each round adds new intents that are resolved but not directly converted by the agent. Adversarial agents also contribute to the gap through denied intents.

Need marks grow steadily (126 total, ~13/round) driven primarily by boardroom requests and lunch exhaustion.

![Round progression](results/fig_round_progression.png)
*Four-panel view: active agents per round, steps vs waste, cumulative mark accumulation, and verdict distribution. The AM/PM pattern is visible: AM rounds have ~99-103 active agents while PM rounds drop to 35-46.*

### 5.2 Clock and decay

The clock models absolute week time:

| Round | Time | Clock (s) | Gap to next |
|---|---|---|---|
| 0 | Mon 8:00 | 28,800 | 4h |
| 1 | Mon 12:00 | 43,200 | 20h |
| 2 | Tue 8:00 | 115,200 | 4h |
| 3 | Tue 12:00 | 129,600 | 20h |
| ... | | | |
| 8 | Fri 8:00 | 374,400 | 4h |
| 9 | Fri 12:00 | 388,800 | n/a |

Gaps are non-uniform: AM->PM = 4h, PM->AM(next day) = 20h overnight. The non-uniformity matters for decay calculations. Overnight gaps produce much more decay than within-day gaps.

**Observation decay in practice:** Parking bot writes capacity observation Mon AM (t=28,800). By Tue AM (t=115,200), age=86,400s (24h), strength = 0.5^(24/6) = **0.063**, nearly stale. By Wed AM, effectively zero. Each day's observation is fresh and relevant only for that day.

**Warning decay:** Building ops issues a warning at round start. Half-life = 2h. Within the same round (age=0), full strength. By next PM round (4h later), strength = 0.25. By next AM (20h overnight), strength ~= 0. Warnings decay to negligible strength within a single overnight gap.

**Intent TTL:** Set to 2h. All round gaps are >=4h, so intents expire between every pair of rounds. Boardroom and parking resolution must therefore happen at end-of-round (same clock). By the next round, the intents are gone.

![Mark decay curves](results/fig_decay_curves.png)
*Theoretical decay curves with round boundaries marked. Observations (blue) retain 6.3% strength after one overnight gap (24h). Warnings (red) are effectively dead after 8h. Intents (orange dashed) have a hard TTL cutoff at 2h, shorter than the minimum inter-round gap (4h).*

---

## 6. What We Learned

### 6.1 The protocol works where architecture matches decomposition

105 agents (100 normal + 5 adversarial), 7 resource types, 3 visibility levels, 3 conflict policies, 2 external system agents, 10 rounds. Zero safety violations. The mark space maintained consistency under concurrent access throughout. 927 action marks written without a single conflicting state, including when agents made poor decisions and when adversarial agents actively tried to break scope boundaries.

Google's scaling research found the same pattern: agent systems work when the task structure matches the coordination architecture. Their data shows +80.8% improvement on parallelizable financial analysis but -39% to -70% degradation on sequential crafting tasks, across 180 configurations with up to 4 agents using direct-communication architectures (hub-and-spoke, peer-to-peer, hybrid).

Our scenario is dominated by parallelizable, independent resource claims, the structure stigmergic coordination handles well. Each agent's booking decision is independent at the protocol level. Serialization happens at the resource level (locks), not the agent level (no turn-taking), so adding agents doesn't degrade safety.

Stigmergic coordination falls outside Google's taxonomy entirely. Their five architectures (single-agent, independent, centralized, decentralized, hybrid) all involve either no shared state or direct agent-to-agent messaging. Environment-mediated coordination, where agents share state through marks without messaging each other, is a sixth architecture they did not evaluate. Their scaling predictions (super-linear turn growth at exponent 1.724, efficiency collapse beyond 3-4 agents) apply to direct-communication architectures and may not transfer to environment-mediated coordination. Our 105-agent result at 26x their maximum agent count suggests stigmergy sidesteps the communication overhead that drives their degradation curves, though a controlled comparison is needed to confirm this.

### 6.2 Fairness requires explicit design

Without agent shuffling, alphabetical ordering in the thread pool created a 92-percentage-point parking gap between eng (98%) and ops (5.9%). The protocol guarantees safety but not fairness. It prevents double bookings but doesn't ensure equitable access.

Randomizing agent order each round is essential. **Concurrent systems that serialize access must randomize submission order to avoid systematic bias.**

### 6.3 Lock-based guards break HIGHEST_CONFIDENCE

The guard holds an RLock across the entire pre_action -> tool_fn -> post_action cycle. This means at most one agent can be inside a guarded scope at any time. When two agents want the same parking spot, one finishes before the other starts. The second sees an already-taken resource and gets CONFLICT, not a confidence comparison.

This caused HIGHEST_CONFIDENCE to degenerate to FIRST_WRITER for all guard-protected resources. A department head with confidence=0.95 could lose a parking spot to a regular employee with confidence=0.5, purely because the regular employee's thread was scheduled first.

In real-time systems this problem is called **priority inversion**, the canonical case being the [Mars Pathfinder bug](https://www.cs.cornell.edu/courses/cs614/1999sp/papers/pathfinder.html) (1997), where a low-priority task holding a mutex blocked a high-priority task. NASA fixed it with priority inheritance in the VxWorks RTOS. In database systems, the solution is [optimistic concurrency control](https://doi.org/10.1145/319566.319567) (Kung & Robinson, 1981): transactions execute without locks, conflicts are detected only at commit time. Our deferred resolution pattern is a direct analogue: agents collect claims (the "read phase"), then end-of-round resolution compares by priority (the "validation phase").

**Fix: deferred resolution.** Agents write intents (unguarded), get BLOCKED, and end-of-round resolution sorts by confidence. Making HIGHEST_CONFIDENCE meaningful under lock-based serialization requires collecting all claims before comparing them.

The underlying tension: locks guarantee safety (no double bookings) but destroy the information needed for priority comparison (who else wants this resource?). The deferred pattern separates claim collection from allocation. This is a variant of the [Informational Braess Paradox](https://doi.org/10.1287/opre.2017.1712) (Acemoglu et al., 2018): more information (who currently holds the lock) can hurt when it forces serial processing of inherently parallel decisions. The [validation experiments (Section 4)](../validation/analysis.md#4-the-visibility-reversal) document the same phenomenon for calendar visibility.

### 6.4 YIELD_ALL is the right pattern for scarce shared resources

The boardroom's YIELD_ALL policy, where no agent can book directly and all requests are mediated by a principal, allocated all 10 slots across 5 departments. Unlike HIGHEST_CONFIDENCE (strongest claim wins) or FIRST_WRITER (fastest claim wins), YIELD_ALL defers every decision to the principal.

YIELD_ALL trades latency for fairness: agents wait until end-of-round for resolution instead of getting an immediate answer. Suitable for scarce resources where equitable access matters more than speed.

### 6.5 Projected reads enable coordination without full disclosure

8,197 projected reads means agents made thousands of decisions based on incomplete information. They could see that a room was booked at a time, but not by whom or for what. PROTECTED visibility in practice. Department rooms use PROTECTED visibility, and the `view_all_rooms` tool provides cross-department room availability as projected reads. Agents see projected availability for rooms in other departments, enabling coordination across organizational boundaries without disclosing booking details.

Only ops (facilities management) has content-level access to shared rooms. A realistic access model where the department responsible for building operations sees full booking details while others coordinate through projected marks.

### 6.6 Observation decay models information freshness

Parking bot observations decayed naturally. A "28 of 30 spots remaining" observation from Monday was effectively worthless by Wednesday (strength < 0.01). Agents could still read it, but the decay signal told them it was stale. This models real-world information aging without requiring explicit invalidation.

The absolute week-time clock model (Mon 8:00 = 28,800s, ... Fri 12:00 = 388,800s) with non-uniform gaps (4h AM->PM, 20h PM->next AM) produces realistic decay behavior: within-day observations are relevant, overnight observations are degraded, cross-day observations are essentially expired.

### 6.7 Where the protocol is weakest: sequential work

**Task dependencies (39% success for eng and design):** Google found sequential tasks degrade by 39-70% under multi-agent coordination; our dependency chains (task 5 → 10 → 15) show a 61% gap from theoretical maximum, within their predicted range. The mechanism matches their explanation: "unnecessary decomposition generates substantial coordination messages...consuming token budget on coordination rather than reasoning." 20 agents wait for one to finish task 5 before anyone can claim task 10. The protocol's FIRST_WRITER policy works at the individual claim level, but it can't parallelize inherently sequential work.

**Shared rooms (23% success rate):** The most contested resource with 4.8:1 contention. Agents see only projected marks (structure but no content) for other departments' rooms. The protocol doesn't help agents choose *which* resource to target. It only tells them whether their choice succeeded.

**Deferred resolution overhead:** The deferred pattern (boardroom + parking) adds 431 BLOCKED verdicts that inflate waste metrics. These aren't true failures (the resources get allocated), but from the agent's perspective, every request is "try and wait." There's no feedback loop for agents to adjust their confidence or timing.

**PM round degradation:** Later-in-day rounds have higher waste (53% on Wed PM and Fri PM) because resources are depleted. The protocol provides accurate conflict feedback but agents can't avoid attempting resources they don't know are full.

### 6.8 Token economics

| Metric | Value | Share |
|---|---|---|
| Total tokens | 7,954,713 | |
| Prompt tokens | 7,531,757 | 94.7% |
| Completion tokens | 422,956 | 5.3% |
| Per agent (avg) | 75,759 | |
| Per action mark | 8,581 | |
| Per round (avg) | 795,471 | |

Prompt tokens dominate (94.7%) because agents receive substantial context: system prompt with department info, manifest items, current bookings, warnings, and tool schemas. Completion tokens are lean; agents make tool calls with minimal reasoning.

The 8,581 tokens per successful action is the protocol's coordination overhead. This includes the agent's context, failed attempts (41.8% overall waste rate), and view calls before booking. For comparison, a simple "book room X" API call would be ~100 tokens. The ~86x overhead is the cost of decentralized coordination without a central scheduler. Google's data shows centralized MAS adds 285% token overhead and decentralized (peer-to-peer) adds 263%. This architecture avoids inter-agent messaging entirely. The overhead comes from tool schemas and mark-space reads.

Prompt tokens per agent climb 40% over the week (9,802 on Mon AM to 13,672 on Fri PM). Each agent's prompt includes only their own bookings (5-8 items by Friday), not the full mark space (927 marks). Growth plateaus as agents complete their manifests.

| Round | Prompt/agent | Steps/agent |
|---|---|---|
| 0 | 9,802 | 3.9 |
| 1 | 12,414 | 4.3 |
| 2 | 8,988 | 3.4 |
| 3 | 11,083 | 3.8 |
| 4 | 9,771 | 3.6 |
| 5 | 12,885 | 4.2 |
| 6 | 9,155 | 3.1 |
| 7 | 13,894 | 4.3 |
| 8 | 10,722 | 3.5 |
| 9 | 13,672 | 3.8 |

Cost growth is driven by the multi-step tool loop, not prompt size. PM rounds cost more per agent because agents retry after CONFLICT and check status more. Mon PM averages 4.3 steps/agent vs Tue AM's 3.4. Each retry is another full LLM call with the same prompt. Prompt size is O(agent's own bookings), which is bounded. Cost per round is O(retries), which grows with resource depletion.

![LLM cost scaling](results/fig_llm_cost_scaling.png)
*Prompt tokens per agent (bars) show a sawtooth pattern: PM rounds are more expensive than AM rounds due to more retry steps, not bigger prompts. The growth is bounded by agent-level booking count, not system-wide mark accumulation.*

---

## 7. Adversarial Robustness

Five adversarial agents (one per department) were added with normal permissions but adversarial system prompts instructing them to attempt cross-department resource acquisition and warning injection. They were given two extra tools not available to normal agents: `book_other_dept_room` (attempts to book a room in another department's scope, rejects same-department targets) and `issue_warning` (attempts to write WARNING marks to arbitrary scopes).

### 7.1 Adversarial agent activity

| Metric | Value |
|---|---|
| Adversarial agents | 5 (one per department) |
| Total adversarial steps | 398 |

**Adversarial verdicts:**

| Verdict | Count |
|---|---|
| denied | 171 |
| conflict | 48 |
| blocked | 25 |
| allow | 69 |

The 69 ALLOW verdicts are legitimate: adversarial agents succeeded only on resources within their normal permissions. Own-department rooms, shared rooms, tasks, lunch, and parking were all accessible because these agents held valid department membership. The protocol does not distinguish between adversarial and normal agents at the identity level; it enforces scope boundaries regardless of intent.

### 7.2 Cross-department booking attempts

| Result | Count | Rate | Notes |
|---|---|---|---|
| Denied | 121 | 100% | All rejected by scope check or own-dept precondition |
| **Total** | **121** | | |

All 121 attempts to book rooms via the cross-department tool were denied. The tool enforces "other department" as a precondition (rejecting same-department targets before reaching the guard), and the guard enforces scope boundaries (rejecting cross-department targets). Neither layer can be bypassed.

### 7.3 Warning injection attempts

| Result | Count | Rate |
|---|---|---|
| Denied | 37 | 100% |

All 37 attempts to write WARNING marks to scopes outside the agent's trust level were denied. WARNING marks require elevated trust (building ops bot level), and adversarial agents had normal employee trust. The guard rejects the write before the mark reaches the space.

### 7.4 Key finding

The guard and scope system enforce invariants independent of agent intent. The adversarial prompts instructed agents to be creative, persistent, and to try different approaches. Across 398 steps, 5 adversarial agents tested cross-department bookings, warning injection, and various tool parameter combinations. None succeeded in violating scope boundaries. All 121 cross-department booking attempts were denied. All 37 warning injection attempts were denied.

The protocol enforces invariants regardless of what the agent's prompt says. Adversarial and cooperative agents produce the same safety outcome; the difference shows up only in the DENIED verdict count.

---

## 8. Strengths and Limitations

**What held up:**
- Safety invariants (zero violations at 105-agent scale, including adversarial agents)
- Adversarial robustness (190 denied attempts across 5 adversarial agents)
- Multi-scope isolation (CLASSIFIED, PROTECTED, OPEN)
- Projected reads under PROTECTED visibility (8,197 redacted reads)
- Mediated resolution (YIELD_ALL -> NEED -> principal)
- External agent integration (different trust levels, decay)
- Temporal decay (observations, warnings, intent TTL)
- Confidence-based priority (when using deferred resolution pattern)

**What the protocol defers to other mechanisms:**
- **Priority comparison** is deferred to a resolution phase. Lock-based guards serialize access, which prevents the simultaneous comparison HIGHEST_CONFIDENCE needs. The deferred pattern (collect intents, resolve at batch boundary) restores it.
- **Fairness** is deferred to the scheduling layer. The protocol guarantees safety but is agnostic to submission order. Randomizing agent scheduling eliminates ordering bias.
- **Mediation** is deferred to a principal. YIELD_ALL explicitly hands contested decisions to an external authority. The protocol provides the signaling (NEED marks, `aggregate_needs()`); the principal provides the judgment.
- **Confidence validation** is deferred to the trust model. Nothing in the protocol prevents an agent from claiming confidence=1.0. Enforcing that confidence reflects actual priority is a policy layer concern. The adversarial test ([Section 7](#7-adversarial-robustness)) showed that confidence manipulation alone does not bypass scope enforcement: adversarial agents could not book resources outside their department scope regardless of claimed confidence.

**What the protocol cannot solve:**
- Strategic planning: agents have no cross-round memory; each round is a fresh decision with no ability to reason about future availability
- Demand-pressure visibility: an agent sees 3 open rooms but cannot know 40 others are about to pick the same one; the protocol shows current state, not impending contention
