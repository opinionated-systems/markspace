#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Final analysis plots — only the insightful ones."""

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

RESULTS_DIR = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("results")

# ── Load data ──────────────────────────────────────────────
rounds: list[dict] = []
with open(RESULTS_DIR / "rounds.jsonl") as f:
    for line in f:
        if line.strip():
            rounds.append(json.loads(line))

steps: list[dict] = []
with open(RESULTS_DIR / "steps.jsonl") as f:
    for line in f:
        if line.strip():
            steps.append(json.loads(line))

DEPTS = ["eng", "design", "product", "sales", "ops"]
DEPT_COLORS = {
    "eng": "#2196F3",
    "design": "#9C27B0",
    "product": "#FF9800",
    "sales": "#4CAF50",
    "ops": "#F44336",
}
ROUND_LABELS = [f"{r['day'].upper()}\n{r['block']}" for r in rounds]


# ── Figure 1: Shared room contention ──────────────────────
def plot_shared_room_contention():
    dept_attempts: Counter = Counter()
    dept_success: Counter = Counter()
    for s in steps:
        if s.get("tool") == "book_shared_room":
            agent = s["agent"]
            for d in DEPTS:
                if agent.startswith(d):
                    dept_attempts[d] += 1
                    if s.get("guard_verdict") == "allow":
                        dept_success[d] += 1
                    break

    fig, ax = plt.subplots(figsize=(8, 4.5))
    x = np.arange(len(DEPTS))
    width = 0.35
    attempts = [dept_attempts.get(d, 0) for d in DEPTS]
    successes = [dept_success.get(d, 0) for d in DEPTS]

    ax.bar(x - width / 2, attempts, width, label="Attempts", color="#BBDEFB")
    ax.bar(x + width / 2, successes, width, label="Success", color="#1565C0")

    for i, (att, suc) in enumerate(zip(attempts, successes)):
        rate = suc / att * 100 if att else 0
        ax.text(
            i + width / 2,
            suc + 1,
            f"{rate:.0f}%",
            ha="center",
            fontsize=9,
            fontweight="bold",
        )

    ax.set_xticks(x)
    ax.set_xticklabels([d.capitalize() for d in DEPTS])
    ax.set_ylabel("Count")
    ax.set_title("Shared room booking: attempts vs success by department")
    ax.legend()
    fig.tight_layout()
    out = RESULTS_DIR / "fig_shared_room_contention.png"
    fig.savefig(out, dpi=150)
    print(f"Saved: {out}")
    plt.close(fig)


# ── Figure 2: Decay curves ────────────────────────────────
def plot_decay_curves():
    fig, ax = plt.subplots(figsize=(10, 4.5))
    hours = np.linspace(0, 48, 500)

    obs_strength = 0.5 ** (hours / 6)
    ax.plot(
        hours,
        obs_strength,
        label="Observation (half-life=6h)",
        color="#2196F3",
        linewidth=2,
    )

    warn_strength = 0.5 ** (hours / 2)
    ax.plot(
        hours,
        warn_strength,
        label="Warning (half-life=2h)",
        color="#F44336",
        linewidth=2,
    )

    intent_strength = np.where(hours <= 2, 1.0, 0.0)
    ax.plot(
        hours,
        intent_strength,
        label="Intent (TTL=2h)",
        color="#FF9800",
        linewidth=2,
        linestyle="--",
    )

    round_hours = [0, 4, 24, 28, 48]
    round_labels = ["Mon\nAM", "Mon\nPM", "Tue\nAM", "Tue\nPM", "Wed\nAM"]
    for h, label in zip(round_hours, round_labels):
        ax.axvline(x=h, color="#E0E0E0", linestyle=":", linewidth=1)
        ax.text(h, 1.05, label, ha="center", fontsize=8, color="#757575")

    ax.set_xlabel("Hours since mark creation")
    ax.set_ylabel("Effective strength")
    ax.set_title("Mark decay over time")
    ax.legend(fontsize=10)
    ax.set_ylim(-0.05, 1.15)
    ax.set_xlim(-1, 50)
    fig.tight_layout()
    out = RESULTS_DIR / "fig_decay_curves.png"
    fig.savefig(out, dpi=150)
    print(f"Saved: {out}")
    plt.close(fig)


