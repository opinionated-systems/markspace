# Running Experiments

## Setup

Install the package with experiment dependencies:

```bash
poetry install --extras experiments  # adds matplotlib, numpy
```

Copy `.env.example` to `.env` and add API keys for the models you want to use. The LLM-based experiments (stress test, trials, validation) require at least one provider configured.

```bash
cp .env.example .env
```

Unit tests (no API keys needed):

```bash
pytest
```

## No-LLM Experiments

These run locally without API keys or network access.

### Unit Tests

```bash
pytest  # 774 tests (tests/, experiments/stress_test/, experiments/envelope_sim/ and experiments/trials/)
```

Covers mark algebra, decay, trust weighting, conflict resolution (also across scope trees), guard enforcement and scope visibility, composition properties, scheduling, thread safety, hypothesis property-based tests, the LLM client, the stress-test pacer, and the stress test's safety metrics.

### Composition Stress Test

Deterministic pipeline validation. 14 agents in a 5-stage pipeline with 4 mid-run hot-swaps - simultaneous cross-stage swaps, mid-processing swaps, and permission changes on non-leaf agents.

```bash
python -m experiments.composition_stress.run
```

No arguments. Results print to stdout. Analysis in [composition_stress/analysis.md](composition_stress/analysis.md).

### Envelope Simulator

False and missed RESTRICTED verdicts of the statistical envelope (spec 9.7, a monitor) on synthetic workloads, and a replay of the gpt-oss-120b defense trials of commit 0bf9a05. Pure Python. The full run (400 seeds of 15,000 windows per profile) took 445 s on 48 processes; `--quick` (20 seeds of 2,000 windows) takes about 10 s.

```bash
python -m experiments.envelope_sim.measure --out experiments/envelope_sim/results
python -m experiments.envelope_sim.measure --out experiments/envelope_sim/results --quick
python -m experiments.envelope_sim.measure --out experiments/envelope_sim/results --detector write_budget
```

