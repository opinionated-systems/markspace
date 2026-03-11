# Validation Extension: Multi-Phase (Long-Running) Coordination

## Context

The validation experiments (1,439 trials) tested safety and efficiency across model, agent count, visibility, temperature, and execution mode. Every trial is a single-phase instant task: agents get one conversation, book one slot, done. The clock is frozen so intent TTL and observation decay never activate. This means the validation says nothing about coordination across time: agents resuming after context loss, navigating partially-filled state they didn't create, or operating with expired intents in the mark space.

## Goal

Add `n_rounds` as a new factor in the existing validation runner. A multi-round trial runs the same agents through multiple booking rounds on the same shared calendar, with clock advancement and fresh LLM conversations between rounds. This exercises mark decay, state reconstruction from marks, and cross-round safety.

## Design

### New factor: `n_rounds`

Added to `Cell` dataclass. Values: `1` (current behavior, backward-compatible) and `2`.

- **Round 1**: Standard booking. N agents each book 1 slot. Clock = T (1,000,000.0).
- Clock advances to T + 3600 (1 hour). Intent TTL is 30 min, so Round 1 intents expire. Action marks (permanent) survive.
- **Round 2**: Fresh LLM conversation per agent. Same `CalendarEnv` (marks persist). Each agent books a second slot. Calendar already has N slots taken from Round 1. Agents must discover this from marks alone.

### What changes mechanically

1. **Intent decay**: Round 1 intents have `compute_strength == 0` after clock advance (TTL expired). The guard's `get_intents()` filters them out. New Round 2 intents won't collide with stale Round 1 intents.

