#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Full Validation Analysis — Stigmergic Coordination

Loads all phase results, runs statistical tests, generates figures.

Usage:
    python experiments/validation/analyze.py
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

RESULTS_DIR = Path(__file__).parent
FIGURES_DIR = RESULTS_DIR / "figures"
FIGURES_DIR.mkdir(exist_ok=True)

MODEL_SHORT = {
    "gpt-oss-120b": "GPT-OSS",
    "deepseek-v3p2": "DeepSeek",
    "kimi-k2p5": "Kimi",
    "glm-5": "GLM-5",
    "mercury-2": "Mercury-2",
}

MODEL_PRICING: dict[str, tuple[float, float]] = {
    "gpt-oss-120b": (0.15, 0.60),
    "deepseek-v3p2": (0.56, 1.68),
    "kimi-k2p5": (0.60, 3.00),
    "glm-5": (1.00, 3.20),
    "mercury-2": (0.25, 0.75),
}

MODEL_ORDER = ["gpt-oss-120b", "deepseek-v3p2", "kimi-k2p5", "glm-5", "mercury-2"]
MODEL_COLORS = {
    "gpt-oss-120b": "#1f77b4",
    "deepseek-v3p2": "#ff7f0e",
    "kimi-k2p5": "#2ca02c",
    "glm-5": "#d62728",
    "mercury-2": "#9467bd",
}


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def load_all() -> pd.DataFrame:
    """Load all JSONL result files into a single DataFrame."""
    rows: list[dict] = []
    for f in sorted(RESULTS_DIR.glob("results_*.jsonl")):
        phase_tag = f.stem.split("_")[1]  # pilot, phase2b, phase3
        for line in open(f):
            r = json.loads(line)
            if r.get("trial_id", 0) == -1:
                continue  # skip sentinels
            if r.get("error"):
                continue  # skip errors
            c = r["cell"]
            n_rounds = c.get("n_rounds", 1)
            rounds_data = r.get("rounds", [])
            row = {
                "phase": r["phase"],
                "file": f.name,
                "model": c["model"],
                "model_short": MODEL_SHORT.get(c["model"], c["model"]),
                "n_agents": c["n_agents"],
                "visibility": c["visibility"],
                "temperature": c["temperature"],
                "execution_mode": c["execution_mode"],
                "n_rounds": n_rounds,
                "trial_id": r["trial_id"],
                "steps_per_agent": r["steps_per_agent"],
                "total_steps": r["total_steps"],
                "guard_invocations": r["guard_invocations"],
                "wasted_attempts": r["wasted_attempts"],
                "double_bookings": r["double_bookings"],
                "all_completed": r["all_completed"],
                "wall_seconds": r["wall_clock_seconds"],
                "prompt_tokens": r["tokens"]["prompt"],
                "completion_tokens": r["tokens"]["completion"],
                "total_tokens": r["tokens"]["prompt"] + r["tokens"]["completion"],
            }
            # Per-round metrics for multi-phase trials
            if len(rounds_data) >= 2:
                row["r1_steps_per_agent"] = rounds_data[0]["steps_per_agent"]
                row["r2_steps_per_agent"] = rounds_data[1]["steps_per_agent"]
                row["r1_completed"] = rounds_data[0]["all_completed"]
                row["r2_completed"] = rounds_data[1]["all_completed"]
                row["r1_double_bookings"] = rounds_data[0]["double_bookings"]
                row["r2_double_bookings"] = rounds_data[1]["double_bookings"]
            else:
                row["r1_steps_per_agent"] = np.nan
                row["r2_steps_per_agent"] = np.nan
                row["r1_completed"] = np.nan
                row["r2_completed"] = np.nan
                row["r1_double_bookings"] = np.nan
                row["r2_double_bookings"] = np.nan
            # Compute cost
            pricing = MODEL_PRICING.get(c["model"], (0.50, 2.00))
            row["cost"] = (
                row["prompt_tokens"] / 1_000_000 * pricing[0]
                + row["completion_tokens"] / 1_000_000 * pricing[1]
            )
            # Efficiency: wasted / total steps
            row["waste_ratio"] = (
                r["wasted_attempts"] / r["total_steps"] if r["total_steps"] > 0 else 0.0
            )
            rows.append(row)
    df = pd.DataFrame(rows)
    return df


# ---------------------------------------------------------------------------
# Figures
# ---------------------------------------------------------------------------


