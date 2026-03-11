# Validation Experiment Results

Experiment design: [`design.md`](design.md). Multi-phase extension: [`multi-phase_design.md`](multi-phase_design.md).

## Overview

1,439 completed trials across 5 models, 3 agent counts (3, 5, 10), 2 visibility levels, 2 temperatures, and 2 execution modes. Total cost: ~$20. Zero double bookings post-fix (exact 95% CI on failure rate: [0, 0.21%]).

The guard-based coordination protocol prevents safety violations regardless of model, temperature, concurrency, or agent count. The interesting findings are about efficiency, not safety.

## Experimental Setup

N agents share a 15-slot calendar (mon-fri, 09/11/14). All agents prefer the same slot (wed-14), forcing conflict. Each agent must book exactly one slot.

Two visibility conditions:
- **Visible**: agents have `view_calendar` + `book_slot` tools. They can see what's taken.
- **Hidden**: agents have only `book_slot`. They must trial-and-error: attempt preferred slot, get CONFLICT, try another.

Two execution modes:
- **Sequential**: agents take turns, one at a time. No race conditions.
- **Concurrent**: all agents run simultaneously via ThreadPoolExecutor. Real race conditions. Guard serializes conflicting writes.

### Models

| Model | Params | Architecture | $/1M input | $/1M output |
|-------|--------|-------------|-----------|------------|
| GPT-OSS 120B | 120B | MoE | $0.15 | $0.60 |
| Mercury 2 | - | Diffusion LLM | $0.25 | $0.75 |
| DeepSeek V3.2 | 671B (37B active) | MoE | $0.56 | $1.68 |
| Kimi K2.5 | 1T | Dense | $0.60 | $3.00 |
| GLM-5 | 744B (40B active) | MoE + Sparse Attn | $1.00 | $3.20 |

### Phases

| Phase | Mode | Trials | Models | Purpose |
|-------|------|--------|--------|---------|
| Pilot | Sequential | 480 | 4 original | Baseline variance, power calculations |
| Phase 2b | Concurrent | 480 | 4 original | Real coordination pressure |
| Phase 3 | Concurrent (hardest) | 240 | 3 (no GLM-5) | Safety stress test (N=10, t=0.7) |
| Mercury-2 | Sequential + Concurrent | 240 | Mercury 2 | Diffusion LLM comparison |

## Key Results

### 1. Safety: Zero Double Bookings

0 double bookings across 1,439 completed trials. The one double booking found during Phase 3 (before the guard fix) exposed a genuine race condition in the `execute()` method. The lock didn't span the full pre_action-tool_fn-post_action cycle, so two concurrent agents could both pass the intent check before either wrote its action mark. Fixing `execute()` to hold the RLock across the full cycle eliminated the issue. All subsequent trials (480+ concurrent, including Mercury 2) confirmed the fix.

Exact 95% CI on failure rate: [0, 0.21%] (Rule of Three: 3/n).

### 2. Visibility Matters More Than Model Choice

**Sequential ANOVA (600 trials, all 5 models, Type II):**

| Factor | F | p | Significant? |
|--------|---|---|-------------|
| Model | 25.48 | <0.001 | Yes |
| N agents | 1562.74 | <0.001 | Yes |
| Visibility | 3334.96 | <0.001 | Yes |
| Temperature | 20.56 | <0.001 | Yes |
| Visibility x N agents | 1562.74 | <0.001 | Yes |

Visibility and N agents dominate (F > 1500). Model and temperature are statistically significant but with much smaller effect sizes (F = 20-25) - they only matter in the hidden condition where trial-and-error introduces variance. All five models solve the visible condition in exactly 2 steps/agent (view, book) regardless of N. The hidden condition forces trial-and-error, and steps scale with N because each successive agent has more taken slots to collide with.

