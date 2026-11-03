#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Measure today's detector (D0) on the simulator's profiles and the replays.

    python -m experiments.envelope_sim.measure --out experiments/envelope_sim/results

Normal profiles: each seed is one agent, run until its first RESTRICTED
verdict or the end of its stream. The result is the share of agents
restricted within 1, 7 and 30 days of simulated time, with an exact
(Clopper-Pearson) 95 % interval. A share by horizon, not a rate per window,
because the chance of a restriction is not constant over an agent's life.
Attack profiles: per agent, restricted before the first attack write, during
the attack (up to one window after its last write: detected), later, or
never; for the detected, the windows from the first attack write to the
restriction and the attack writes counted before it.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .driver import round_time, run_resampled_agent, run_stream
from .replay import TRIALS, load_trial, normal_rounds, replay_d0
from .streams import PROFILES, L

DAY = 86_400.0
HORIZON_DAYS = (1, 7, 30)


def binomial_interval(k: int, n: int, confidence: float = 0.95) -> tuple[float, float]:
    """Exact (Clopper-Pearson) interval for a proportion after k of n, by
    bisection on the binomial CDF, computed in log space."""

    def cdf(j: int, p: float) -> float:
        if p <= 0.0:
            return 1.0
        if p >= 1.0:
            return 1.0 if j >= n else 0.0
        log_terms = [
            math.lgamma(n + 1)
            - math.lgamma(i + 1)
            - math.lgamma(n - i + 1)
            + i * math.log(p)
            + (n - i) * math.log1p(-p)
            for i in range(j + 1)
        ]
        top = max(log_terms)
        return min(1.0, math.exp(top) * sum(math.exp(x - top) for x in log_terms))

    tail = (1.0 - confidence) / 2.0

    def solve(target: float, j: int) -> float:
        lo, hi = 0.0, 1.0
        for _ in range(100):
            mid = (lo + hi) / 2.0
            if cdf(j, mid) > target:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2.0

    lower = 0.0 if k == 0 else solve(1.0 - tail, k - 1)
    upper = 1.0 if k == n else solve(tail, k)
    return lower, upper


def summarize_normal(
    runs: Sequence[dict[str, Any]], horizons: Sequence[float]
) -> dict[str, Any]:
    """The share of agents restricted within each horizon (seconds after the
    agent's first event)."""
    out: dict[str, Any] = {"agents": len(runs)}
    for h in horizons:
        k = sum(
            1
            for r in runs
            if r["restricted_at"] is not None and r["restricted_at"] - r["start"] <= h
        )
        lo, hi = binomial_interval(k, len(runs))
        out[f"restricted_within_{h / DAY:g}_days"] = {
            "agents": k,
            "share": round(k / len(runs), 4),
            "interval95": [round(lo, 4), round(hi, 4)],
        }
    out["restricted_by_the_end"] = sum(
        1 for r in runs if r["restricted_at"] is not None
    )
    return out


def summarize_attack(
    runs: Sequence[dict[str, Any]], window_seconds: float
) -> dict[str, Any]:
    before = detected = late = never = 0
    delays: list[float] = []
    harm: list[int] = []
    for r in runs:
        t = r["restricted_at"]
        if t is None:
            never += 1
        elif t < r["attack_first"]:
            before += 1
        elif t <= r["attack_last"] + window_seconds:
            detected += 1
            delays.append((t - r["attack_first"]) / window_seconds)
            harm.append(r["attack_writes_before"])
        else:
            late += 1
    return {
        "agents": len(runs),
        "restricted_before_the_attack": before,
        "detected": detected,
        "restricted_later": late,
        "never_restricted": never,
        "median_windows_to_detection": (
            round(statistics.median(delays), 2) if delays else None
        ),
        "median_attack_writes_before_detection": (
            statistics.median(harm) if harm else None
        ),
    }


def _run_profile(args: tuple[str, int, int]) -> dict[str, Any]:
    name, seed, n_windows = args
    rng = random.Random(f"{name}/{seed}")
    profile = PROFILES[name]
    return asdict(run_stream(profile.generate(rng, n_windows), profile.config))


_T_POOL: dict[str, Any] = {}


def _run_t(args: tuple[int, int]) -> dict[str, Any]:
    seed, n_rounds = args
    if not _T_POOL:  # loaded once per worker process
        for trial in TRIALS:
            _T_POOL.update(normal_rounds(load_trial(trial)))
    return asdict(run_resampled_agent(_T_POOL, random.Random(f"T/{seed}"), n_rounds))


def replay_summary() -> dict[str, Any]:
    out = {}
    for name in TRIALS:
        trial = load_trial(name)
        first = replay_d0(trial)
        adversaries = [a for a, m in trial.adversarial.items() if m is not None]
        normal = [a for a, m in trial.adversarial.items() if m is None]
        # The slow_drift adversaries' rounds before their restriction are lost
        # (an ERROR step each), so their stored restrictions cannot be replayed.
        compared = [
            a
            for a in trial.adversarial
            if not (name == "slow_drift" and a in adversaries)
        ]
        out[name] = {
            "normal_agents": len(normal),
            "normal_restricted": sum(1 for a in normal if a in first),
            "adversaries": len(adversaries),
            "adversaries_restricted": sum(1 for a in adversaries if a in first),
            "agents_compared_with_stored": len(compared),
            "differences_from_stored": [
                a for a in compared if first.get(a) != trial.stored_first.get(a)
            ],
        }
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Measure D0 on the envelope simulator."
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--seeds", type=int, default=400)
    parser.add_argument("--windows", type=int, default=15_000)
    parser.add_argument("--workers", type=int, default=32)
    parser.add_argument(
        "--quick",
        action="store_true",
        help="20 seeds of 2,000 windows: seconds, for iterating",
    )
    args = parser.parse_args()
    if args.quick:
        args.seeds, args.windows = 20, 2000
    args.out.mkdir(parents=True, exist_ok=True)
    seeds = range(1, args.seeds + 1)
    span_l = args.windows * L.window_seconds
    horizons_l = [d * DAY for d in HORIZON_DAYS if d * DAY <= span_l]
    # Configuration T: two rounds a day; the stored trials ran 10 to 20 days.
    span_t = round_time(args.windows - 1)
    horizons_t = [d * DAY for d in (10, 20, 30) if d * DAY <= span_t]
    results: dict[str, Any] = {
        "config": {"seeds": args.seeds, "windows": args.windows, "quick": args.quick},
        "profiles": {},
    }
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        for name in sorted(PROFILES):
            profile = PROFILES[name]
            runs = list(ex.map(_run_profile, [(name, s, args.windows) for s in seeds]))
            if profile.kind == "normal":
                summary = summarize_normal(runs, horizons_l)
            else:
                summary = summarize_attack(runs, profile.config.window_seconds)
            results["profiles"][name] = {"config": "L", "kind": profile.kind, **summary}
            print(name, json.dumps(results["profiles"][name]), flush=True)
        t_runs = list(ex.map(_run_t, [(s, args.windows) for s in seeds]))
        results["profiles"]["T_resampled_normal"] = {
            "config": "T",
            "kind": "normal",
            **summarize_normal(t_runs, horizons_t),
        }
        print(
            "T_resampled_normal",
            json.dumps(results["profiles"]["T_resampled_normal"]),
            flush=True,
        )
    results["replays"] = replay_summary()
    name = "d0_quick.json" if args.quick else "d0.json"
    (args.out / name).write_text(json.dumps(results, indent=2) + "\n")


if __name__ == "__main__":
    main()