2. **Action persistence**: Round 1 action marks have `compute_strength == 1.0` (actions don't decay). `view_calendar` reads action marks, so Round 2 agents see Round 1 bookings as "BOOKED by agent-01" etc.

3. **Agent identity**: Each round reuses the same `Agent` objects (same name and UUID). The guard recognizes the agent's own prior action marks. `view_calendar` shows "BOOKED by agent-01" and the agent knows that's itself. This models a real system where agents have persistent identities across phases. The conversation context resets (fresh LLM messages), but the agent identity persists.

4. **Self-re-booking**: The guard allows an agent to re-book its own slot (it filters `agent_id != agent.id` when checking existing action marks). In the visible-calendar condition, agents see what they already booked, and the prompt instructs them to book an *additional* slot. In the hidden-calendar condition, agents have no such signal, so if they try their preferred slot again, the guard won't block it. This is an intentional asymmetry that the experiment measures.

### Prompts

Round 1 prompts are unchanged (the `round_num=1` default produces identical output).

**Round 2 visible prompt** adds:
> "This is round 2 of a multi-round booking. Some slots are already booked from a previous round (you may have booked one yourself). You need to book ONE additional slot."

**Round 2 hidden prompt** adds:
> "This is round 2. Some slots are already booked. If you get CONFLICT, try a different slot."

### What this tests

1. **Cross-round safety**: Double bookings must be zero across both rounds combined. A double booking occurs when the same slot appears in `all_round_booked` more than once, whether from two different agents or from the same agent re-booking its own slot.

2. **State reconstruction**: Do agents correctly interpret a partially-filled calendar? Do visible agents avoid re-booking their own Round 1 slot?

3. **Efficiency penalty**: How many extra steps does Round 2 take vs Round 1, given the calendar is partially filled?

4. **Intent decay correctness**: The guard must not be confused by expired Round 1 intents. New Round 2 intents should be processed cleanly.

5. **Self-re-booking under hidden calendar**: Agents in the hidden-calendar condition cannot see the calendar. If they try their preferred slot (which they may have booked in Round 1), the guard allows it. This measures whether the hidden-calendar condition produces more cross-round double bookings than the visible-calendar condition.

### DVs (recorded per trial)

Existing top-level fields stay unchanged. New fields in `TrialResult`:

- `n_rounds`: int, how many rounds were run
- `rounds`: list of per-round dicts, each containing:
  - `round_id`: int
  - `steps_per_agent`: float
  - `all_completed`: bool
  - `wasted_attempts`: int
  - `guard_invocations`: int (delta from previous round)
  - `tokens`: {prompt, completion}
  - `double_bookings`: int (within this round only)
  - `agents`: list of AgentRecord dicts

Top-level `steps_per_agent` is now total steps divided by `n_agents * n_rounds` (average per agent-round). Top-level `all_completed` is AND across all rounds. Top-level `double_bookings` counts across ALL rounds combined (a slot booked in R1 and again in R2 counts as 1 double booking). This preserves backward compatibility: single-round trials produce identical output to existing data, and `rounds` is an empty list.

### Cell key

Extends to include `n_rounds` as 7th pipe-separated field:
```
"{model}|{n_agents}|{visibility}|{temperature}|{execution_mode}|{conflict_policy}|{n_rounds}"
```

For `n_rounds=1`, old records without the field in JSONL are handled by `load_completed` using `.get("n_rounds", 1)`.

## Factorial for this run

| Factor | Levels |
|--------|--------|
| Model | gpt-oss-120b, mercury-2 |
| N agents | 3, 5, 10 |
| Visibility | visible, hidden |
| Temperature | 0.0, 0.7 |
| N rounds | 2 |
| Execution mode | sequential |

2 x 3 x 2 x 2 x 1 x 1 = **24 cells**, 10 trials/cell = **240 trials**

Sequential only for the pilot. Concurrent can be added in a follow-up if results warrant.

Single-round baseline already exists in the mercury-2 and gpt-oss-120b results files. No need to rerun `n_rounds=1`.

## Implementation

### Files modified

#### `experiments/validation/run.py`

1. **`Cell` dataclass**: Added `n_rounds: int = 1`. Updated `key()` to include it.
2. **`TrialResult` dataclass**: Added `n_rounds: int = 1` and `rounds: list[dict]`.
3. **Prompts**: `make_visible_prompt` and `make_hidden_prompt` accept `round_num: int = 1` parameter. Round 2+ prompts include multi-round context.
4. **Trial runner refactored** into three functions:
   - `_run_round()`: runs one round for all agents (sequential or concurrent)
   - `_compute_round_metrics()`: computes per-round stats
   - `run_trial()`: creates agents once (persistent identity), loops rounds with clock advance
5. **`load_completed`**: Handles old records missing `n_rounds` (defaults to 1).
6. **CLI**: Added `--n-rounds` (nargs="+", type=int, default=[1]).

#### `experiments/validation/analyze_pilot.py`

7. **Cell key**: 7th pipe-separated field (`n_rounds`).
8. **Cell display**: ` R=N` tag when n_rounds > 1.
9. **New Section 7**: Cross-round analysis table: R1 vs R2 steps/agent, completion rate, double bookings per cell. Overall penalty calculation.

#### `experiments/validation/analyze.py`

10. **Data loading**: Extracts `n_rounds` and per-round metrics (r1/r2 steps, completion, double bookings) from JSONL.
11. **ANOVA**: Includes `n_rounds` as factor when multiple levels present.
12. **Condition table**: Groups by `n_rounds`. Added multi-phase per-round breakdown (R1/R2 side-by-side).
13. **Two new figures**: `fig_round_comparison` (R1 vs R2 bar chart by model/N) and `fig_round_comparison_by_visibility` (R2 penalty by visibility).

## Run command

```bash
python experiments/validation/run.py \
    --models gpt-oss-120b mercury-2 \
    --agents 3 5 10 \
    --visibility visible hidden \
    --temperature 0.0 0.7 \
    --n-rounds 2 \
    --execution-mode sequential \
    --phase multi_phase \
    --trials-per-cell 10 \
    --parallel-cells 12
```

## Verification

### Single-trial smoke test

```bash
python experiments/validation/run.py \
    --models gpt-oss-120b \
    --agents 3 \
    --visibility visible \
    --temperature 0.0 \
    --n-rounds 2 \
    --trials-per-cell 1 \
    --phase test
```

Confirm in JSONL output:
- `n_rounds: 2` and `rounds` array with 2 entries
- Round 1 has ~2 steps/agent (visible baseline)
- Round 2 has >= 2 steps/agent (partially filled calendar)
- `double_bookings: 0` across both rounds
- Top-level `steps_per_agent` is aggregate across both rounds

### Backward compatibility test

```bash
python experiments/validation/run.py \
    --models gpt-oss-120b \
    --agents 3 \
    --visibility visible \
    --temperature 0.0 \
    --n-rounds 1 \
    --trials-per-cell 1 \
    --phase test
```

Confirm output matches existing format (n_rounds=1, empty rounds list).

### Analysis compatibility

```bash
python experiments/validation/analyze_pilot.py results_pilot_*.jsonl results_multi_phase_*.jsonl
```

Confirm: no crashes, old cells parsed correctly, new multi-round cells appear in cross-round analysis section.

### Full run

240 trials. Check:
- Zero double bookings across all trials
- Round 2 efficiency penalty (R2 steps/agent > R1 steps/agent)
- Visible R2 penalty < hidden R2 penalty (visible agents can see existing bookings)
- Intent decay working: no guard confusion from stale Round 1 intents

## Predictions

1. **Visible, sequential**: Round 2 adds ~0 extra steps/agent vs Round 1. Agents view the calendar, see N slots taken, pick a free one. Same as Round 1 but with fewer available slots.

2. **Hidden, sequential**: Round 2 adds ~N/15 extra steps/agent vs Round 1. More slots are taken, so blind attempts hit CONFLICT more often. The penalty scales with how full the calendar is.

3. **Self-re-booking (hidden)**: Agents in the hidden condition may re-book their own Round 1 slot because the guard doesn't block self-re-booking and they can't see the calendar. The preferred slot (wed-14) was booked by agent-01 in Round 1. In Round 2, agent-01 tries wed-14 again, succeeds (guard allows self), and it shows up as a double booking. Visible agents should avoid this because the calendar shows "BOOKED by agent-01".

4. **Safety**: Zero double bookings in visible condition. Possible double bookings in hidden condition from self-re-booking (prediction 3). This would be the first condition where double bookings are observed, and it's an inherent limitation of the hidden condition combined with persistent agent identity.

5. **Intent decay**: No effect on outcomes. Round 1 intents are consumed by the guard during Round 1 (superseded by action marks). Even if they weren't, the 1-hour clock advance expires them cleanly. This is a correctness verification, not a functional test.

## Limitations

1. **Only 2 rounds**: The plan tests 2 rounds, not N rounds. Longer horizons (5-10 rounds) would stress mark accumulation and calendar exhaustion (15 slots, 10 agents x 5 rounds = 50 bookings needed for 15 slots). This is a follow-up.

2. **Sequential only**: Concurrent multi-round introduces additional complexity (concurrent agents in Round 2 may all read the same partially-filled state). Worth testing but deferred to avoid confounding.

3. **Same preferred slot**: All agents still prefer wed-14. In Round 2, this slot is already taken. The prompt doesn't update the preference. A more realistic design would assign different preferences per round, but this adds a confound.

4. **No explicit "your bookings" context**: Round 2 agents get a fresh conversation. They're told "you may have booked one yourself" but don't receive an explicit list of their Round 1 bookings. Agents in the visible-calendar condition can discover this via `view_calendar`. Agents in the hidden-calendar condition cannot. A production system might pass prior-booking context explicitly.
