#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Experiment orchestrator for multi-trial, adversarial, scaling, and defense experiments.

Invokes experiments/stress_test/run.py for each configuration, streaming
output in real time. Supports --resume to skip completed runs and --dry-run
to preview commands.

Usage:
    python experiments/trials/orchestrate.py --experiment multi_trial --models gpt-oss-120b mercury-2
    python experiments/trials/orchestrate.py --experiment adversarial --models gpt-oss-120b mercury-2
    python experiments/trials/orchestrate.py --experiment scaling --models gpt-oss-120b mercury-2
    python experiments/trials/orchestrate.py --experiment defense --models gpt-oss-120b
    python experiments/trials/orchestrate.py --experiment all --models gpt-oss-120b mercury-2
    python experiments/trials/orchestrate.py --experiment defense --dry-run
"""

from __future__ import annotations

import argparse
import json
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

RUNNER = Path(__file__).resolve().parent.parent / "stress_test" / "run.py"
RESULTS_DIR = Path(__file__).resolve().parent / "results"

SEEDS = [42, 43, 44, 45, 46]
DEFAULT_ROUNDS = 20
DEFAULT_AGENTS_PER_DEPT = 20
ADVERSARIAL_AGENTS = 5

# Scaling: (agents_per_dept, total_agents)
SCALING_POINTS = [
    (2, 10),
    (10, 50),
    (20, 100),
    (40, 200),
    (100, 500),
    (200, 1000),
]

# Per-model rate limits and concurrency caps
MODEL_DEFAULTS: dict[str, dict[str, int | float]] = {
    "gpt-oss-120b": {"rps": 30, "max_concurrent": 20, "max_concurrent_large": 30},
    "mercury-2": {"rps": 8, "max_concurrent": 10, "max_concurrent_large": 15},
}

# Pricing ($ per million tokens) for cost estimates
MODEL_PRICING: dict[str, tuple[float, float]] = {
    "gpt-oss-120b": (0.15, 0.60),
    "mercury-2": (0.25, 0.75),
}


# ---------------------------------------------------------------------------
# Run definitions
# ---------------------------------------------------------------------------


@dataclass
class RunConfig:
    """A single experiment run configuration."""

    experiment: str
    model: str
    variant: str
    output_dir: Path
    agents_per_dept: int
    rounds: int
    seed: int
    adversarial: int = 0
    adversarial_mode: str = "confidence"
    max_concurrent: int = 20
    rps: float = 0
    est_llm_calls: int = 0
    scale_resources: bool = False
    defense_enabled: bool = False
    probe_interval: int = 5


def _estimate_calls(n_agents: int, rounds: int) -> int:
    """Rough estimate of LLM calls: agents x rounds x ~3.5 calls/round."""
    return int(n_agents * rounds * 3.5)


def build_multi_trial_runs(models: list[str]) -> list[RunConfig]:
    runs: list[RunConfig] = []
    for model in models:
        defaults = MODEL_DEFAULTS.get(model, {"rps": 0, "max_concurrent": 20})
        for seed in SEEDS:
            n = DEFAULT_AGENTS_PER_DEPT * 5
            runs.append(
                RunConfig(
                    experiment="multi_trial",
                    model=model,
                    variant=f"seed_{seed}",
                    output_dir=RESULTS_DIR / "multi_trial" / model / f"seed_{seed}",
                    agents_per_dept=DEFAULT_AGENTS_PER_DEPT,
                    rounds=DEFAULT_ROUNDS,
                    seed=seed,
                    max_concurrent=int(defaults["max_concurrent"]),
                    rps=float(defaults["rps"]),
                    est_llm_calls=_estimate_calls(n, DEFAULT_ROUNDS),
                )
            )
    return runs


def build_adversarial_runs(models: list[str]) -> list[RunConfig]:
    runs: list[RunConfig] = []
    for model in models:
        defaults = MODEL_DEFAULTS.get(model, {"rps": 0, "max_concurrent": 20})
        for mode in ("confidence", "flood", "injection"):
            n = DEFAULT_AGENTS_PER_DEPT * 5 + ADVERSARIAL_AGENTS
            runs.append(
                RunConfig(
                    experiment="adversarial",
                    model=model,
                    variant=mode,
                    output_dir=RESULTS_DIR / "adversarial" / model / mode,
                    agents_per_dept=DEFAULT_AGENTS_PER_DEPT,
                    rounds=DEFAULT_ROUNDS,
                    seed=42,
                    adversarial=ADVERSARIAL_AGENTS,
                    adversarial_mode=mode,
                    max_concurrent=int(defaults["max_concurrent"]),
                    rps=float(defaults["rps"]),
                    est_llm_calls=_estimate_calls(n, DEFAULT_ROUNDS),
                )
            )
    return runs


def build_scaling_runs(models: list[str]) -> list[RunConfig]:
    runs: list[RunConfig] = []
    for model in models:
        defaults = MODEL_DEFAULTS.get(model, {"rps": 0, "max_concurrent": 20})
        for agents_per_dept, total in SCALING_POINTS:
            mc_key = "max_concurrent_large" if total >= 200 else "max_concurrent"
            mc = int(defaults.get(mc_key, defaults["max_concurrent"]))
            runs.append(
                RunConfig(
                    experiment="scaling",
                    model=model,
                    variant=f"n_{total}",
                    output_dir=RESULTS_DIR / "scaling" / model / f"n_{total}",
                    agents_per_dept=agents_per_dept,
                    rounds=DEFAULT_ROUNDS,
                    seed=42,
                    max_concurrent=mc,
                    rps=float(defaults["rps"]),
                    est_llm_calls=_estimate_calls(total, DEFAULT_ROUNDS),
                )
            )
    return runs


# Proportional scaling: resources + adversarial agents grow with agent count.
# Baseline is n_100 (20 agents/dept, 5 adversarial, default resources).
# At each scale point, adversarial count = round(5 * scale_factor).
PROPORTIONAL_SCALING_POINTS = [
    (20, 100),
    (100, 500),
    (200, 1000),
]


def build_scaling_proportional_runs(models: list[str]) -> list[RunConfig]:
    runs: list[RunConfig] = []
    for model in models:
        defaults = MODEL_DEFAULTS.get(model, {"rps": 0, "max_concurrent": 20})
        for agents_per_dept, total in PROPORTIONAL_SCALING_POINTS:
            mc_key = "max_concurrent_large" if total >= 200 else "max_concurrent"
            mc = int(defaults.get(mc_key, defaults["max_concurrent"]))
            scale = agents_per_dept / DEFAULT_AGENTS_PER_DEPT
            n_adversarial = round(ADVERSARIAL_AGENTS * scale)
            runs.append(
                RunConfig(
                    experiment="scaling_proportional",
                    model=model,
                    variant=f"n_{total}",
                    output_dir=RESULTS_DIR
                    / "scaling_proportional"
                    / model
                    / f"n_{total}",
                    agents_per_dept=agents_per_dept,
                    rounds=DEFAULT_ROUNDS,
                    seed=42,
                    adversarial=n_adversarial,
                    adversarial_mode="confidence",
                    max_concurrent=mc,
                    rps=float(defaults["rps"]),
                    est_llm_calls=_estimate_calls(
                        total + n_adversarial, DEFAULT_ROUNDS
                    ),
                    scale_resources=total > 100,  # n_100 uses default resources
                )
            )
    return runs


DEFENSE_SCENARIOS: dict[str, dict[str, str | int]] = {
    "rate_spike": {
        "adversarial_mode": "rate_spike",
        "rounds": 20,
        "adversarial": 5,
    },
    "type_shift": {
        "adversarial_mode": "type_shift",
        "rounds": 20,
        "adversarial": 5,
    },
    "escalation": {
        "adversarial_mode": "escalation",
        "rounds": 20,
        "adversarial": 5,
    },
    "probe_evasion": {
        "adversarial_mode": "probe_evasion",
        "rounds": 20,
        "adversarial": 5,
        "probe_interval": 5,
    },
    "slow_drift": {
        "adversarial_mode": "slow_drift",
        "rounds": 40,
        "adversarial": 5,
    },
    "defense_combined": {
        "adversarial_mode": "defense_combined",
        "rounds": 30,
        "adversarial": 5,
    },
}


def build_defense_runs(models: list[str]) -> list[RunConfig]:
    runs: list[RunConfig] = []
    for model in models:
        defaults = MODEL_DEFAULTS.get(model, {"rps": 0, "max_concurrent": 20})
        for scenario_name, cfg in DEFENSE_SCENARIOS.items():
            n_rounds = int(cfg.get("rounds", DEFAULT_ROUNDS))
            n_adv = int(cfg.get("adversarial", ADVERSARIAL_AGENTS))
            n = DEFAULT_AGENTS_PER_DEPT * 5 + n_adv
            runs.append(
                RunConfig(
                    experiment="defense",
                    model=model,
                    variant=scenario_name,
                    output_dir=RESULTS_DIR / "defense" / model / scenario_name,
                    agents_per_dept=DEFAULT_AGENTS_PER_DEPT,
                    rounds=n_rounds,
                    seed=42,
                    adversarial=n_adv,
                    adversarial_mode=str(cfg["adversarial_mode"]),
                    max_concurrent=int(defaults["max_concurrent"]),
                    rps=float(defaults["rps"]),
                    est_llm_calls=_estimate_calls(n, n_rounds),
                    defense_enabled=True,
                    probe_interval=int(cfg.get("probe_interval", 5)),
                )
            )
    return runs


def build_runs(experiment: str, models: list[str]) -> list[RunConfig]:
    builders = {
        "multi_trial": build_multi_trial_runs,
        "adversarial": build_adversarial_runs,
        "scaling": build_scaling_runs,
        "scaling_proportional": build_scaling_proportional_runs,
        "defense": build_defense_runs,
    }
    if experiment == "all":
        runs: list[RunConfig] = []
        for builder in builders.values():
            runs.extend(builder(models))
        return runs
    return builders[experiment](models)


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def is_completed(run: RunConfig) -> bool:
    """Check if a run already completed successfully."""
    trial_file = run.output_dir / "trial.jsonl"
    if not trial_file.exists():
        return False
    try:
        with open(trial_file) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                if record.get("seed") == run.seed and not record.get("error"):
                    return True
    except (json.JSONDecodeError, KeyError):
        pass
    return False


def build_command(run: RunConfig) -> list[str]:
    """Build the run.py command for a run configuration."""
    cmd = [
        sys.executable,
        str(RUNNER),
        "--agents-per-dept",
        str(run.agents_per_dept),
        "--rounds",
        str(run.rounds),
        "--seed",
        str(run.seed),
        "--max-concurrent",
        str(run.max_concurrent),
        "--model",
        run.model,
        "--phase",
        f"{run.experiment}_{run.variant}",
        "--output-dir",
        str(run.output_dir),
    ]
    if run.adversarial > 0:
        cmd.extend(["--adversarial", str(run.adversarial)])
        cmd.extend(["--adversarial-mode", run.adversarial_mode])
    if run.rps > 0:
        cmd.extend(["--requests-per-second", str(run.rps)])
    if run.scale_resources:
        cmd.append("--scale-resources")
    if run.defense_enabled:
        cmd.append("--defense")
        cmd.extend(["--probe-interval", str(run.probe_interval)])
    return cmd


def estimate_cost(run: RunConfig) -> float:
    """Estimate cost in USD for a run."""
    pricing = MODEL_PRICING.get(run.model, (0.50, 2.00))
    # ~1500 prompt tokens + ~200 completion tokens per call
    prompt_tokens = run.est_llm_calls * 1500
    completion_tokens = run.est_llm_calls * 200
    return (
        prompt_tokens / 1_000_000 * pricing[0]
        + completion_tokens / 1_000_000 * pricing[1]
    )


def execute_run(run: RunConfig, dry_run: bool = False) -> dict | None:
    """Execute a single run, streaming output. Returns summary or None."""
    cmd = build_command(run)
    cost = estimate_cost(run)

    total_agents = run.agents_per_dept * 5 + run.adversarial
    print(f"\n{'=' * 70}")
    print(
        f"  Experiment: {run.experiment} | Model: {run.model} | Variant: {run.variant}"
    )
    print(f"  Agents: {total_agents} | Rounds: {run.rounds} | Seed: {run.seed}")
    print(f"  Est. LLM calls: ~{run.est_llm_calls:,} | Est. cost: ${cost:.2f}")
    print(f"  Rate limit: {run.rps} req/s | Max concurrent: {run.max_concurrent}")
    print(f"  Output: {run.output_dir}")
    if dry_run:
        print(f"  Command: {' '.join(cmd)}")
        print("  [DRY RUN - skipping]")
        return None

    print(f"{'=' * 70}")

    run.output_dir.mkdir(parents=True, exist_ok=True)
    log_path = run.output_dir / "run.log"
    t_start = time.monotonic()

    proc: subprocess.Popen[str] | None = None
    try:
        # Write child stdout to a log file so the child process is not
        # affected if the orchestrator's own stdout pipe closes (e.g.
        # when run as a background subprocess with limited output capture).
        with open(log_path, "w") as log_f:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                log_f.write(line)
                log_f.flush()
                try:
                    sys.stdout.write(f"  | {line}")
                    sys.stdout.flush()
                except BrokenPipeError:
                    pass  # orchestrator stdout closed; child keeps running
            proc.wait()
        elapsed = time.monotonic() - t_start

        if proc.returncode != 0:
            print(f"\n  FAILED (exit code {proc.returncode}) after {elapsed:.1f}s")
            print(f"  Log: {log_path}")
            return {"status": "failed", "elapsed": elapsed}

        print(f"\n  Completed in {elapsed:.1f}s (est. cost: ${cost:.2f})")
        return {"status": "ok", "elapsed": elapsed, "est_cost": cost}

    except KeyboardInterrupt:
        print("\n  Interrupted by user")
        if proc is not None and proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)
        return {"status": "interrupted", "elapsed": time.monotonic() - t_start}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Experiment orchestrator for multi-trial, adversarial, and scaling runs"
    )
    parser.add_argument(
        "--experiment",
        choices=[
            "multi_trial",
            "adversarial",
            "scaling",
            "scaling_proportional",
            "defense",
            "all",
        ],
        required=True,
    )
    parser.add_argument(
        "--models",
        nargs="+",
        default=["gpt-oss-120b", "mercury-2"],
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print commands without executing"
    )
    parser.add_argument(
        "--resume", action="store_true", help="Skip already-completed runs"
    )
    args = parser.parse_args()

    runs = build_runs(args.experiment, args.models)

    # Summary
    total_calls = sum(r.est_llm_calls for r in runs)
    total_cost = sum(estimate_cost(r) for r in runs)
    print(f"Orchestrator: {len(runs)} runs planned")
    print(f"  Total est. LLM calls: ~{total_calls:,}")
    print(f"  Total est. cost: ${total_cost:.2f}")

    if args.resume:
        skipped = [r for r in runs if is_completed(r)]
        runs = [r for r in runs if not is_completed(r)]
        if skipped:
            print(
                f"  Resuming: {len(skipped)} already completed, {len(runs)} remaining"
            )

    if not runs:
        print("  Nothing to do.")
        return

    results: list[dict] = []
    interrupted = False

    for i, run in enumerate(runs):
        print(f"\n[{i + 1}/{len(runs)}]", end="")
        try:
            result = execute_run(run, dry_run=args.dry_run)
            if result:
                result["run"] = f"{run.experiment}/{run.model}/{run.variant}"
                results.append(result)
                if result["status"] == "interrupted":
                    interrupted = True
                    break
        except KeyboardInterrupt:
            print("\n\nOrchestrator interrupted.")
            interrupted = True
            break

    # Final summary
    print(f"\n\n{'=' * 70}")
    print("SUMMARY")
    print(f"{'=' * 70}")
    ok = sum(1 for r in results if r.get("status") == "ok")
    failed = sum(1 for r in results if r.get("status") == "failed")
    total_elapsed = sum(r.get("elapsed", 0) for r in results)
    total_spent = sum(r.get("est_cost", 0) for r in results if r.get("status") == "ok")

    print(f"  Completed: {ok} | Failed: {failed} | Total time: {total_elapsed:.0f}s")
    print(f"  Est. cost: ${total_spent:.2f}")
    if interrupted:
        remaining = len(runs) - len(results)
        print(
            f"  Interrupted with {remaining} runs remaining. Use --resume to continue."
        )

    for r in results:
        status = r.get("status", "?")
        symbol = {"ok": "+", "failed": "X", "interrupted": "!"}
        print(f"  [{symbol.get(status, '?')}] {r['run']} - {r.get('elapsed', 0):.0f}s")


if __name__ == "__main__":
    # Prevent BrokenPipeError from crashing the orchestrator when
    # stdout is a pipe that gets closed (e.g. background subprocess).
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    try:
        main()
    except BrokenPipeError:
        # stdout closed; child processes are unaffected (output goes to run.log)
        sys.exit(0)