def fig_steps_by_visibility_and_n(df: pd.DataFrame, exec_mode: str, tag: str) -> None:
    """Bar chart: steps/agent by visibility × n_agents, grouped by model."""
    sub = df[df["execution_mode"] == exec_mode]
    if len(sub) == 0:
        return

    fig, axes = plt.subplots(1, 3, figsize=(15, 5), sharey=True)
    fig.suptitle(f"Steps per Agent — {tag}", fontsize=14, fontweight="bold")

    for idx, n in enumerate(sorted(sub["n_agents"].unique())):
        ax = axes[idx]
        n_sub = sub[sub["n_agents"] == n]

        x = np.arange(len(MODEL_ORDER))
        width = 0.35

        vis_means = []
        vis_errs = []
        hid_means = []
        hid_errs = []

        for m in MODEL_ORDER:
            v = n_sub[(n_sub["model"] == m) & (n_sub["visibility"] == "visible")][
                "steps_per_agent"
            ]
            h = n_sub[(n_sub["model"] == m) & (n_sub["visibility"] == "hidden")][
                "steps_per_agent"
            ]
            vis_means.append(v.mean() if len(v) > 0 else 0)
            vis_errs.append(v.std() / math.sqrt(len(v)) if len(v) > 1 else 0)
            hid_means.append(h.mean() if len(h) > 0 else 0)
            hid_errs.append(h.std() / math.sqrt(len(h)) if len(h) > 1 else 0)

        bars1 = ax.bar(
            x - width / 2,
            vis_means,
            width,
            yerr=vis_errs,
            label="visible",
            color="#4CAF50",
            alpha=0.8,
            capsize=3,
        )
        bars2 = ax.bar(
            x + width / 2,
            hid_means,
            width,
            yerr=hid_errs,
            label="hidden",
            color="#F44336",
            alpha=0.8,
            capsize=3,
        )

        ax.set_xlabel("Model")
        ax.set_title(f"N={n} agents")
        ax.set_xticks(x)
        ax.set_xticklabels(
            [MODEL_SHORT[m] for m in MODEL_ORDER], rotation=30, ha="right"
        )
        if idx == 0:
            ax.set_ylabel("Steps per Agent (mean ± SE)")
            ax.legend()

    plt.tight_layout()
    plt.savefig(
        FIGURES_DIR / f"steps_by_visibility_{tag.lower().replace(' ', '_')}.png",
        dpi=150,
    )
    plt.close()
    print(f"  Saved: steps_by_visibility_{tag.lower().replace(' ', '_')}.png")


def fig_steps_heatmap(df: pd.DataFrame, exec_mode: str, tag: str) -> None:
    """Heatmap: mean steps/agent by model × condition."""
    sub = df[df["execution_mode"] == exec_mode]
    if len(sub) == 0:
        return

    # Build pivot: rows = model, cols = N_vis_temp
    pivot_data: dict[str, dict[str, float]] = {}
    for m in MODEL_ORDER:
        pivot_data[MODEL_SHORT[m]] = {}
        for n in sorted(sub["n_agents"].unique()):
            for vis in ["visible", "hidden"]:
                for t in sorted(sub["temperature"].unique()):
                    key = f"N{n}_{vis[:3]}_t{t}"
                    vals = sub[
                        (sub["model"] == m)
                        & (sub["n_agents"] == n)
                        & (sub["visibility"] == vis)
                        & (sub["temperature"] == t)
                    ]["steps_per_agent"]
                    pivot_data[MODEL_SHORT[m]][key] = (
                        vals.mean() if len(vals) > 0 else np.nan
                    )

    pdf = pd.DataFrame(pivot_data).T
    fig, ax = plt.subplots(figsize=(16, 4))
    im = ax.imshow(pdf.values, cmap="YlOrRd", aspect="auto")
    ax.set_xticks(range(len(pdf.columns)))
    ax.set_xticklabels(pdf.columns, rotation=45, ha="right", fontsize=8)
    ax.set_yticks(range(len(pdf.index)))
    ax.set_yticklabels(pdf.index)
    # Annotate cells
    for i in range(len(pdf.index)):
        for j in range(len(pdf.columns)):
            val = pdf.values[i, j]
            if not np.isnan(val):
                color = "white" if val > 3.5 else "black"
                ax.text(
                    j,
                    i,
                    f"{val:.1f}",
                    ha="center",
                    va="center",
                    fontsize=7,
                    color=color,
                )
    plt.colorbar(im, ax=ax, label="Steps/Agent")
    ax.set_title(f"Mean Steps per Agent — {tag}", fontweight="bold")
    plt.tight_layout()
    plt.savefig(FIGURES_DIR / f"heatmap_{tag.lower().replace(' ', '_')}.png", dpi=150)
    plt.close()
    print(f"  Saved: heatmap_{tag.lower().replace(' ', '_')}.png")


