# Confirmatory Experiment Design: Stigmergic Coordination

## Purpose

Shift from discovery ("does this work?") to confirmation ("here's rigorous evidence it works, and these are the main effects"). The 9 discovery experiments (S-001 through S-008) validated the protocol qualitatively with a single model (Kimi K2.5). This document designs statistically powered experiments across multiple models and conditions.

## Models

Four models confirmed for use, all on Fireworks (OpenAI-compatible API):

| Model | Short Name | Params | Architecture | Context | Tool Calling | Notes |
|-------|-----------|--------|-------------|---------|-------------|-------|
| Kimi K2.5 | `kimi-k2p5` | 1T | Dense | 262k | Native structured | Proven in all 9 discovery experiments. Moonshot AI's flagship agentic model. |
| DeepSeek V3.2 | `deepseek-v3p2` | 671B (37B active) | MoE | 131k | Structured JSON | Fireworks-enabled. Documented as weak at multi-turn function calling, which our protocol tests directly. |
| GLM-5 | `glm-5` | 744B (40B active) | MoE + Sparse Attention | 203k | Supported | Z.ai's SOTA. Targets "complex systems engineering and long-horizon agentic tasks." |
| GPT-OSS 120B | `gpt-oss-120b` | 120B | MoE | 131k | Supported (fixed) | "Surpasses o3-mini, approaches o4-mini." Fireworks fixed tokenizer bug. Cheapest capable model. |

### Pricing

| Model | Input ($/1M tokens) | Output ($/1M tokens) | Cached Input | Relative Cost |
|-------|---------------------|----------------------|-------------|---------------|
| `gpt-oss-120b` | $0.15 | $0.60 | - | 1x (cheapest) |
| `deepseek-v3p2` | $0.56 | $1.68 | - | 3x |
| `kimi-k2p5` | $0.60 | $3.00 | $0.10 | 5x |
| `glm-5` | $1.00 | $3.20 | $0.20 | 6x |

### Model-Specific Risks

- **`deepseek-v3p2`**: Fireworks blog says "not great at multi-turn function calling." Our protocol requires multi-turn (view → book → retry). May underperform on hidden-visibility and concurrent conditions where more steps are needed.
- **`glm-5`**: Untested in our setup. MoE with sparse attention. Reasoning quality under tool-calling constraints is unknown.
- **`gpt-oss-120b`**: Fireworks fixed a tokenizer bug that caused malformed tool calls. Should be reliable now, but no prior data in our experiments.
- **`kimi-k2p5`**: Proven baseline. Most expensive per output token.

## Token Usage Estimation

### Per-Call Breakdown (from S-001 results)

A typical agent step involves:
- **System prompt**: ~100 tokens (agent name, preferred slot, strategy instructions)
- **Tool definitions**: ~200 tokens (2 tools with descriptions, parameter schemas)
- **Conversation history**: ~50-300 tokens (grows with each step, includes prior tool calls/results)
- **Output**: ~50-150 tokens (reasoning + tool call)

**Step 1 (view_calendar)**: ~500 input + ~100 output = ~600 tokens
**Step 2 (book_slot)**: ~700 input + ~80 output = ~780 tokens
**Total per agent (2 steps)**: ~1,400 tokens

For S-001 (2 agents, 2 steps each): ~2,800 tokens/trial

### Scaling with Agent Count

Each additional agent adds:
- Its own 2-step loop: ~1,400 tokens
- Slightly larger calendar state in view_calendar response: +50 tokens/agent

| N Agents | Steps/Agent | Est. Tokens/Trial |
|----------|-------------|-------------------|
| 3 | 2.0 | ~4,500 |
| 5 | 2.0 | ~7,500 |
| 10 | 2.0 | ~15,000 |

### Scaling with Concurrency

Concurrent execution adds guard conflicts and retries:
- Failed booking + re-read: +2 steps × ~800 tokens = ~1,600 per conflict
- N=5 concurrent averages 8 conflicts: +~12,800 tokens
- N=5 concurrent: ~7,500 + ~12,800 = ~20,000 tokens/trial

### Temperature Factor

Adding temperature > 0 does not change token counts. It changes output distribution. No cost impact.

## Why Not Uniform 50 Trials/Cell

Discovery data from Kimi K2.5 reveals a critical problem with uniform sample sizes:

