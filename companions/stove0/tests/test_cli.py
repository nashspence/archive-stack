from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from lifecycle_events import EventPage
from stove0_cli import main as stove0_cli
from typer.testing import CliRunner


class FakeClient:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def close(self) -> None:
        pass

    def __getattr__(self, name: str) -> Any:
        if name == "list_events":
            return lambda **_kwargs: EventPage(events=[], next_cursor="0", has_more=False)
        if name == "list_recipes":
            return lambda **_kwargs: {"recipes": [{"id": "fixture/v1", "revision": 1}]}
        if name == "list_work":
            return lambda **_kwargs: {
                "page": 1,
                "pages": 1,
                "per_page": 1,
                "total": 1,
                "work": [{"work_id": "work-1", "phase": "complete", "revision": 3}],
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
        "list_work": ["work", "list"],
        "create_work": ["work", "create", "fixture/v1", "1"],
        "get_work": ["work", "show", "work-1"],
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
        "list_work",
        "create_work",
        "get_work",
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
