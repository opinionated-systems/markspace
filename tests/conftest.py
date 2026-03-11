# -*- coding: utf-8 -*-
"""Hypothesis profile configuration for markspace tests."""

from hypothesis import HealthCheck, settings

# CI profile: fast, fewer examples for quick feedback
settings.register_profile(
    "ci",
    max_examples=50,
    suppress_health_check=[HealthCheck.too_slow],
)

# Dev profile: thorough exploration
settings.register_profile(
    "dev",
    max_examples=500,
)

# Default: moderate
settings.register_profile(
    "default",
    max_examples=200,
)

settings.load_profile("default")
