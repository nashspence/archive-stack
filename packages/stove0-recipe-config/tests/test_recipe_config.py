from __future__ import annotations

from pathlib import Path

import pytest
from config_validation import ConfigError
from stove0_recipe_config import RecipeCatalog


def test_checked_example_is_a_portable_content_addressed_catalog() -> None:
    path = Path(__file__).parents[3] / "companions/stove0/config/recipes.example.yaml"

    catalog = RecipeCatalog.load(path)
    document = catalog.validation_document()

    assert document["catalog_sha256"] == catalog.sha256
    assert document["recipe_count"] == len(catalog.recipes)
    assert document["operation_count"] == len(catalog.operations)
    assert "stove0.conformance-media/v1" in {recipe.id for recipe in catalog.recipes}
    assert RecipeCatalog.model_json_schema()["additionalProperties"] is False


def test_catalog_loader_rejects_ambiguous_duplicate_yaml_keys(tmp_path: Path) -> None:
    path = tmp_path / "recipes.yaml"
    path.write_text("format: stove0-recipes/v1\nformat: stove0-recipes/v1\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="duplicate key 'format'"):
        RecipeCatalog.load(path)