def fig_sequential_vs_concurrent(df: pd.DataFrame) -> None:
    """Compare sequential vs concurrent steps/agent for hidden conditions."""
    seq = df[(df["execution_mode"] == "sequential") & (df["visibility"] == "hidden")]
    conc = df[(df["execution_mode"] == "concurrent") & (df["visibility"] == "hidden")]

    if len(seq) == 0 or len(conc) == 0:
        return

    fig, axes = plt.subplots(1, 3, figsize=(15, 5), sharey=True)
    fig.suptitle(
        "Sequential vs Concurrent — Hidden Visibility", fontsize=14, fontweight="bold"
    )

    for idx, n in enumerate(sorted(seq["n_agents"].unique())):
        ax = axes[idx]
        x = np.arange(len(MODEL_ORDER))
        width = 0.35

        seq_means = []
        seq_errs = []
        conc_means = []
        conc_errs = []

        for m in MODEL_ORDER:
            s = seq[(seq["model"] == m) & (seq["n_agents"] == n)]["steps_per_agent"]
            c = conc[(conc["model"] == m) & (conc["n_agents"] == n)]["steps_per_agent"]
            seq_means.append(s.mean() if len(s) > 0 else 0)
            seq_errs.append(s.std() / math.sqrt(len(s)) if len(s) > 1 else 0)
            conc_means.append(c.mean() if len(c) > 0 else 0)
            conc_errs.append(c.std() / math.sqrt(len(c)) if len(c) > 1 else 0)

        ax.bar(
            x - width / 2,
            seq_means,
            width,
            yerr=seq_errs,
            label="sequential",
            color="#2196F3",
            alpha=0.8,
            capsize=3,
        )
        ax.bar(
            x + width / 2,
            conc_means,
            width,
            yerr=conc_errs,
            label="concurrent",
            color="#FF9800",
            alpha=0.8,
            capsize=3,
        )

        ax.set_title(f"N={n} agents")
        ax.set_xticks(x)
        ax.set_xticklabels(
            [MODEL_SHORT[m] for m in MODEL_ORDER], rotation=30, ha="right"
        )
        if idx == 0:
            ax.set_ylabel("Steps per Agent (mean ± SE)")
            ax.legend()

    plt.tight_layout()
    plt.savefig(FIGURES_DIR / "seq_vs_concurrent_hidden.png", dpi=150)
    plt.close()
    print("  Saved: seq_vs_concurrent_hidden.png")


def fig_waste_ratio(df: pd.DataFrame) -> None:
    """Wasted attempts ratio by model and condition."""
    fig, axes = plt.subplots(1, 2, figsize=(14, 5))
    fig.suptitle(
        "Wasted Attempts (CONFLICT retries / total steps)",
        fontsize=14,
        fontweight="bold",
    )

    for idx, exec_mode in enumerate(["sequential", "concurrent"]):
        ax = axes[idx]
        sub = df[(df["execution_mode"] == exec_mode) & (df["visibility"] == "hidden")]
        if len(sub) == 0:
            continue

        for m in MODEL_ORDER:
            m_sub = sub[sub["model"] == m]
            means = []
            ns = sorted(m_sub["n_agents"].unique())
            for n in ns:
                vals = m_sub[m_sub["n_agents"] == n]["waste_ratio"]
                means.append(vals.mean() if len(vals) > 0 else 0)
            ax.plot(
                ns,
                means,
                "o-",
                label=MODEL_SHORT[m],
                color=MODEL_COLORS[m],
                linewidth=2,
            )

        ax.set_xlabel("Number of Agents")
        ax.set_ylabel("Waste Ratio")
        ax.set_title(f"{exec_mode.title()} — Hidden")
        ax.legend()
        ax.set_xticks(sorted(sub["n_agents"].unique()))

    plt.tight_layout()
    plt.savefig(FIGURES_DIR / "waste_ratio.png", dpi=150)
    plt.close()
    print("  Saved: waste_ratio.png")


