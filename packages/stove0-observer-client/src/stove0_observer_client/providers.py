"""Explicit composition of installed observer semantic-validator providers."""

from __future__ import annotations

import importlib.metadata
from collections.abc import Sequence

from stove0_observer_protocol import SemanticValidatorBinding, SemanticValidatorRegistry

SEMANTIC_VALIDATOR_ENTRY_POINT_GROUP = "stove0.observer-semantic-validators"


def load_semantic_validator_registry(
    provider_names: Sequence[str],
) -> SemanticValidatorRegistry:
    """Load only the explicitly enabled installed contract providers."""

    normalized = tuple(name.strip() for name in provider_names)
    if any(not name for name in normalized) or len(normalized) != len(set(normalized)):
        raise ValueError("semantic validator provider names must be nonempty and unique")
    available: dict[str, list[importlib.metadata.EntryPoint]] = {}
    for entry_point in importlib.metadata.entry_points(group=SEMANTIC_VALIDATOR_ENTRY_POINT_GROUP):
        available.setdefault(entry_point.name, []).append(entry_point)
    bindings: list[SemanticValidatorBinding] = []
    for name in normalized:
        matches = available.get(name, [])
        if len(matches) != 1:
            raise ValueError(f"semantic validator provider must resolve exactly once: {name}")
        binding = matches[0].load()
        if not isinstance(binding, SemanticValidatorBinding):
            raise TypeError(f"semantic validator provider has an invalid binding: {name}")
        bindings.append(binding)
    return SemanticValidatorRegistry(bindings)


__all__ = ["SEMANTIC_VALIDATOR_ENTRY_POINT_GROUP", "load_semantic_validator_registry"]