Writes `d0.json` (the envelope's default detector) or `write_budget.json` (the write budgets of spec 9.14, with the settings it measured); `--quick` writes `*_quick.json`. `python -m experiments.envelope_sim.report experiments/envelope_sim/results/d0.json experiments/envelope_sim/analysis.md experiments/envelope_sim/results/write_budget.json` writes the analysis from them: [envelope_sim/analysis.md](envelope_sim/analysis.md).

## LLM Experiments

These call model APIs and require keys in `.env`.

### Stress Test

Full 105-agent office coordination scenario. 100 employees across 5 departments coordinate meetings, tasks, equipment, parking, and lunch through the shared mark space. Two external system bots publish capacity observations and room warnings. Scenario design in [stress_test/design.md](stress_test/design.md).

```bash
# Smoke test (10 agents, 3 rounds)
python experiments/stress_test/run.py --agents-per-dept 2 --rounds 3

# Full run (100 agents, 20 rounds)
python experiments/stress_test/run.py --agents-per-dept 20 --rounds 20 \
    --seed 42 --max-concurrent 20

# With adversarial agents
python experiments/stress_test/run.py --agents-per-dept 20 --rounds 20 \
    --adversarial 5 --adversarial-mode confidence

# With the defenses (bot write budgets, barrier, probe) and the envelope as a monitor
python experiments/stress_test/run.py --agents-per-dept 20 --rounds 20 \
    --adversarial 5 --adversarial-mode rate_spike --defense --probe-interval 5

# Custom model
python experiments/stress_test/run.py --agents-per-dept 20 --rounds 10 \
    --model mercury-2 --requests-per-second 8

# Paced run on a provider with adaptive rate limits (Fireworks), with a
# 2048-token thinking budget; target 0.3 leaves the rest of the limit to
# other workloads on the same account and model (see Pacing below)
python experiments/stress_test/run.py --agents-per-dept 20 --rounds 10 \
    --seed 42 --adversarial 5 --max-concurrent 20 \
    --model accounts/fireworks/models/deepseek-v4p1-flash-1017 \
    --reasoning-effort 2048 --pace-start-rps 0.25 --pace-target-fraction 0.3

# Resume interrupted run
python experiments/stress_test/run.py --resume --output results_stress_v1_20260228.jsonl
```

| Argument | Default | Description |
|----------|---------|-------------|
| `--agents-per-dept` | 20 | Agents per department (5 departments) |
| `--rounds` | 10 | Simulation rounds (2 per simulated day) |
| `--seed` | 42 | Random seed |
| `--model` | `gpt-oss-120b` | Model short name |
| `--max-concurrent` | 20 | Max concurrent agent executions |
| `--requests-per-second` | 0 | Fixed rate limit (0 = unlimited); not together with `--pace-start-rps` |
| `--reasoning-effort` | none | Sent as `reasoning_effort`: a level name, or an integer token budget. Fireworks' chat-completions reference calls an integer of 1024 or more a hard thinking budget on DeepSeek V4.1; in a run at 2048, 18 of 2,555 calls went over it by 12 or 13 tokens |
| `--pace-start-rps` | none | Turn on pacing ([`pacing.py`](stress_test/pacing.py)), starting at this many requests per second |
| `--pace-step-rps` | 0.25 | Rate added per step |
| `--pace-step-seconds` | 60 | Length of a step window |
| `--pace-max-rps` | 5.0 | Highest rate |
| `--pace-min-rps` | 0.05 | Lowest rate after cuts |
| `--pace-target-fraction` | 0.6 | Stop stepping once this run's generated tokens in the last minute reach this share of the provider's reported limit |
| `--pace-cooldown-seconds` | 300 | No step for this long after a rate-limit signal |
| `--output-dir` | auto | Output directory; a relative path is taken from `experiments/stress_test/` |
| `--adversarial` | 0 | Number of adversarial agents |
| `--adversarial-mode` | `confidence` | `confidence`, `flood`, `injection`, `rate_spike`, `type_shift`, `escalation`, `probe_evasion`, `slow_drift`, `low_rate_drift` |
| `--scale-resources` | false | Scale resources proportionally with agents |
| `--defense` | false | Enable the defenses (bot write budgets, barrier, probe) and the envelope as a monitor |
| `--probe-interval` | 5 | Run diagnostic probe every N rounds (requires `--defense`) |
| `--resume` | false | Resume from existing output |

Output goes to a results directory containing decomposed JSONL files: `trial.jsonl` (aggregates), `rounds.jsonl` (per-round), `agents.jsonl` (per-agent-per-round), `steps.jsonl` (per-tool-call), `manifests.jsonl` (agent manifests), and optionally `messages.jsonl` (full LLM history, with each call's raw `usage` block). Token totals count `prompt`, `completion`, `cached_prompt` and `reasoning` tokens and the number of `calls`; `calls_cached_unknown` counts responses that reported no cached-token count. A paced run also writes `pacer.jsonl`, one line per rate change. `python experiments/stress_test/stored_run_safety.py <run dir>...` checks stored runs from their `steps.jsonl` and bot logs: double bookings as `run.py` counts them (resources allowed twice in one round, rebookings no building-ops warning explains, lunch windows over capacity), the verdicts of cross-department writes, and what the parking and boardroom bots gave out. Runs made before `run.py` measured double bookings and scope violations report 0 for both, so use this for them.

**Pacing.** Providers such as Fireworks set limits in tokens per minute, per account and per model, and raise a limit only while usage stays above half of it; traffic that ramps up too quickly gets HTTP 429. With `--pace-start-rps`, every request, retries included, waits in one arrival-ordered line, so concurrent agents never fire together. The rate starts low and steps up only while requests succeed and agents use about the whole current rate. A 429 or an over-limit signal halves it, and no request starts before a 429's Retry-After has passed. Other workloads on the same account and model share its limits without being counted here: keep the sum of every workload's share at or below the limit, for example `--pace-target-fraction 0.3` next to a workload that uses 70% of it.

### Trial Experiments

The orchestrator runs batches of stress tests across models and configurations. It invokes `stress_test/run.py` under the hood with constructed arguments.

```bash
# Multi-trial repeatability (5 seeds x 2 models)
python experiments/trials/orchestrate.py --experiment multi_trial

# Adversarial robustness (3 modes x 2 models)
python experiments/trials/orchestrate.py --experiment adversarial

# Scaling with fixed resources (contention increases)
python experiments/trials/orchestrate.py --experiment scaling

# Scaling with proportional resources
python experiments/trials/orchestrate.py --experiment scaling_proportional

# Defenses (write budgets for the bots, barrier, probe; the envelope as a monitor)
# run.py appends to existing JSONL files: move earlier runs away first, or pass --resume to skip completed runs
python experiments/trials/orchestrate.py --experiment defense --models glm-5p3-flash

# All experiments
python experiments/trials/orchestrate.py --experiment all

# Dry run (preview commands without executing)
python experiments/trials/orchestrate.py --experiment defense --dry-run

# Specific models only
python experiments/trials/orchestrate.py --experiment multi_trial --models gpt-oss-120b

# Resume interrupted batch
python experiments/trials/orchestrate.py --experiment multi_trial --resume
```

| Argument | Default | Description |
|----------|---------|-------------|
| `--experiment` | required | `multi_trial`, `adversarial`, `scaling`, `scaling_proportional`, `defense`, or `all` |
| `--models` | `gpt-oss-120b mercury-2` | Space-separated model names |
| `--dry-run` | false | Print commands without executing |
| `--resume` | false | Skip already-completed runs |

Results land in `experiments/trials/results/{experiment}/{model}/{variant}/`.

### Validation

Confirmatory statistical validation with factorial design across models, agent counts, visibility modes, temperatures, and conflict policies.

```bash
# Pilot phase (10 trials per cell)
python experiments/validation/run.py --trials-per-cell 10 --parallel-cells 12

# Single cell for quick testing
python experiments/validation/run.py --models gpt-oss-120b --agents 3 \
    --visibility visible --temperature 0.0 --trials-per-cell 1

# Concurrency focus
python experiments/validation/run.py --execution-mode sequential concurrent \
    --conflict-policy highest_confidence first_writer \
    --trials-per-cell 50

# Multi-round extension
python experiments/validation/run.py --n-rounds 1 2 --trials-per-cell 30

# Resume
python experiments/validation/run.py --resume --output results_pilot_20260227.jsonl
```

| Argument | Default | Description |
|----------|---------|-------------|
| `--models` | `kimi-k2p5 deepseek-v3p2 glm-5 gpt-oss-120b` | Models to test |
| `--agents` | `3 5 10` | Agent counts |
| `--visibility` | `visible hidden` | Visibility modes |
| `--temperature` | `0.0 0.7` | Temperature values |
| `--trials-per-cell` | 10 | Trials per factor combination |
| `--execution-mode` | `sequential` | `sequential` and/or `concurrent` |
| `--conflict-policy` | `highest_confidence` | Policy choices |
| `--n-rounds` | `1` | Booking rounds |
| `--n-slots` | `15` | Calendar size |
| `--parallel-cells` | 12 | Concurrent cell workers |
| `--resume` | false | Resume from output file |

### Comparison Experiment

Markspace vs a message-passing framework ([Microsoft Agent Framework](https://github.com/microsoft/agent-framework)) on a shared resource booking scenario. 10 agents, 12 slots. Each arm runs independently. Design in [comparison/design.md](comparison/design.md).

```bash
# Run both arms (10 seeds each)
python experiments/comparison/run_markspace.py --seeds 42,43,44,45,46,47,48,49,50,51
python experiments/comparison/run_messagepassing.py --seeds 42,43,44,45,46,47,48,49,50,51

# Single seed
python experiments/comparison/run_markspace.py --seed 42

# Analyze results
python experiments/comparison/analyze.py
```

| Argument | Default | Description |
|----------|---------|-------------|
| `--seed` | 42 | Single seed |
| `--seeds` | none | Comma-separated seeds (overrides `--seed`) |
| `--model` | `gpt-oss-120b` | Model short name |
| `--output-dir` | `results/markspace` or `results/messagepassing` | Output directory |

Results land in `experiments/comparison/results/{arm}/`. Each seed produces a `seed_N.jsonl` (metrics) and `seed_N_messages.jsonl` (full message traces including thinking). Analysis in [comparison/analysis.md](comparison/analysis.md).

## Analysis

### Stress test analysis

```bash
python experiments/stress_test/analyze.py results_dir/
python experiments/stress_test/analyze.py results_dir/ --figures
python experiments/stress_test/analyze.py results_dir/ --agent eng-lead
python experiments/stress_test/analyze.py results_dir/ --round 0 --steps
```

### Trial analysis

```bash
python experiments/trials/analyze_trials.py --experiment multi_trial
python experiments/trials/analyze_trials.py --experiment adversarial
python experiments/trials/analyze_trials.py --experiment defense
python experiments/trials/analyze_trials.py --experiment all
```

### Validation analysis

```bash
python experiments/validation/analyze.py
```

Generates statistical tests and plots in `figures/`.

### Comparison analysis

```bash
python experiments/comparison/analyze.py
```

Prints safety violations, behavioral metrics, and per-seed results for both arms.

### Visualization

Generate animated GIFs of agent activity:

```bash
python experiments/stress_test/animate.py results_dir/
python experiments/trials/animate.py experiments/trials/results/scaling_proportional/gpt-oss-120b/n_500
```

Generate matplotlib figures:

```bash
python experiments/stress_test/plot_final.py results_dir/
```

## Rate Limits

Per-model defaults are configured in the orchestrator:

| Model | Requests/sec | Max concurrent |
|-------|-------------|----------------|
| gpt-oss-120b | 30 | 20 |
| mercury-2 | 8 | 10 |
| glm-5p3-flash | 30 | 20 |

Override with `--requests-per-second` and `--max-concurrent` on `stress_test/run.py`, or adjust `MODEL_DEFAULTS` in `orchestrate.py`.

## Cost

Model pricing per million tokens (check provider pages for current rates):

| Model | Prompt | Completion | Provider |
|-------|--------|------------|----------|
| [gpt-oss-120b](https://fireworks.ai/models/fireworks/gpt-oss-120b) | $0.15 ($0.015 cached) | $0.60 | [Fireworks](https://fireworks.ai/) (model page, read 2026-11-03) |
| [mercury-2](https://docs.inceptionlabs.ai/) | $0.25 | $0.75 | [Inception AI](https://www.inceptionlabs.ai/) |
| [glm-5p3-flash](https://fireworks.ai/models/fireworks/glm-5p3-flash) | $0.15 ($0.03 cached) | $0.50 (thinking included) | [Fireworks](https://fireworks.ai/) (model page, read 2026-11-03) |
| deepseek-v4p1-flash-1017 | $0.30 ($0.006 cached) | $1.20 (thinking included) | [Fireworks](https://fireworks.ai/) (model page, read 2026-10-30) |

Actual costs from trial data (105 agents, 20 rounds, averaged across 5 seeds):

| Scale | gpt-oss-120b | mercury-2 |
|-------|-------------|-----------|
| 105 agents (baseline) | ~$2.30 | ~$2.60 |
| 525 agents | ~$39 | - |
| 1,050 agents | ~$132 | - |

Mercury-2 uses ~80% fewer completion tokens than gpt-oss-120b but has a higher per-token prompt rate, so baseline per-run cost is similar. Scaling costs are dominated by prompt tokens - context grows with agent count.

Measured single run, DeepSeek V4.1 Flash 1017 (105 agents, 10 rounds, thinking budget 2048, paced; the prices above): $2.59, of which $0.82 for output ([stress test, section 9](stress_test/analysis.md#9-re-run-on-deepseek-v41-flash-1017)).