def fig_model_performance(df: pd.DataFrame) -> None:
    """Model comparison: cost, tokens, wall time, completion rate."""
    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    fig.suptitle("Model Performance Comparison", fontsize=14, fontweight="bold")

    # Only use complete trials
    ok = df[df["all_completed"]].copy()

    # 1. Cost per trial by model and execution mode
    ax = axes[0, 0]
    for i, exec_mode in enumerate(["sequential", "concurrent"]):
        sub = ok[ok["execution_mode"] == exec_mode]
        x = np.arange(len(MODEL_ORDER))
        means = [
            sub[sub["model"] == m]["cost"].mean() * 1000 for m in MODEL_ORDER
        ]  # millicents
        ax.bar(x + i * 0.4 - 0.2, means, 0.35, label=exec_mode, alpha=0.8)
    ax.set_ylabel("Cost per Trial ($ × 1000)")
    ax.set_title("Cost per Trial")
    ax.set_xticks(np.arange(len(MODEL_ORDER)))
    ax.set_xticklabels([MODEL_SHORT[m] for m in MODEL_ORDER])
    ax.legend()

    # 2. Total tokens by model
    ax = axes[0, 1]
    for m in MODEL_ORDER:
        m_ok = ok[ok["model"] == m]
        data = []
        for n in sorted(ok["n_agents"].unique()):
            vals = m_ok[m_ok["n_agents"] == n]["total_tokens"]
            data.append(vals.mean() if len(vals) > 0 else 0)
        ax.plot(
            sorted(ok["n_agents"].unique()),
            data,
            "o-",
            label=MODEL_SHORT[m],
            color=MODEL_COLORS[m],
            linewidth=2,
        )
    ax.set_xlabel("Number of Agents")
    ax.set_ylabel("Total Tokens per Trial")
    ax.set_title("Token Usage Scaling")
    ax.legend()

    # 3. Wall time by model and n_agents (concurrent only)
    ax = axes[1, 0]
    conc = ok[ok["execution_mode"] == "concurrent"]
    if len(conc) > 0:
        for m in MODEL_ORDER:
            m_sub = conc[conc["model"] == m]
            ns = sorted(m_sub["n_agents"].unique())
            means = [m_sub[m_sub["n_agents"] == n]["wall_seconds"].mean() for n in ns]
            ax.plot(
                ns,
                means,
                "o-",
                label=MODEL_SHORT[m],
                color=MODEL_COLORS[m],
                linewidth=2,
            )
        ax.set_xlabel("Number of Agents")
        ax.set_ylabel("Wall Clock (seconds)")
        ax.set_title("Wall Time — Concurrent")
        ax.legend()
        ax.set_xticks(sorted(conc["n_agents"].unique()))

    # 4. Steps/agent for hidden N=10 (hardest condition) — all models
    ax = axes[1, 1]
    hard = ok[(ok["n_agents"] == 10) & (ok["visibility"] == "hidden")]
    if len(hard) > 0:
        positions = []
        data_list = []
        labels_list = []
        colors_list = []
        pos = 0
        for exec_mode in ["sequential", "concurrent"]:
            for m in MODEL_ORDER:
                vals = hard[
                    (hard["model"] == m) & (hard["execution_mode"] == exec_mode)
                ]["steps_per_agent"]
                if len(vals) > 0:
                    data_list.append(vals.values)
                    positions.append(pos)
                    labels_list.append(f"{MODEL_SHORT[m]}\n{exec_mode[:3]}")
                    colors_list.append(MODEL_COLORS[m])
                    pos += 1
            pos += 0.5  # gap between exec modes

        bp = ax.boxplot(data_list, positions=positions, widths=0.6, patch_artist=True)
        for patch, color in zip(bp["boxes"], colors_list):
            patch.set_facecolor(color)
            patch.set_alpha(0.6)
        ax.set_xticks(positions)
        ax.set_xticklabels(labels_list, fontsize=7, rotation=30, ha="right")
        ax.set_ylabel("Steps per Agent")
        ax.set_title("Hardest Condition: N=10 Hidden")

    plt.tight_layout()
    plt.savefig(FIGURES_DIR / "model_performance.png", dpi=150)
    plt.close()
    print("  Saved: model_performance.png")