**Most sequential conditions are deterministic (SD=0):**
```
S-001 conflict:      2.00 ± 0.00  (n=20)
S-005 all_same:      2.00 ± 0.00  (n=20)
S-006 open:          2.00 ± 0.00  (n=10)
S-006 classified:    3.00 ± 0.00  (n=20)
S-007 sequential:    2.00 ± 0.00  (n=10)
```

**Only concurrent conditions show variance:**
```
S-007 concurrent_n5: 5.20 ± 0.78  (n=20)
S-007 concurrent_n10:5.66 ± 1.01  (n=10)
S-008 concurrent_n5: 4.70 ± 0.71  (n=20)
```

At temperature 0.0, Kimi K2.5 takes exactly 2.0 steps every time. Zero variance. Even 3 trials would confirm the effect. 50 is wasted money.

Meanwhile, the effects we actually care about powering:

| Comparison | Cohen's d | n/group for 80% power |
|-----------|----------|----------------------|
| Sequential vs concurrent | 4.1 | 5 |
| Visible vs hidden (temp=0.0) | ∞ (SD=0) | 3 |
| FIRST_WRITER vs HIGHEST_CONFIDENCE | 0.47 | **70** |
| Concurrent N=5 vs N=10 | 0.51 | **61** |
| Model differences | Unknown | Unknown |
| Temperature effect | Unknown | Unknown |

The two unknowns (model, temperature) are the entire point of the validation work. We can't power-analyze them without data. A pilot solves this.

## Experimental Design

### Phase 1: Pilot (Estimate Variances)

**Goal**: Measure per-cell SDs across all 4 models and both temperatures. Determine which cells need large samples and which are deterministic.

**Design**: Same factorial as the full experiment, but 10 trials/cell.

| Factor | Levels | Values |
|--------|--------|--------|
| Model | 4 | kimi-k2p5, deepseek-v3p2, glm-5, gpt-oss-120b |
| N agents | 3 | 3, 5, 10 |
| Mark visibility | 2 | visible, hidden |
| Temperature | 2 | 0.0, 0.7 |

**Cells**: 4 × 3 × 2 × 2 = **48 cells**
**Trials per cell**: 10
**Total**: 480 trials

**Dependent variables**:
- Double bookings (binary safety measure)
- Steps/agent (efficiency, primary DV)
- Guard invocations (mechanism indicator)
- Completion rate (task success)
- Wasted attempts (cost of coordination)
- Token usage (prompt + completion, from API response)

**Cost estimate**:

| Condition | Tokens/Trial | Trials (4 models × 2 temps) | Total Tokens |
|-----------|-------------|----------------------------|--------------|
| N=3, visible | 4,500 | 80 | 360,000 |
| N=3, hidden | 6,000 | 80 | 480,000 |
| N=5, visible | 7,500 | 80 | 600,000 |
| N=5, hidden | 10,000 | 80 | 800,000 |
| N=10, visible | 15,000 | 80 | 1,200,000 |
| N=10, hidden | 20,000 | 80 | 1,600,000 |

**Total Phase 1**: ~5,040,000 tokens across 480 trials

| Model | Trials | Est. Cost |
|-------|--------|-----------|
| `gpt-oss-120b` | 120 | **$0.33** |
| `deepseek-v3p2` | 120 | **$1.06** |
| `kimi-k2p5` | 120 | **$1.51** |
| `glm-5` | 120 | **$1.95** |

**Total Phase 1 (pilot): ~$5**

**Pilot outputs** (per cell):
- Mean and SD of steps/agent
- Completion rate (out of 10)
- Failure modes (no tool call, malformed args, timeout)
- Token usage distribution

**Decision gate after pilot:**

1. **Drop failed models**: Any model with <50% completion across its 12 cells gets cut from remaining phases.

2. **Classify cells by variance**:
   - SD = 0 → **deterministic**. No additional trials needed. Pilot data is the final result.
   - SD > 0 → **stochastic**. Needs powered sample size.

3. **Compute required n per stochastic cell**: For each pair of cells we want to compare, use pilot SDs to compute n for 80% power at alpha=0.05. Formula: n = (z_α + z_β)² × (SD₁² + SD₂²) / δ², where δ is the observed or minimum interesting difference.

4. **Cap at 70 trials/cell** (sufficient for d=0.47, the smallest effect seen in discovery).

