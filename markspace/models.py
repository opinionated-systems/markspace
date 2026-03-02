# -*- coding: utf-8 -*-
"""
Model registry for markspace experiments.

Maps short model names to provider-specific model IDs and endpoints.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelEntry:
    """A model's provider-specific details."""

    model_id: str
    base_url: str
    api_key_env: str  # environment variable name for the API key


# Fireworks models
FIREWORKS_MODELS: dict[str, str] = {
    "kimi-k2p5": "accounts/fireworks/models/kimi-k2p5",
    "deepseek-v3p2": "accounts/fireworks/models/deepseek-v3p2",
    "glm-5": "accounts/fireworks/models/glm-5",
    "minimax-m2p5": "accounts/fireworks/models/minimax-m2p5",
    "gpt-oss-20b": "accounts/fireworks/models/gpt-oss-20b",
    "gpt-oss-120b": "accounts/fireworks/models/gpt-oss-120b",
}

# Non-Fireworks models with their own endpoints
EXTERNAL_MODELS: dict[str, ModelEntry] = {
    "mercury-2": ModelEntry(
        model_id="mercury-2",
        base_url="https://api.inceptionlabs.ai/v1",
        api_key_env="INCEPTION_API_KEY",  # pragma: allowlist secret
    ),
}

DEFAULT_MODELS: list[str] = list(FIREWORKS_MODELS.keys())


def resolve_model_id(short_name: str) -> str:
    """
    Resolve a short model name to a full model ID.

    If the name contains a slash, it's treated as a full model path.
    Otherwise, looks up in FIREWORKS_MODELS, then EXTERNAL_MODELS,
    falling back to accounts/fireworks/models/{short_name}.
    """
    if "/" in short_name:
        return short_name
    if short_name in EXTERNAL_MODELS:
        return EXTERNAL_MODELS[short_name].model_id
    return FIREWORKS_MODELS.get(short_name, f"accounts/fireworks/models/{short_name}")