def fig_temperature_effect(df: pd.DataFrame) -> None:
    """Temperature effect on variance (SD) for hidden conditions."""
    fig, axes = plt.subplots(1, 2, figsize=(14, 5))
    fig.suptitle(
        "Temperature Effect on Variance (Hidden Conditions)",
        fontsize=14,
        fontweight="bold",
    )

    for idx, exec_mode in enumerate(["sequential", "concurrent"]):
        ax = axes[idx]
        sub = df[(df["execution_mode"] == exec_mode) & (df["visibility"] == "hidden")]
        if len(sub) == 0:
            continue

        x_labels = []
        sd_0 = []
        sd_7 = []

        for m in MODEL_ORDER:
            for n in sorted(sub["n_agents"].unique()):
                v0 = sub[
                    (sub["model"] == m)
                    & (sub["n_agents"] == n)
                    & (sub["temperature"] == 0.0)
                ]["steps_per_agent"]
                v7 = sub[
                    (sub["model"] == m)
                    & (sub["n_agents"] == n)
                    & (sub["temperature"] == 0.7)
                ]["steps_per_agent"]
                if len(v0) > 1 and len(v7) > 1:
                    x_labels.append(f"{MODEL_SHORT[m]}\nN={n}")
                    sd_0.append(v0.std())
                    sd_7.append(v7.std())

        x = np.arange(len(x_labels))
        width = 0.35
        ax.bar(x - width / 2, sd_0, width, label="t=0.0", color="#2196F3", alpha=0.8)
        ax.bar(x + width / 2, sd_7, width, label="t=0.7", color="#F44336", alpha=0.8)
        ax.set_xticks(x)
        ax.set_xticklabels(x_labels, fontsize=7, rotation=30, ha="right")
        ax.set_ylabel("Standard Deviation")
        ax.set_title(f"{exec_mode.title()}")
        ax.legend()

    plt.tight_layout()
    plt.savefig(FIGURES_DIR / "temperature_effect.png", dpi=150)
    plt.close()
    print("  Saved: temperature_effect.png")


def fig_safety_cumulative(df: pd.DataFrame) -> None:
    """Cumulative double booking rate with CI band."""
    # Sort all trials chronologically by file then trial_id
    all_trials = df.sort_values(["file", "trial_id"]).reset_index(drop=True)

    cum_trials = np.arange(1, len(all_trials) + 1)
    cum_dbl = np.cumsum(all_trials["double_bookings"].values)
    cum_rate = cum_dbl / cum_trials

    # Wilson CI upper bound
    z = 1.96
    upper = np.zeros(len(cum_trials))
    for i, (n, k) in enumerate(zip(cum_trials, cum_dbl)):
        p_hat = k / n
        upper[i] = (
            p_hat
            + z**2 / (2 * n)
            + z * math.sqrt((p_hat * (1 - p_hat) + z**2 / (4 * n)) / n)
        ) / (1 + z**2 / n)

    fig, ax = plt.subplots(figsize=(12, 5))
    ax.plot(cum_trials, cum_rate, color="#d62728", linewidth=2, label="Observed rate")
    ax.fill_between(
        cum_trials, 0, upper, alpha=0.2, color="#d62728", label="95% CI upper bound"
    )
    ax.set_xlabel("Cumulative Trials")
    ax.set_ylabel("Double Booking Rate")
    ax.set_title("Cumulative Safety — Double Booking Rate", fontweight="bold")
    ax.legend()
    ax.set_ylim(-0.001, max(0.02, upper[-1] * 2))

    # Add vertical lines for phase boundaries
    phases = all_trials.groupby("file").size().cumsum()
    for name, boundary in phases.items():
        label = name.replace("results_", "").replace("_20260227.jsonl", "")
        ax.axvline(boundary, color="gray", linestyle="--", alpha=0.5)
        ax.text(boundary, ax.get_ylim()[1] * 0.9, f" {label}", fontsize=8, color="gray")

    plt.tight_layout()
    plt.savefig(FIGURES_DIR / "safety_cumulative.png", dpi=150)
    plt.close()
    print("  Saved: safety_cumulative.png")