# ── Figure 3: Round progression (4-panel) ─────────────────
def plot_round_progression():
    fig, axes = plt.subplots(2, 2, figsize=(13, 8))
    x = np.arange(len(rounds))

    # Panel 1: Active agents
    ax = axes[0, 0]
    active = [r["active_agents"] for r in rounds]
    ax.bar(x, active, color="#42A5F5", width=0.7)
    ax.set_ylabel("Active agents")
    ax.set_title("Active agents per round")
    ax.set_xticks(x)
    ax.set_xticklabels(ROUND_LABELS, fontsize=8)

    # Panel 2: Steps vs wasted
    ax = axes[0, 1]
    total_steps = [r["steps"] for r in rounds]
    wasted = [r["wasted_attempts"] for r in rounds]
    useful = [t - w for t, w in zip(total_steps, wasted)]
    ax.bar(x, useful, color="#66BB6A", width=0.7, label="Useful")
    ax.bar(x, wasted, bottom=useful, color="#EF5350", width=0.7, label="Wasted")
    ax.set_ylabel("Steps")
    ax.set_title("Steps: useful vs wasted")
    ax.legend(fontsize=8)
    ax.set_xticks(x)
    ax.set_xticklabels(ROUND_LABELS, fontsize=8)

    # Panel 3: Mark accumulation
    ax = axes[1, 0]
    mark_types = ["action", "intent", "need", "observation", "warning"]
    colors_marks = {
        "action": "#1565C0",
        "intent": "#42A5F5",
        "need": "#FF9800",
        "observation": "#66BB6A",
        "warning": "#EF5350",
    }
    for mt in mark_types:
        vals = [r["mark_counts"].get(mt, 0) for r in rounds]
        ax.plot(
            x,
            vals,
            marker="o",
            markersize=4,
            label=mt.capitalize(),
            color=colors_marks[mt],
            linewidth=2,
        )
    ax.set_ylabel("Cumulative marks")
    ax.set_title("Mark accumulation")
    ax.legend(fontsize=7, ncol=2)
    ax.set_xticks(x)
    ax.set_xticklabels(ROUND_LABELS, fontsize=8)

    # Panel 4: Verdicts per round
    ax = axes[1, 1]
    verdict_colors = {
        "allow": "#4CAF50",
        "conflict": "#FF9800",
        "blocked": "#F44336",
        "denied": "#9E9E9E",
    }
    bottom = np.zeros(len(rounds))
    for v in ["allow", "conflict", "blocked", "denied"]:
        vals = np.array([r["verdicts"].get(v, 0) for r in rounds])
        ax.bar(
            x,
            vals,
            bottom=bottom,
            label=v.capitalize(),
            color=verdict_colors[v],
            width=0.7,
        )
        bottom += vals
    ax.set_ylabel("Verdicts")
    ax.set_title("Verdict distribution per round")
    ax.legend(fontsize=8)
    ax.set_xticks(x)
    ax.set_xticklabels(ROUND_LABELS, fontsize=8)

    fig.tight_layout()
    out = RESULTS_DIR / "fig_round_progression.png"
    fig.savefig(out, dpi=150)
    print(f"Saved: {out}")
    plt.close(fig)


# ── Figure 4: LLM cost scaling ────────────────────────────
def plot_llm_cost_scaling():
    """Prompt tokens per agent vs steps per agent — shows retry-driven cost."""
    fig, ax1 = plt.subplots(figsize=(10, 5))

    x = np.arange(len(rounds))
    prompt_per_agent = [
        r["tokens"]["prompt"] / r["active_agents"] if r["active_agents"] else 0
        for r in rounds
    ]
    steps_per_agent = [
        r["steps"] / r["active_agents"] if r["active_agents"] else 0 for r in rounds
    ]

    # Primary axis: prompt tokens per agent
    color1 = "#1565C0"
    ax1.bar(
        x,
        prompt_per_agent,
        color=color1,
        alpha=0.7,
        width=0.6,
        label="Prompt tokens / agent",
    )
    ax1.set_ylabel("Prompt tokens per agent", color=color1)
    ax1.tick_params(axis="y", labelcolor=color1)
    ax1.set_xticks(x)
    ax1.set_xticklabels(ROUND_LABELS, fontsize=9)

    # Secondary axis: steps per agent
    ax2 = ax1.twinx()
    color2 = "#F44336"
    ax2.plot(
        x,
        steps_per_agent,
        color=color2,
        marker="o",
        markersize=6,
        linewidth=2,
        label="Steps / agent",
    )
    ax2.set_ylabel("Steps per agent", color=color2)
    ax2.tick_params(axis="y", labelcolor=color2)
    ax2.set_ylim(0, 6)

    ax1.set_title("LLM cost per agent: driven by retry steps, not prompt growth")

    # Combined legend
    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2, loc="upper left", fontsize=9)

    fig.tight_layout()
    out = RESULTS_DIR / "fig_llm_cost_scaling.png"
    fig.savefig(out, dpi=150)
    print(f"Saved: {out}")
    plt.close(fig)


if __name__ == "__main__":
    plot_shared_room_contention()
    plot_decay_curves()
    plot_round_progression()
    plot_llm_cost_scaling()
    print("\nAll final figures generated.")