### Phase 2: Full Run (Powered by Pilot)

**Hypotheses** (pre-specified):
- H1: Mark visibility reduces steps/agent (visible < hidden)
- H2: Steps/agent is constant across N (O(1) scaling)
- H3: Model choice affects completion rate but not the safety invariant
- H4: Temperature > 0 increases variance in steps/agent without affecting safety
- H5: Concurrent execution increases guard conflicts with N
- H6: Re-read strategy recovers completion rate under concurrency
- H7: FIRST_WRITER outperforms HIGHEST_CONFIDENCE under concurrency

**Design**: Two sub-experiments using the same unified runner.

**Phase 2a: Effectiveness** (extends pilot cells):

Same 48-cell factorial. Top up each stochastic cell to its powered n. Deterministic cells keep their 10 pilot trials. Pilot trials count toward the total (not discarded).

**Phase 2b: Concurrency**:

| Factor | Levels | Values |
|--------|--------|--------|
| Model | K (from pilot) | Qualifying models |
| N agents | 3 | 3, 5, 10 |
| Execution mode | 2 | sequential, concurrent |
| Conflict policy | 2 | FIRST_WRITER, HIGHEST_CONFIDENCE |

Temperature fixed at 0.0 (isolate concurrency effect).

Trials/cell: Set by pilot SDs from concurrent conditions. Based on discovery data (SD ~0.8 for concurrent), expect 60-70/cell for policy comparison, 10-20/cell for sequential vs concurrent (d=4.1).

**Cost estimate** (scenario analysis):

*Scenario A: Most cells deterministic (like Kimi at temp=0.0)*
- ~30 of 48 pilot cells have SD=0 → keep at 10
- ~18 stochastic cells topped up to 50 → 40 additional trials each = 720
- Phase 2b: 48 cells, average 40/cell = 1,920
- Total additional: ~2,640 trials, ~$15-25

*Scenario B: New models/temperature introduce variance everywhere*
- All 48 cells stochastic, topped up to 50 → 40 additional each = 1,920
- Phase 2b: 48 cells × 60/cell = 2,880
- Total additional: ~4,800 trials, ~$30-50

*Scenario C: Some models fail, reducing cell count*
- 1 model drops out → 36 cells instead of 48
- Proportional cost reduction (~25%)

**Best estimate**: Scenario A/B blend. ~$20-35 for Phase 2.

### Phase 3: Safety Bound (Certification)

**Goal**: Establish a credible upper bound on failure probability.

**Design**: Cheapest qualifying model (`gpt-oss-120b`), simplest scenario (S-001, 2 agents, sequential, visible, temperature 0.0). Pure volume run.

| Trials | 0 failures → 95% CI upper bound |
|--------|----------------------------------|
| 500 | 0.60% |
| 1,000 | 0.30% |
| 2,000 | 0.15% |
| 3,000 | 0.10% |

At ~2,800 tokens/trial with `gpt-oss-120b`:

| Trials | Tokens | Cost |
|--------|--------|------|
| 1,000 | 2,800,000 | $0.74 |
| 3,000 | 8,400,000 | **$2.22** |

**Recommendation**: 3,000 trials. $2.22 for a 0.1% failure rate bound.

**Note**: All trials from Phases 1 and 2 also count toward the safety bound (pooled). If Phase 2 produces 3,000+ total trials with 0 failures, Phase 3 may be unnecessary. Decision made after Phase 2.

## Cost Summary

| Phase | Cells | Trials | Est. Tokens | Est. Cost | Purpose |
|-------|-------|--------|-------------|-----------|---------|
| 1: Pilot | 48 | 480 | 5M | ~$5 | Estimate variances, drop failed models |
| 2: Full run | 48-96 | 2,000-5,000 | 20-50M | ~$20-35 | Main effects + concurrency (powered) |
| 3: Safety bound | 1 | 0-3,000 | 0-8M | $0-2 | Failure rate bound (if needed) |

**Grand total: ~2,500-8,500 trials, ~25-63M tokens, ~$25-42**

The pilot-then-power approach saves money when cells turn out deterministic (no point running 50 trials of something with SD=0) and spends it where it matters (stochastic cells where the effect size is small). Worst case matches the uniform design. Best case cuts cost by 50%.