def fig_round_comparison(df: pd.DataFrame) -> None:
    """Round 1 vs Round 2 steps/agent comparison for multi-phase trials."""
    multi = df[df["n_rounds"] > 1].dropna(
        subset=["r1_steps_per_agent", "r2_steps_per_agent"]
    )
    if len(multi) == 0:
        return

    models_present = [m for m in MODEL_ORDER if m in multi["model"].values]
    if not models_present:
        return

    n_vals = sorted(multi["n_agents"].unique())
    fig, axes = plt.subplots(
        1, max(len(n_vals), 1), figsize=(6 * len(n_vals), 5), sharey=True, squeeze=False
    )
    fig.suptitle(
        "Multi-Phase: Round 1 vs Round 2 Steps per Agent",
        fontsize=14,
        fontweight="bold",
    )

    for idx, n in enumerate(n_vals):
        ax = axes[0][idx]
        n_sub = multi[multi["n_agents"] == n]

        x = np.arange(len(models_present))
        width = 0.35

        r1_means = []
        r1_errs = []
        r2_means = []
        r2_errs = []

        for m in models_present:
            r1 = n_sub[n_sub["model"] == m]["r1_steps_per_agent"]
            r2 = n_sub[n_sub["model"] == m]["r2_steps_per_agent"]
            r1_means.append(r1.mean() if len(r1) > 0 else 0)
            r1_errs.append(r1.std() / math.sqrt(len(r1)) if len(r1) > 1 else 0)
            r2_means.append(r2.mean() if len(r2) > 0 else 0)
            r2_errs.append(r2.std() / math.sqrt(len(r2)) if len(r2) > 1 else 0)

        ax.bar(
            x - width / 2,
            r1_means,
            width,
            yerr=r1_errs,
            label="Round 1",
            color="#2196F3",
            alpha=0.8,
            capsize=3,
        )
        ax.bar(
            x + width / 2,
            r2_means,
            width,
            yerr=r2_errs,
            label="Round 2",
            color="#FF9800",
            alpha=0.8,
            capsize=3,
        )

        ax.set_xlabel("Model")
        ax.set_title(f"N={n} agents")
        ax.set_xticks(x)
        ax.set_xticklabels(
            [MODEL_SHORT.get(m, m) for m in models_present], rotation=30, ha="right"
        )
        if idx == 0:
            ax.set_ylabel("Steps per Agent (mean ± SE)")
            ax.legend()

    plt.tight_layout()
    plt.savefig(FIGURES_DIR / "round_comparison.png", dpi=150)
    plt.close()
    print("  Saved: round_comparison.png")


def fig_round_comparison_by_visibility(df: pd.DataFrame) -> None:
    """Round 2 penalty (R2 - R1 steps/agent) by visibility and model."""
    multi = (
        df[df["n_rounds"] > 1]
        .dropna(subset=["r1_steps_per_agent", "r2_steps_per_agent"])
        .copy()
    )
    if len(multi) == 0:
        return

    multi["r2_penalty"] = multi["r2_steps_per_agent"] - multi["r1_steps_per_agent"]
    models_present = [m for m in MODEL_ORDER if m in multi["model"].values]
    if not models_present:
        return

    fig, ax = plt.subplots(figsize=(10, 5))
    fig.suptitle(
        "Round 2 Efficiency Penalty (R2 - R1 steps/agent)",
        fontsize=14,
        fontweight="bold",
    )

    x = np.arange(len(models_present))
    width = 0.35

    for i, vis in enumerate(["visible", "hidden"]):
        means = []
        errs = []
        for m in models_present:
            vals = multi[(multi["model"] == m) & (multi["visibility"] == vis)][
                "r2_penalty"
            ]
            means.append(vals.mean() if len(vals) > 0 else 0)
            errs.append(vals.std() / math.sqrt(len(vals)) if len(vals) > 1 else 0)
        color = "#4CAF50" if vis == "visible" else "#F44336"
        ax.bar(
            x + (i - 0.5) * width,
            means,
            width,
            yerr=errs,
            label=vis,
            color=color,
            alpha=0.8,
            capsize=3,
        )

    ax.axhline(0, color="gray", linestyle="--", linewidth=0.8)
    ax.set_xlabel("Model")
    ax.set_ylabel("R2 Penalty (steps/agent)")
    ax.set_xticks(x)
    ax.set_xticklabels(
        [MODEL_SHORT.get(m, m) for m in models_present], rotation=30, ha="right"
    )
    ax.legend()

    plt.tight_layout()
    plt.savefig(FIGURES_DIR / "round_penalty_by_visibility.png", dpi=150)
    plt.close()
    print("  Saved: round_penalty_by_visibility.png")


