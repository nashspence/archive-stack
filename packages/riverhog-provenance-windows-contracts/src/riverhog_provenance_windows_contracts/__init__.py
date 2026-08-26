"""Portable Windows observation schemas for Riverhog provenance."""

from __future__ import annotations

import json
from importlib import resources
from typing import Any, cast

from riverhog_provenance_contracts import index_schema_documents

PLATFORM_FAMILY = "windows"


def load_schemas() -> dict[str, dict[str, Any]]:
    """Return every Windows observer schema by its canonical identity."""

    documents = (
        cast(dict[str, Any], json.loads(resource.read_text(encoding="utf-8")))
        for resource in resources.files(__package__).joinpath("schemas").iterdir()
        if resource.name.endswith(".schema.json")
    )
    return index_schema_documents(documents, owner="Windows provenance")


__all__ = ["PLATFORM_FAMILY", "load_schemas"]