Effect sizes (Cohen's d, visible vs hidden):
- N=3: d=0.36 (small, because even agents in the hidden-calendar condition get lucky at low N)
- N=5: d=4.97
- N=10: d=6.19

### 3. Concurrency Amplifies Model Differences

**Concurrent ANOVA (841 trials, all 5 models, Type II):**

| Factor | F | p | Significant? |
|--------|---|---|-------------|
| Model | 66.09 | <0.001 | Yes |
| N agents | 967.30 | <0.001 | Yes |
| Visibility | 640.78 | <0.001 | Yes |
| Temperature | 21.72 | <0.001 | Yes |
| Visibility x N agents | 5.33 | 0.005 | Yes |

Under concurrency, model and temperature effects are amplified (model F=66.09 vs 25.48 sequential, temperature F=21.72 vs 20.56). The visibility x N agents interaction is significant but small (F=5.33) - the main effects of visibility and N agents individually explain most of the variance. Concurrent execution is the condition that separates models.

### 4. The Visibility Reversal

The most counterintuitive finding. In sequential execution, agents in the visible-calendar condition are faster (fewer steps) than those in the hidden-calendar condition. Expected: you can see what's taken, you pick something free, done.

In concurrent execution, this reverses. Agents with calendar visibility take MORE steps than those without it.

**Concurrent mode, mean steps/agent:**

| | N=3 visible | N=3 hidden | N=5 visible | N=5 hidden | N=10 visible | N=10 hidden |
|---|---|---|---|---|---|---|
| GPT-OSS | 3.73 | 2.00 | 4.44 | 2.93 | 6.65 | 4.81 |
| Mercury 2 | 3.62 | 2.00 | 4.28 | 2.87 | 6.78 | 4.38 |
| DeepSeek | 3.10 | 1.95 | 3.98 | 2.90 | 5.67 | 5.19 |
| Kimi | 3.47 | 2.00 | 4.16 | 2.76 | 4.96 | 4.30 |
| GLM-5 | 3.13 | 1.97 | 3.14 | 2.61 | 4.01 | 3.81 |

Why: when agents run concurrently and all call `view_calendar` at roughly the same time, they all see the same state (mostly empty). They all pick wed-14. They all get CONFLICT. They re-read, see wed-14 is taken, pick the next best. But with 10 agents re-reading simultaneously, they see the same second-choice slot, and collide again. The view step costs a turn but provides stale information under concurrency.

Agents in the hidden-calendar condition don't have this problem. They just try slots. When 10 agents blindly try wed-14, 1 succeeds and 9 get CONFLICT. Each of the 9 picks a different random slot (there's enough randomness in which slots they try next) and mostly succeeds on the second attempt. They waste attempts, but each attempt is cheap.

This has a direct implication for system design: **giving concurrent agents more information can hurt when that information is instantly stale.** The view_calendar tool becomes actively counterproductive under concurrency because it synchronizes agent behavior, creating thundering-herd collisions on the same "best available" slot.