def fig_scaling(df: pd.DataFrame) -> None:
    """How steps/agent scales with N for each model."""
    fig, axes = plt.subplots(1, 2, figsize=(14, 5))
    fig.suptitle(
        "Coordination Cost Scaling with Agent Count", fontsize=14, fontweight="bold"
    )

    for idx, exec_mode in enumerate(["sequential", "concurrent"]):
        ax = axes[idx]
        sub = df[
            (df["execution_mode"] == exec_mode)
            & (df["visibility"] == "hidden")
            & (df["temperature"] == 0.0)
        ]

        for m in MODEL_ORDER:
            m_sub = sub[sub["model"] == m]
            ns = sorted(m_sub["n_agents"].unique())
            means = []
            errs = []
            for n in ns:
                vals = m_sub[m_sub["n_agents"] == n]["steps_per_agent"]
                means.append(vals.mean())
                errs.append(vals.std() / math.sqrt(len(vals)) if len(vals) > 1 else 0)
            ax.errorbar(
                ns,
                means,
                yerr=errs,
                fmt="o-",
                label=MODEL_SHORT[m],
                color=MODEL_COLORS[m],
                linewidth=2,
                capsize=4,
            )

        # Theoretical minimum: 1 + (N-1)/N for hidden (first agent books directly,
        # rest need 1 extra step on average for the conflict)
        ns_theory = np.array([3, 5, 10])
        # With view hidden: each agent tries preferred, gets conflict, tries another
        # Minimum = 1 (if lucky) to N (worst case sequential collisions)

        ax.set_xlabel("Number of Agents")
        ax.set_ylabel("Steps per Agent")
        ax.set_title(f"{exec_mode.title()} — Hidden, t=0.0")
        ax.legend()
        ax.set_xticks(sorted(sub["n_agents"].unique()) if len(sub) > 0 else [3, 5, 10])

    plt.tight_layout()
    plt.savefig(FIGURES_DIR / "scaling.png", dpi=150)
    plt.close()
    print("  Saved: scaling.png")


# ---------------------------------------------------------------------------
# Summary tables
# ---------------------------------------------------------------------------


def print_summary_table(df: pd.DataFrame) -> None:
    """Print comprehensive summary tables."""
    print("=" * 90)
    print("OVERALL SUMMARY")
    print("=" * 90)
    total = len(df)
    ok = df["all_completed"].sum()
    dbl = df["double_bookings"].sum()
    print(f"  Total trials (excl errors/sentinels): {total}")
    print(f"  Completed: {ok}")
    print(f"  Double bookings: {dbl}")

    # By phase
    print(f"\n  By phase:")
    for phase, g in df.groupby("file"):
        n = len(g)
        n_ok = g["all_completed"].sum()
        n_dbl = g["double_bookings"].sum()
        print(f"    {phase:<45} {n:>4} trials, {n_ok:>4} completed, {n_dbl} dbl")

    # Safety CI
    print(f"\n  Safety:")
    n_total = len(df[df["all_completed"]])
    n_dbl = int(df["double_bookings"].sum())
    if n_dbl == 0:
        upper = 1 - 0.05 ** (1 / n_total)
        print(f"    {n_total} completed trials, {n_dbl} double bookings")
        print(f"    95% CI on failure rate: [0, {upper:.4%}]")
    else:
        rate = n_dbl / n_total
        # Wilson interval
        z = 1.96
        p_hat = rate
        lower = (
            p_hat
            + z**2 / (2 * n_total)
            - z * math.sqrt((p_hat * (1 - p_hat) + z**2 / (4 * n_total)) / n_total)
        ) / (1 + z**2 / n_total)
        upper = (
            p_hat
            + z**2 / (2 * n_total)
            + z * math.sqrt((p_hat * (1 - p_hat) + z**2 / (4 * n_total)) / n_total)
        ) / (1 + z**2 / n_total)
        print(f"    {n_total} completed trials, {n_dbl} double bookings")
        print(f"    Rate: {rate:.4%}")
        print(f"    95% Wilson CI: [{max(0, lower):.4%}, {upper:.4%}]")

    # Model summary
    print(f"\n{'=' * 90}")
    print("MODEL SUMMARY")
    print("=" * 90)
    print(
        f"  {'Model':<15} {'Trials':>7} {'Compl%':>7} {'Steps/A':>8} {'Waste%':>7} {'Tokens':>8} {'$/trial':>8}"
    )
    for m in MODEL_ORDER:
        m_df = df[df["model"] == m]
        m_ok = m_df[m_df["all_completed"]]
        compl_pct = len(m_ok) / len(m_df) * 100 if len(m_df) > 0 else 0
        steps = m_ok["steps_per_agent"].mean() if len(m_ok) > 0 else 0
        waste = m_ok["waste_ratio"].mean() * 100 if len(m_ok) > 0 else 0
        tokens = m_ok["total_tokens"].mean() if len(m_ok) > 0 else 0
        cost = m_ok["cost"].mean() if len(m_ok) > 0 else 0
        print(
            f"  {MODEL_SHORT[m]:<15} {len(m_df):>7} {compl_pct:>6.1f}% {steps:>8.2f} {waste:>6.1f}% {tokens:>8,.0f} ${cost:>7.4f}"
        )