| Model | Pilot | Full Run (est.) | Safety | Total (est.) |
|-------|-------|----------------|--------|-------------|
| `gpt-oss-120b` | $0.33 | $2-4 | $0-2 | **$2-6** |
| `deepseek-v3p2` | $1.06 | $5-8 | - | **$6-9** |
| `kimi-k2p5` | $1.51 | $6-10 | - | **$8-12** |
| `glm-5` | $1.95 | $8-13 | - | **$10-15** |

## Temperature as a Factor

Temperature 0.0 (deterministic) was used in all discovery experiments. Adding temperature 0.7 tests:

1. **Robustness**: Does the protocol work when agents make less predictable choices?
2. **Diversity**: Do agents explore the slot space differently at higher temperature?
3. **Variance**: How much does stochastic sampling increase step count variance?

Temperature is a zero-cost factor (same tokens, same API calls). It doubles Phase 1/2a cells but provides a direct test of protocol robustness under realistic deployment conditions.

**Levels**: 0.0 and 0.7. Not 1.0, which is too high for tool calling (models hallucinate function names at high temperature). 0.7 is the standard "creative but coherent" setting used in most production deployments.

Phase 2b (concurrency) uses temperature 0.0 only to isolate the concurrency effect. If the pilot shows temperature has no main effect on safety, this is justified. If it does, Phase 2b would need to be extended.

**Key pilot question**: Does temperature 0.7 turn deterministic cells (SD=0) into stochastic ones? If yes, temperature is a real factor worth powering. If most cells stay at SD≈0, temperature doesn't matter for this protocol and can be reported as a null result.

## Analysis Plan

### Pilot Analysis (Phase 1)

1. **Variance classification**: For each of 48 cells, report mean, SD, min, max of steps/agent. Classify as deterministic (SD=0) or stochastic (SD>0).

2. **Model viability**: For each model, report completion rate across its 12 cells. Flag models with <50% completion in any cell.

3. **Power calculations**: For each pair of cells being compared (visible vs hidden, model A vs model B, etc.), use pilot SDs to compute required n for 80% power. Output a table of recommended sample sizes per cell.

4. **Temperature effect on variance**: Compare SD at temp=0.0 vs temp=0.7 within each model×N×visibility triple. Report whether temperature introduces stochasticity.

5. **Token usage**: Report mean tokens/trial by model and condition. Update cost estimates for Phase 2.

### Full Run Analysis (Phase 2)

1. **Safety**: Exact binomial 95% CI on failure rate, pooled across all trials (pilot + full run). Report: "0 failures in N trials; 95% CI [0, X%]."

2. **Main effects**: Depends on variance structure revealed by pilot.
   - If most cells are stochastic: Four-way ANOVA on steps/agent with model (4), N (3), visibility (2), temperature (2). Report main effects, two-way interactions, partial eta-squared.
   - If most cells are deterministic: ANOVA is inappropriate (violates normality/homogeneity). Use non-parametric alternatives (Kruskal-Wallis) or report exact values with CIs. For deterministic cells, the result is the value itself and no inference is needed.

3. **Post-hoc comparisons**:
   - Tukey HSD for pairwise model differences on steps/agent (stochastic cells only)
   - Planned contrast: visible vs hidden (H1)
   - Linear regression of steps/agent on log(N) to test O(1) claim (H2); slope should be ~0

4. **Completion rate**:
   - Chi-squared test of independence: model × completion
   - Fisher exact for pairwise model comparisons
   - Logistic regression: completion ~ model + N + visibility + temperature

5. **Effect sizes**: Cohen's d for pairwise comparisons (stochastic cells), partial eta-squared for ANOVA factors.

6. **Token usage**: Report mean tokens/trial by condition. Compute actual cost/trial by model. Descriptive only.

### Concurrency Analysis (Phase 2b)

1. **Conflict rate**: Poisson regression: conflicts ~ model + N + execution_mode + conflict_policy + interactions
2. **Sequential vs concurrent**: Wilcoxon signed-rank on steps/agent within each model × N cell (non-parametric, conflict counts are right-skewed)
3. **Conflict policy**: t-test on steps/agent: FIRST_WRITER vs HIGHEST_CONFIDENCE within each model × N × mode cell. This is the smallest expected effect (d≈0.47), requiring the most trials per cell (~70).
4. **Completion recovery**: McNemar's test comparing completion rates between sequential and concurrent within each model

