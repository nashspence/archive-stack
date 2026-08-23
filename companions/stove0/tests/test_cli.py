from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from lifecycle_events import EventPage
from stove0_cli import main as stove0_cli
from typer.testing import CliRunner

CONFORMANCE_CATALOG = Path(__file__).parents[3] / "companions/stove0/config/recipes.example.yaml"


class FakeClient:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def close(self) -> None:
        pass

    def __getattr__(self, name: str) -> Any:
        if name == "list_events":
            return lambda **_kwargs: EventPage(events=[], next_cursor="0", has_more=False)
        if name == "list_recipes":
            return lambda **_kwargs: {
                "catalog_sha256": "b" * 64,
                "recipes": [{"id": "fixture/v1", "revision": 1, "sha256": "c" * 64}],
            }
        if name == "list_work":
            return lambda **_kwargs: {
                "page": 1,
                "pages": 1,
                "per_page": 1,
                "total": 1,
                "work": [
                    {
                        "work_id": "work-1",
                        "phase": "complete",
                        "revision": 3,
                        "workflow_plan": {"result_kind": "external-effect"},
                        "target_status": {
                            "protocol": "stove0-effect-target/v1",
                            "state": "succeeded",
                            "effect_receipt": {"receipt_sha256": "a" * 64},
                        },
                    }
                ],
            }
        if name == "list_evaluations":
            return lambda **_kwargs: {
                "page": 1,
                "pages": 1,
                "per_page": 1,
                "total": 1,
                "evaluations": [
                    {"evaluation_id": "evaluation-1", "phase": "complete", "revision": 3}
                ],
            }
        if name in {"health_live", "health_ready"}:
            return lambda **_kwargs: {"service": "stove0", "status": "ok"}
        return lambda *_args, **_kwargs: {"operation": name, "status": "ok"}


def _commands(definition: Path) -> dict[str, list[str]]:
    return {
        "health_live": ["health"],
        "list_events": ["event", "list"],
        "list_recipes": ["recipe", "list"],
        "get_recipe": ["recipe", "show", "fixture/v1"],
        "validate_recipe_catalog": ["recipe", "validate", str(CONFORMANCE_CATALOG)],
        "list_work": ["work", "list"],
        "create_work": [
            "work",
            "create",
            "fixture/v1",
            "1",
            "--preview-sha256",
            "a" * 64,
        ],
        "get_work": ["work", "show", "work-1"],
        "inspect_work_coordination": ["work", "coordination", "work-1"],
        "get_artifact_selection": ["selection", "show", "a" * 64],
        "step_work": ["work", "step", "work-1"],
        "retry_work": ["work", "retry", "work-1"],
        "cancel_work": ["work", "cancel", "work-1"],
        "preview_workflow": ["preview", "fixture/v1", "1"],
        "list_evaluations": ["evaluation", "list"],
        "create_evaluation": ["evaluation", "create", str(definition)],
        "get_evaluation": ["evaluation", "show", "evaluation-1"],
        "step_evaluation": ["evaluation", "step", "evaluation-1"],
        "cancel_evaluation": ["evaluation", "cancel", "evaluation-1"],
        "retry_evaluation_variant": ["evaluation", "retry", "evaluation-1", "variant-1"],
        "review_evaluation_variant": [
            "evaluation",
            "review",
            "evaluation-1",
            "variant-1",
            "--rating",
            "4",
        ],
        "scheduler_status": ["scheduler", "status"],
        "run_scheduler": ["scheduler", "run"],
    }


def test_every_stove0_command_executes_in_rich_human_and_stable_json_modes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definition = tmp_path / "evaluation.json"
    definition.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(stove0_cli, "Stove0ApiClient", FakeClient)
    runner = CliRunner()

    for operation, command in _commands(definition).items():
        human = runner.invoke(stove0_cli.app, command)
        assert human.exit_code == 0, (operation, human.output, human.exception)
        assert human.output.strip(), operation

        machine = runner.invoke(stove0_cli.app, ["--json", *command])
        assert machine.exit_code == 0, (operation, machine.output, machine.exception)
        payload = json.loads(machine.stdout)
        assert isinstance(payload, dict), operation


def test_stove0_cli_operation_inventory_matches_the_public_surface(tmp_path: Path) -> None:
    definition = tmp_path / "evaluation.json"
    definition.write_text("{}\n", encoding="utf-8")
    operations = set(_commands(definition))
    assert operations - {"health_live"} == {
        "list_events",
        "list_recipes",
        "get_recipe",
        "validate_recipe_catalog",
        "list_work",
        "create_work",
        "get_work",
        "inspect_work_coordination",
        "get_artifact_selection",
        "step_work",
        "retry_work",
        "cancel_work",
        "preview_workflow",
        "list_evaluations",
        "create_evaluation",
        "get_evaluation",
        "step_evaluation",
        "cancel_evaluation",
        "retry_evaluation_variant",
        "review_evaluation_variant",
        "scheduler_status",
        "run_scheduler",
    }


def test_recipe_validation_reports_only_exact_catalog_identities() -> None:
    runner = CliRunner()

    human = runner.invoke(
        stove0_cli.app,
        ["recipe", "validate", str(CONFORMANCE_CATALOG)],
    )
    result = runner.invoke(
        stove0_cli.app,
        ["--json", "recipe", "validate", str(CONFORMANCE_CATALOG)],
    )

    assert human.exit_code == 0, (human.output, human.exception)
    assert "Catalog:" in human.stdout
    assert "stove0.conformance" in human.stdout
    assert result.exit_code == 0, (result.output, result.exception)
    payload = json.loads(result.stdout)
    assert payload["format"] == "stove0-recipe-catalog-validation/v1"
    assert len(payload["catalog_sha256"]) == 64
    assert payload["recipe_count"] == len(payload["recipes"])
    assert "stove0.conformance-media/v1" in {item["id"] for item in payload["recipes"]}
    assert set(payload) == {
        "catalog_sha256",
        "format",
        "operation_count",
        "recipe_count",
        "recipes",
    }


def test_work_list_rich_and_json_preserve_effect_result_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stove0_cli, "Stove0ApiClient", FakeClient)
    runner = CliRunner()
    human = runner.invoke(stove0_cli.app, ["work", "list"])
    assert human.exit_code == 0
    assert "external-eff" in human.stdout
    assert "succeeded" in human.stdout
    assert "a" * 12 in human.stdout

    machine = runner.invoke(stove0_cli.app, ["--json", "work", "list"])
    assert machine.exit_code == 0
    row = json.loads(machine.stdout)["work"][0]
    assert row["target_status"]["effect_receipt"]["receipt_sha256"] == "a" * 64


def test_work_list_rich_projection_identifies_coordination_settlement() -> None:
    row = {
        "work_id": "nested-work",
        "phase": "complete",
        "branch_set_plan": {"branch_set_sha256": "b" * 64},
        "coordination_settlement": {"settlement_sha256": "c" * 64},
    }

    assert stove0_cli._table_value(row, "result_kind") == "coordination"
    assert stove0_cli._table_value(row, "result_identity") == "c" * 64