def print_condition_table(df: pd.DataFrame) -> None:
    """Print per-condition breakdown."""
    print(f"\n{'=' * 90}")
    print("CONDITION BREAKDOWN — Mean Steps/Agent (SD)")
    print("=" * 90)

    for exec_mode in ["sequential", "concurrent"]:
        sub = df[df["execution_mode"] == exec_mode]
        if len(sub) == 0:
            continue

        for n_rounds in sorted(sub["n_rounds"].unique()):
            rounds_sub = sub[sub["n_rounds"] == n_rounds]
            rounds_label = f" R={n_rounds}" if n_rounds > 1 else ""
            print(f"\n--- {exec_mode.title()}{rounds_label} ---")
            models_present = [m for m in MODEL_ORDER if m in rounds_sub["model"].values]
            header = f"  {'Condition':<35}"
            for m in models_present:
                header += f" {MODEL_SHORT[m]:>12}"
            print(header)
            print("  " + "-" * (35 + 13 * len(models_present)))

            for n in sorted(rounds_sub["n_agents"].unique()):
                for vis in ["visible", "hidden"]:
                    for t in sorted(rounds_sub["temperature"].unique()):
                        label = f"N={n} {vis} t={t}"
                        row = f"  {label:<35}"
                        for m in models_present:
                            vals = rounds_sub[
                                (rounds_sub["model"] == m)
                                & (rounds_sub["n_agents"] == n)
                                & (rounds_sub["visibility"] == vis)
                                & (rounds_sub["temperature"] == t)
                            ]["steps_per_agent"]
                            if len(vals) > 0:
                                mean = vals.mean()
                                sd = vals.std()
                                row += f" {mean:>5.2f}({sd:.2f})"
                            else:
                                row += f" {'—':>12}"
                        print(row)

    # Multi-round per-round breakdown
    multi = df[df["n_rounds"] > 1].dropna(
        subset=["r1_steps_per_agent", "r2_steps_per_agent"]
    )
    if len(multi) > 0:
        print(f"\n{'=' * 90}")
        print("MULTI-PHASE PER-ROUND BREAKDOWN — Mean Steps/Agent (R1 / R2)")
        print("=" * 90)
        models_present = [m for m in MODEL_ORDER if m in multi["model"].values]
        header = f"  {'Condition':<35}"
        for m in models_present:
            header += f" {MODEL_SHORT[m]:>16}"
        print(header)
        print("  " + "-" * (35 + 17 * len(models_present)))

        for n in sorted(multi["n_agents"].unique()):
            for vis in ["visible", "hidden"]:
                for t in sorted(multi["temperature"].unique()):
                    label = f"N={n} {vis} t={t}"
                    row = f"  {label:<35}"
                    for m in models_present:
                        msub = multi[
                            (multi["model"] == m)
                            & (multi["n_agents"] == n)
                            & (multi["visibility"] == vis)
                            & (multi["temperature"] == t)
                        ]
                        if len(msub) > 0:
                            r1 = msub["r1_steps_per_agent"].mean()
                            r2 = msub["r2_steps_per_agent"].mean()
                            row += f" {r1:>5.1f} / {r2:<5.1f}"
                        else:
                            row += f" {'—':>16}"
                    print(row)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    print("Loading data...")
    df = load_all()
    print(f"Loaded {len(df)} trials from {df['file'].nunique()} files\n")

    print_summary_table(df)
    print_condition_table(df)

    print(f"\n{'=' * 90}")
    print("GENERATING FIGURES")
    print("=" * 90)
    fig_steps_by_visibility_and_n(df, "sequential", "Sequential (Pilot)")
    fig_steps_by_visibility_and_n(df, "concurrent", "Concurrent (Phase 2b)")
    fig_steps_heatmap(df, "sequential", "Sequential (Pilot)")
    fig_steps_heatmap(df, "concurrent", "Concurrent (Phase 2b)")
    fig_sequential_vs_concurrent(df)
    fig_waste_ratio(df)
    fig_model_performance(df)
    fig_temperature_effect(df)
    fig_safety_cumulative(df)
    fig_scaling(df)
    fig_round_comparison(df)
    fig_round_comparison_by_visibility(df)

    print(f"\nAll figures saved to {FIGURES_DIR}/")


if __name__ == "__main__":
    main()