### Phase 3 Analysis

1. **Clopper-Pearson exact CI** on failure rate at 95% and 99% confidence
2. **Sequential monitoring plot**: CI width vs cumulative trial count, showing convergence

## Implementation Requirements

### New Infrastructure

1. **Token tracking in LLMClient**: Extract `usage.prompt_tokens` and `usage.completion_tokens` from API responses. Store per-step.

2. **Unified experiment runner**: Single script (`experiments/validation/run.py`) that:
   - Takes factorial parameters: `--models`, `--agents` (list of N values), `--visibility` (visible/hidden/both), `--temperature` (list), `--trials-per-cell`
   - Runs all cells, saves structured results
   - Supports `--resume` to continue interrupted runs (appends to existing JSONL, skips completed cells)
   - Prints progress: cell X/Y, trial Z/N

3. **Early stopping**: If a model fails 10 consecutive trials in any cell, mark that cell as failed. Log the failure mode (no tool call, malformed args, timeout). Continue with remaining cells.

4. **Results format**: One JSONL per run, each line includes:
   ```json
   {
     "phase": "pilot",
     "cell": {"model": "kimi-k2p5", "n_agents": 5, "visibility": "visible", "temperature": 0.0},
     "trial_id": 7,
     "double_bookings": 0,
     "all_completed": true,
     "total_steps": 10,
     "steps_per_agent": 2.0,
     "guard_invocations": 0,
     "wasted_attempts": 0,
     "tokens": {"prompt": 3200, "completion": 450},
     "wall_clock_seconds": 4.2,
     "agents": [...]
   }
   ```

5. **Pilot analysis script** (`experiments/validation/analyze_pilot.py`):
   - Reads pilot JSONL
   - Computes per-cell mean, SD, completion rate
   - Classifies cells as deterministic (SD=0) or stochastic
   - Runs power calculations for each comparison of interest
   - Outputs recommended n per cell for Phase 2
   - Outputs updated cost estimate

6. **Full analysis script** (`experiments/validation/analyze.py`):
   - Reads all JSONL (pilot + full run)
   - Runs the full analysis plan (ANOVA/Kruskal-Wallis, post-hoc, CIs)
   - Outputs summary tables and statistics
   - Uses scipy.stats, no heavyweight dependencies

### Changes to Existing Code

1. **`markspace/llm.py`**:
   - Add per-call `temperature` override to `LLMClient.chat()` (falls back to `LLMConfig.temperature`)
   - Extract and return `usage` from API response alongside the response dict

2. **`markspace/models.py`**: No changes. Registry already has all 4 models.

3. **Discovery experiments**: Unchanged. They remain as-is for reference.

## Execution Order

1. **Phase 1 (pilot)**: 480 trials, ~$5. Run all 48 cells with 10 trials each. Start with `gpt-oss-120b` (cheapest) to validate the runner. Run model-by-model so partial results are usable if interrupted.

2. **Analyze pilot**: Run `analyze_pilot.py`. Outputs:
   - Which models work (completion rate gate)
   - Per-cell variance classification (deterministic vs stochastic)
   - Required n per cell for Phase 2
   - Updated cost estimate for Phase 2
   - Decision on whether Phase 3 is needed (if pilot + Phase 2 total > 3,000 trials with 0 failures, skip Phase 3)

3. **Phase 2 (full run)**: Top up stochastic cells to powered n. Add Phase 2b concurrency cells. Pilot data is reused (not discarded).

4. **Phase 3 (safety bound)**: Only if total trials from Phases 1+2 < 3,000 or if any failures were observed. Cheapest model, unattended batch run.

## Sources

- [Fireworks AI Pricing](https://fireworks.ai/pricing)
- [Fireworks Tool Calling Docs](https://docs.fireworks.ai/guides/function-calling)
- [Kimi K2.5 on Fireworks](https://fireworks.ai/blog/kimi-k2p5)
- [DeepSeek V3 Function Calling on Fireworks](https://fireworks.ai/blog/function-calling-deepseekv3)
- [GPT-OSS on Fireworks](https://fireworks.ai/blog/gpt-oss-on-fireworks-ai)
- [GLM-5 on Fireworks](https://fireworks.ai/models/fireworks/glm-5)
