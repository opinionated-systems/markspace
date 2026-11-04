# -*- coding: utf-8 -*-
"""A trial's cost prices its cached prompt tokens at the cached-input rate
where the run recorded them, and every prompt token at the full rate where
it did not."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

# analyze_trials.py is a script; its directory is not a package.
sys.path.insert(0, str(Path(__file__).resolve().parent))
analyze_trials = importlib.import_module("analyze_trials")


def test_cached_prompt_tokens_are_priced_at_the_cached_rate() -> None:
    tokens = {"prompt": 1_000_000, "cached_prompt": 800_000, "completion": 100_000}
    # 200k at $0.15, 800k at $0.03, 100k at $0.50 per million.
    assert analyze_trials.token_cost(tokens, "glm-5p3-flash") == pytest.approx(
        0.03 + 0.024 + 0.05
    )


def test_a_run_without_cached_counts_is_priced_at_the_full_rate() -> None:
    tokens = {"prompt": 1_000_000, "completion": 100_000}
    assert analyze_trials.token_cost(tokens, "gpt-oss-120b") == pytest.approx(
        0.15 + 0.06
    )


def test_a_model_without_a_cached_rate_prices_cached_tokens_in_full() -> None:
    tokens = {"prompt": 1_000_000, "cached_prompt": 800_000, "completion": 0}
    assert analyze_trials.token_cost(tokens, "mercury-2") == pytest.approx(0.25)
