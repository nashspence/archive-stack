"""Portable macOS observation schemas for Riverhog provenance."""

from __future__ import annotations

import json
from importlib import resources
from typing import Any, cast

PLATFORM_FAMILY = "macos"


def load_schemas() -> dict[str, dict[str, Any]]:
    """Return every macOS observer schema by its canonical identity."""

    result: dict[str, dict[str, Any]] = {}
    for resource in resources.files(__package__).joinpath("schemas").iterdir():
        if resource.name.endswith(".schema.json"):
            document = cast(dict[str, Any], json.loads(resource.read_text(encoding="utf-8")))
            result[str(document["$id"])] = document
    return result


__all__ = ["PLATFORM_FAMILY", "load_schemas"]