This is an instance of the [Informational Braess Paradox](https://doi.org/10.1287/opre.2017.1712) (Acemoglu et al., 2018): providing more information to agents in a congestion game can increase total congestion. In traffic networks, publishing real-time route data causes drivers to crowd the same "optimal" route. In our mark space, publishing real-time calendar data causes agents to crowd the same "optimal" slot. The mechanism is identical: shared information synchronizes choices, and synchronized choices create collisions that outweigh the informational benefit. Acemoglu et al. prove this theoretically for Bayesian agents in congestion games; our experiments demonstrate it empirically for LLM agents in a coordination protocol.

### 5. Model Efficiency Is Not About Token Price

The cheapest model per token (GPT-OSS at $0.15/1M input) is not the most efficient agentic model:

| Model | $/1M input | $/1M output | Steps/agent (avg) | $/trial (avg) | Tokens/trial (avg) |
|-------|-----------|------------|-------------------|---------------|-------------------|
| GPT-OSS 120B | $0.15 | $0.60 | 3.97 | $0.005 | 27,667 |
| Mercury 2 | $0.25 | $0.75 | 3.26 | $0.005 | 20,643 |
| DeepSeek V3.2 | $0.56 | $1.68 | 3.79 | $0.022 | 34,826 |
| Kimi K2.5 | $0.60 | $3.00 | 3.47 | $0.016 | 20,979 |
| GLM-5 | $1.00 | $3.20 | 2.76 | $0.018 | 14,821 |

GPT-OSS and Mercury 2 tie on $/trial (~$0.005) because of their low token prices. But the story diverges on steps: Mercury 2 needs 18% fewer steps than GPT-OSS (3.26 vs 3.97) while costing the same per trial. Under the hardest concurrent conditions (N=10, visible, t=0.0), the gaps widen:

| Model | Steps/agent (N=10, visible, t=0.0, concurrent) |
|-------|------------------------------------------------|
| GPT-OSS 120B | 7.74 |
| Mercury 2 | 6.62 |
| Kimi K2.5 | 5.86 |
| DeepSeek V3.2 | 4.86 |
| GLM-5 | 4.38 |

GPT-OSS takes nearly twice the steps of GLM-5. Mercury 2 sits in the middle of the pack on steps, but its diffusion architecture delivers ~1,000 tokens/second throughput.

Each step is an LLM call with growing context (prior tool calls accumulate in the conversation). More steps means more tokens per step (quadratic growth in prompt tokens), more latency, and more chances for the agent to go off-track.

For agentic workflows, the cost equation is not `tokens x price_per_token`. It's closer to:

```
cost = steps x tokens_per_step(step_number) x price_per_token
```

Where `tokens_per_step` grows with step number because conversation history accumulates. A model that solves the task in 3 steps at $1.00/1M tokens will often be cheaper and faster than a model that takes 6 steps at $0.15/1M tokens, because the 6-step model's later steps are fat with context.

**GLM-5 is the most step-efficient model** despite being the most expensive per token. It solves the coordination task in fewer steps across every condition. The catch: GLM-5's API was unreliable during the experiment (199/681 Phase 2b records were timeout/503 errors), so reliability is a separate consideration.

**Mercury 2 is the most step-efficient cost tradeoff.** It matches GPT-OSS on $/trial while being 18% more step-efficient.

### 6. Scaling Behavior

Steps/agent in the hidden condition is roughly O(N/2): each agent after the first collides once on the preferred slot, then succeeds. The visible sequential condition is O(1), always 2 steps regardless of N. These bounds are tight:

| N | Visible seq | Hidden seq | Visible conc | Hidden conc |
|---|------------|-----------|-------------|------------|
| 3 | 2.00 | 1.67-2.00 | 2.00-4.00 | 1.67-2.00 |
| 5 | 2.00 | 2.00-3.00 | 2.40-5.60 | 2.00-3.00 |
| 10 | 2.00 | 3.30-5.50 | 2.80-8.60 | 3.20-5.50 |

The interesting scaling is in the concurrent visible condition. Steps/agent grows faster than hidden because the thundering-herd effect amplifies with N: more agents reading the same stale state means more simultaneous collisions.

### 7. Waste Ratio

Waste ratio = wasted attempts / total steps. A wasted attempt is a book_slot that returns CONFLICT.

- Visible sequential: 0.00 (agents see what's taken, never attempt a taken slot)
- Hidden sequential N=10: 0.70-0.82 (most attempts are wasted because agents can't see the calendar)
- Visible concurrent N=10: 0.14-0.40 (concurrent stale-read collisions waste some attempts)
- Hidden concurrent N=10: mirrors hidden sequential because the trial-and-error pattern is similar

The waste ratio for hidden conditions is structurally determined by N and the slot count, not by the model. All models produce similar waste ratios in the same condition because the conflict structure is mechanical.

### 8. Temperature

Temperature's effect is asymmetric:
- **Sequential**: significant but small (F=20.56, p<0.001). At t=0.0, all visible conditions are deterministic (SD=0). At t=0.7, most visible conditions are still deterministic. Temperature introduces variance only in hidden conditions with large N. The effect is only detectable after controlling for the much larger visibility and N effects in the factorial ANOVA.
- **Concurrent**: significant (F=21.72, p<0.001). Temperature mostly reduces steps (agents make slightly different choices, reducing herd behavior), but increases variance.

Temperature at 0.7 introduces variance in 5 out of 48 pilot cells (all hidden, most at larger N). It does not introduce variance in any visible condition. This makes sense: visible agents always view then book the best available slot. Temperature randomizes the "reasoning" but the optimal action is unambiguous given the calendar state.

### 9. The Guard Race Condition

Phase 3 found 1 double booking in the pre-fix data (deepseek-v3p2, N=10, visible, t=0.7, trial 35). Root cause:

```
Thread A: pre_action() → ALLOW           (no action marks exist)
Thread B: pre_action() → ALLOW           (still no action marks, A hasn't written one yet)
Thread A: tool_fn() → success
Thread A: post_action() → writes Action
Thread B: tool_fn() → success             (guard already said ALLOW)
Thread B: post_action() → writes Action   (double booking)
```

The `execute()` method held the lock only during `pre_action()`, releasing it before `tool_fn()`. Fix: hold the RLock across the entire `pre_action → tool_fn → post_action` cycle. The RLock (not Lock) is necessary because `execute()` calls `pre_action()` which also acquires the lock.

This is the kind of bug that only surfaces under real concurrency with enough trials. A unit test with 2 threads might not trigger the interleaving. The 240-trial Phase 3 stress test (N=10, concurrent, t=0.7) was specifically designed to find this class of bug.

## Summary Table

| Condition | Steps/agent range | Safety | Key finding |
|-----------|------------------|--------|-------------|
| Visible, sequential | 2.00 (all 5 models, all N) | 0 violations | Deterministic. Model doesn't matter. |
| Hidden, sequential | 1.67 - 5.50 | 0 violations | Scales with N. Model matters slightly. |
| Visible, concurrent | 2.00 - 8.60 | 0 violations | Visibility reversal. Stale reads cause thundering herd. |
| Hidden, concurrent | 1.67 - 5.50 | 0 violations | Similar to sequential hidden. Guard serialization preserves structure. |

## Figures

### Visibility Effect (Sequential vs Concurrent)

Visible vs hidden calendar by model in sequential mode. The gap only appears at N>=5 because N=3 agents in the hidden-calendar condition get lucky often enough.

![Steps by visibility, sequential](figures/steps_by_visibility_sequential_(pilot).png)

Same comparison under concurrent execution. The reversal: agents with calendar visibility now take MORE steps than those without.

![Steps by visibility, concurrent](figures/steps_by_visibility_concurrent_(phase_2b).png)

### Condition Heatmaps

Mean steps/agent across all sequential conditions. The visible block is uniformly 2.0. Hidden + high N is where the work happens.

![Heatmap, sequential](figures/heatmap_sequential_(pilot).png)

Same for concurrent. The visible high-N cells are now the warmest, showing the thundering-herd effect.

![Heatmap, concurrent](figures/heatmap_concurrent_(phase_2b).png)

### Sequential vs Concurrent

Direct comparison in the hidden condition, where the execution mode difference is cleanest.

![Sequential vs concurrent, hidden](figures/seq_vs_concurrent_hidden.png)

### Waste and Scaling

Wasted attempts scale with N in hidden conditions, remain near-zero in visible sequential.

![Waste ratio](figures/waste_ratio.png)

Steps/agent vs N for all conditions. O(1) for visible sequential, sub-linear growth elsewhere.

![Scaling](figures/scaling.png)

### Model Performance

Cost/trial, tokens/trial, wall clock, and steps under the hardest condition (N=10, visible, concurrent).

![Model performance](figures/model_performance.png)

### Temperature Effect

SD at t=0.0 vs t=0.7. Temperature introduces variance only in hidden conditions.

![Temperature effect](figures/temperature_effect.png)

### Safety

Cumulative double-booking rate across all trials with exact confidence interval. Flat at zero.

![Safety, cumulative double bookings](figures/safety_cumulative.png)

## Cost Breakdown

| Phase | Trials | Cost |
|-------|--------|------|
| Pilot (sequential) | 480 | $4.59 |
| Phase 2b (concurrent) | 480 | $7.02 |
| Phase 3 (stress test) | 240 | $6.42 |
| Mercury 2 (seq + conc) | 240 | ~$1.26 |
| **Total** | **1,440** | **~$19.30** |

## Agentic Cost: Why Token Price Misleads

In a single-turn API call, cost = tokens x price. For agentic multi-turn workflows, this breaks down because of conversation history accumulation.

The system prompt and tool definitions are a fixed cost (~400 tokens), sent identically on every step. What grows is the conversation history: each prior tool call + result (~400-500 tokens per turn pair) gets re-sent on every subsequent API call because the chat completions API is stateless. Per-step prompt size grows linearly with step count:

| Step | Fixed (system+tools) | Accumulated history | Total prompt | Completion |
|------|---------------------|-------------------|-------------|-----------|
| 1 | ~400 | 0 | ~400 | ~80 |
| 2 | ~400 | ~500 | ~900 | ~80 |
| 3 | ~400 | ~1,000 | ~1,400 | ~80 |
| 6 | ~400 | ~2,800 | ~3,200 | ~80 |
| 10 | ~400 | ~5,100 | ~5,500 | ~80 |

Total prompt tokens summed across all steps of a 6-step agent: ~400 + 900 + 1400 + 1900 + 2500 + 3200 = ~10,300. For a 3-step agent: ~400 + 900 + 1400 = ~2,700. The 6-step agent consumes 3.8x the prompt tokens of the 3-step agent, not 2x, because the history is re-sent on every call (triangle sum).

But the real cost of extra steps in agentic workflows is latency, not tokens. Each step is a blocking LLM round-trip. An agent taking 8 steps at 500ms/call spends 4 seconds waiting. An agent taking 4 steps spends 2 seconds. Token cost differences between models are dwarfed by step-count differences:

- GPT-OSS at 6.65 steps/agent (N=10 concurrent visible) means 66% more round-trips than GLM-5 at 4.01
- Mercury 2 at 6.78 steps is mid-pack on steps

Model-level step efficiency data:

| Model | Mean steps/agent | SD | Waste ratio | Mean wall clock/trial (s) |
|-------|-----------------|-----|-------------|--------------------------|
| GLM-5 | 2.76 | 0.86 | 35.5% | 110.78* |
| Mercury 2 | 3.26 | 1.46 | 39.1% | 4.68 |
| Kimi K2.5 | 3.47 | 1.29 | 41.7% | 21.35 |
| DeepSeek V3.2 | 3.79 | 1.61 | 43.0% | 54.89 |
| GPT-OSS 120B | 3.97 | 1.77 | 43.9% | 14.75 |

Wall-clock times reflect API rate limits, not model speed. They are not meaningful for model comparison. GLM-5's extreme value is due to 199/681 Phase 2b records being timeout errors.

## Multi-Phase Coordination

The single-phase results above test coordination within one conversation. Every trial is one-shot: agents get a fresh context, book one slot, done. The clock is frozen, so intent TTL and observation decay never activate. This says nothing about coordination across time: agents resuming after context loss, navigating state they didn't create, or operating with expired intents in the mark space.

Four follow-up experiments test multi-phase coordination. All use 2 models (gpt-oss-120b, mercury-2), 3 agent counts, 2 visibility levels, 2 temperatures, 10 trials/cell.

### 10. Cross-Round Safety: The Self-Re-Booking Gap

When agents run multiple booking rounds on the same calendar, a new failure mode emerges. In Round 2, agents get a fresh LLM conversation but retain their identity (same UUID). The calendar retains Round 1 action marks (permanent). Intent marks from Round 1 expire (TTL=30min, clock advances 1 hour between rounds).

The guard's conflict check filters `agent_id != agent.id`, which allows agents to re-book their own slots. This is correct within a single round (an agent correcting itself), but creates a cross-round safety gap:

**Sequential 2-round results (300 trials, 24 cells):**

| Condition | Steps/agent | Double bookings | Completion |
|-----------|-------------|----------------|------------|
| Visible N=3 | 2.00 | 0 | 100% |
| Visible N=5 | 2.00 | 0 | 100% |
| Visible N=10 | 2.20 | 40 | 0% (R2) |
| Hidden-cal N=3 | 2.00 | 218 | 100% |
| Hidden-cal N=5 | 2.89 | 209 | 100% |
| Hidden-cal N=10 | 5.07 | 329 | 92% |

In the visible-calendar condition, agents see the calendar and pick a new slot. Zero double bookings at N=3 and N=5. At N=10, the calendar is nearly full (10/15 slots taken in R1), so some agents re-book their own slot as the only perceived option, and R2 completion hits 0% because there aren't enough free slots for all 10 agents.

In the hidden-calendar condition, agents can't see the calendar. They try their preferred slot (wed-14, which they booked in Round 1). The guard allows it (self-re-booking permitted). Every agent in every trial re-books their own slot. The double booking count is almost exactly N x trials, a structural rather than stochastic pattern.

This is the first condition in the entire validation where double bookings are observed. It is not a guard bug. It is a policy gap: the guard was designed for single-round coordination where self-re-booking means self-correction. In multi-round coordination, self-re-booking means wasting a slot.

### 11. Block-Self-Rebook Eliminates the Gap

Added `block_self_rebook` parameter to the guard. When enabled, the conflict check treats the agent's own prior action marks the same as any other agent's, returning CONFLICT.

**Block-self-rebook results (240 trials, 24 cells):**

| Condition | Steps/agent (default) | Steps/agent (block) | Dbl (default) | Dbl (block) |
|-----------|----------------------|--------------------|--------------:|------------:|
| Visible N=3 | 2.00 | 2.00 | 0 | 0 |
| Visible N=5 | 2.00 | 2.00 | 0 | 0 |
| Hidden-cal N=3 | 2.00 | 3.0-3.5 | ~30 | 0 |
| Hidden-cal N=5 | 2.8-3.0 | 4.2-5.2 | ~45 | 0 |
| Hidden-cal N=10 | 4.8-5.4 | 7.8-9.5 | ~85 | 0 |

**Zero double bookings across all 240 trials.** Both models, all N, both temperatures, both visibility conditions. The policy change completely solves the cross-round safety gap.

The cost: agents in the hidden-calendar condition take 1.5-4 more steps/agent because they hit CONFLICT on their own Round 1 slot and must retry. This tradeoff between safety and efficiency is expected. In the visible-calendar condition, there is no cost: agents see the calendar, see their own slot is taken, and pick a new one.

For production multi-round systems, `block_self_rebook=true` should be the default. The efficiency cost is paid only by agents that can't see the resource state, and even then it's bounded by the number of prior bookings (at most 1 extra CONFLICT per prior round).

### 12. Concurrent Multi-Phase

Does the visibility reversal ([Section 4](#4-the-visibility-reversal)) persist across rounds?

**Concurrent 2-round results (238 trials, 24 cells):**

| Condition | Steps/agent (seq 2-round) | Steps/agent (conc 2-round) |
|-----------|--------------------------|---------------------------|
| Visible N=3 | 2.00 | 3.49 |
| Visible N=5 | 2.00 | 4.34 |
| Visible N=10 | 2.20 | 6.17 |
| Hidden-cal N=3 | 2.00 | 1.99 |
| Hidden-cal N=5 | 2.89 | 2.90 |
| Hidden-cal N=10 | 5.07 | 4.98 |

The visibility reversal persists. Under concurrent multi-phase execution, visible-calendar agents take substantially more steps than hidden-calendar agents. The thundering-herd effect from [Section 4](#4-the-visibility-reversal) compounds with the cross-round partially-filled calendar: all concurrent agents re-read the same state, see the same "best available" slot, and collide repeatedly.

Hidden-calendar agents are unaffected by concurrency in multi-phase trials, with step counts nearly identical to sequential. This matches the single-phase finding: trial-and-error with random slot selection naturally distributes agents across slots, avoiding the synchronized collisions that plague the visible-calendar condition.

Double booking patterns are unchanged from sequential: hidden-calendar condition produces them (self-re-booking), visible-calendar does not (at N=3 and N=5). Total double bookings across concurrent multi-phase trials: Hidden N=3: 117, Hidden N=5: 172, Hidden N=10: 309, Visible N=10: 39.

### 13. Calendar Capacity vs Coordination

With 15 slots and N=10 agents doing 2 rounds, Round 2 needs 10 new bookings but only 5 slots remain. This makes R2 completion impossible in the visible-calendar condition. Is this a coordination failure or a capacity constraint?

**30-slot calendar results (80 trials, 8 cells, N=10 only):**

| Condition | Steps/agent (15 slots) | Steps/agent (30 slots) | R2 comp (15) | R2 comp (30) |
|-----------|----------------------|----------------------|:-------------|:-------------|
| Visible N=10 | 2.2-2.7 | 2.00 | 0% | 100% |
| Hidden-cal N=10 | 4.8-5.4 | 4.4-4.6 | 80-100% | 70-100% |

With 30 slots, visible-calendar agents complete both rounds at 2.0 steps/agent, matching single-round single-phase efficiency. The R2 completion failure at N=10 with 15 slots was purely a capacity artifact.

Hidden-calendar agents still produce double bookings (53-64 per cell) because the self-re-booking gap is orthogonal to capacity. They also still take ~4.5 steps/agent because they can't see which of the 30 slots are taken and must trial-and-error through conflicts.

### 14. Progressive Resource Exhaustion (5 Rounds)

With N=3 and 15 slots, 5 rounds use exactly all 15 slots (3 × 5 = 15). This tests whether mark accumulation degrades coordination as the calendar fills.

**5-round results (80 trials, 8 cells, N=3 only):**

| Condition | Steps/agent | Double bookings | Completion |
|-----------|-------------|:---------------|:----------|
| Visible | 2.00 | 0 | 100% (90% mercury-2 t=0.7) |
| Hidden-cal | 2.00 | 113-120 | 100% |

Visible-calendar agents maintain exactly 2.0 steps/agent across all 5 rounds, even as the calendar goes from empty to completely full. Each round, they view the calendar, see what's taken, book a free slot. Mark accumulation (up to 15 action marks by Round 5) does not degrade their performance.

Hidden-calendar agents also maintain 2.0 steps/agent, but this is misleading. They re-book their own slot every round (self-re-booking), so they never encounter a conflict. The double booking count of ~120 per cell = 3 agents × 4 re-bookable rounds × 10 trials. Every agent after Round 1 is re-booking a slot they already hold. The 100% completion rate masks the fact that no new slots are actually being claimed.

This confirms that multi-phase coordination works correctly when agents can observe resource state. The mark space faithfully records all prior actions, and agents use that information to coordinate across arbitrary numbers of rounds. The failure mode is exclusively in the hidden-calendar condition where agents can't observe the accumulated marks.

### Multi-Phase Summary

| Finding | Evidence |
|---------|----------|
| Guard provides perfect within-round safety | 0 intra-round double bookings across all multi-phase experiments |
| Cross-round safety gap exists | Hidden-calendar condition produces structural double bookings from self-re-booking |
| `block_self_rebook` fully closes the gap | 0 double bookings across 240 trials with the policy enabled |
| Efficiency cost of blocking is bounded | +1.5-4 steps/agent in hidden-calendar only; zero cost in visible-calendar |
| Visibility reversal persists across rounds | Concurrent visible > concurrent hidden steps (thundering herd) |
| N=10 R2 completion=0% is capacity, not coordination | 30-slot calendar restores 100% completion at 2.0 steps/agent |
| Mark accumulation doesn't degrade coordination | 5 rounds, 15 action marks: visible agents maintain 2.0 steps/agent throughout |
| Hidden-calendar multi-round is fundamentally limited | Without observing marks, agents cannot coordinate across rounds |

The multi-phase experiments establish that stigmergic coordination scales across time. The mark space correctly handles clock advancement, intent decay, and action persistence. The guard's deterministic enforcement prevents within-round conflicts. The only failure mode is a policy decision (self-re-booking allowance) that is trivially fixed by a guard parameter. For production multi-phase systems: use `block_self_rebook=true` and ensure agents have visibility into the resource state.

## Limitations

1. **Single scenario.** All trials use the same calendar-booking task. Generalization to other coordination tasks (e.g., resource allocation, workflow orchestration) is not tested.
2. **Single conflict policy.** All trials use HIGHEST_CONFIDENCE. FIRST_WRITER and YIELD_ALL policies are implemented but not validated at scale.
3. **API reliability varies.** GLM-5 had 199 timeout/503 errors out of 681 Phase 2b attempts. Mercury 2 hit rate limits (429s) under parallel cell execution, requiring serialized retries. Model efficiency numbers are real but production use requires accounting for API reliability.
4. **No adversarial conditions.** All agents are cooperative. The trust-weighting and observation-decay mechanisms are tested in the discovery experiments but not under the factorial design.
5. **Fixed slot count.** 15 slots for all N in single-phase experiments. Multi-phase Experiment C tested 30 slots at N=10, confirming that capacity constraints (not coordination failures) drove the R2 completion=0% result.
6. **Sequential multi-phase only for block-self-rebook.** The block_self_rebook experiment was run sequentially. Concurrent + block_self_rebook was not tested but is expected to produce the same zero-double-booking result (the guard lock serializes the check).
7. **Same preferred slot across rounds.** All agents prefer wed-14 in all rounds. A more realistic design would vary preferences per round, but this was held constant to isolate the cross-round coordination variable.
