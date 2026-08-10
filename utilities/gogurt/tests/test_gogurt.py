from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from config_validation import ConfigError
from gogurt.cli import app
from gogurt.core import (
    DEFAULT_GOGURT_MARKER_NAME,
    execute_gogurt_action,
    load_gogurt_actions,
    plan_gogurt_action,
    plan_gogurt_marker,
    route_for_gogurt_marker,
    write_gogurt_marker,
)
from typer.testing import CliRunner

RUNNER = CliRunner()
EXAMPLE_ROOT = Path(__file__).resolve().parents[1] / "config" / "examples"
EXAMPLE_CONFIG = EXAMPLE_ROOT / "gogurt-routes.yaml"


def test_loads_portable_gogurt_actions_from_public_example() -> None:
    actions = load_gogurt_actions(EXAMPLE_CONFIG)

    assert [(action.route, action.command) for action in actions] == [
        (
            "example-camera-card",
            (
                "{python}",
                "{config_dir}/scripts/fake_archive_device.py",
                "{mount_point}",
                "example-camera",
            ),
        ),
    ]


def test_plans_and_executes_one_direct_argv_action(tmp_path: Path) -> None:
    mount = tmp_path / "mount"
    mount.mkdir()
    write_gogurt_marker(EXAMPLE_CONFIG, "example-camera-card", mount)

    plan = plan_gogurt_action(EXAMPLE_CONFIG, mount)

    assert plan == {
        "status": "ready",
        "route": "example-camera-card",
        "mount_point": str(mount.resolve()),
        "marker": str((mount / DEFAULT_GOGURT_MARKER_NAME).resolve()),
        "marker_name": DEFAULT_GOGURT_MARKER_NAME,
        "command": [
            sys.executable,
            str((EXAMPLE_ROOT / "scripts" / "fake_archive_device.py").resolve()),
            str(mount.resolve()),
            "example-camera",
        ],
    }
    completed = execute_gogurt_action(plan, capture_output=True)
    assert completed.returncode == 0
    assert completed.stdout == f"archive example-camera from {mount.resolve()}\n"
    assert completed.stderr == ""


def test_action_plan_reports_an_unmarked_mount(tmp_path: Path) -> None:
    plan = plan_gogurt_action(EXAMPLE_CONFIG, tmp_path)

    assert plan["status"] == "unmarked"
    assert plan["mount_point"] == str(tmp_path.resolve())
    assert plan["marker"] == str((tmp_path / DEFAULT_GOGURT_MARKER_NAME).resolve())


def test_action_directory_resolves_private_style_commands(tmp_path: Path) -> None:
    config = tmp_path / "gogurt-routes.yaml"
    mount = tmp_path / "mount"
    actions = tmp_path / "actions"
    mount.mkdir()
    actions.mkdir()
    executable = actions / "archive-camera"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    config.write_text(
        (
            "schema_version: 1\n"
            "kind: gogurt.routes\n"
            "routes:\n"
            "  camera:\n"
            "    command:\n"
            "      - archive-camera\n"
            '      - "{mount_point}"\n'
        ),
        encoding="utf-8",
    )
    (mount / DEFAULT_GOGURT_MARKER_NAME).write_text("camera\n", encoding="utf-8")

    plan = plan_gogurt_action(config, mount, actions_dir=actions)

    assert plan["command"] == [str(executable.resolve()), str(mount.resolve())]


def test_route_command_requires_one_mount_point(tmp_path: Path) -> None:
    config = tmp_path / "gogurt-routes.yaml"
    config.write_text(
        (
            "schema_version: 1\n"
            "kind: gogurt.routes\n"
            "routes:\n"
            "  camera:\n"
            "    command:\n"
            "      - camera-action\n"
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="must contain one .*mount_point.* token"):
        load_gogurt_actions(config)


def test_write_gogurt_marker_refuses_to_replace_different_route(tmp_path: Path) -> None:
    marker = write_gogurt_marker(EXAMPLE_CONFIG, "example-camera-card", tmp_path)

    assert marker == tmp_path / DEFAULT_GOGURT_MARKER_NAME
    assert marker.read_text(encoding="utf-8") == "example-camera-card\n"
    assert route_for_gogurt_marker(EXAMPLE_CONFIG, "example-camera-card") == ("example-camera-card")

    marker.write_text("other\n", encoding="utf-8")
    with pytest.raises(FileExistsError):
        write_gogurt_marker(EXAMPLE_CONFIG, "example-camera-card", tmp_path)

    write_gogurt_marker(EXAMPLE_CONFIG, "example-camera-card", tmp_path, force=True)
    assert marker.read_text(encoding="utf-8") == "example-camera-card\n"


def test_plan_gogurt_marker_does_not_write(tmp_path: Path) -> None:
    plan = plan_gogurt_marker(EXAMPLE_CONFIG, "example-camera-card", tmp_path)

    assert plan["dry_run"] is True
    assert plan["status"] == "would_write"
    assert plan["route"] == "example-camera-card"
    assert not (tmp_path / DEFAULT_GOGURT_MARKER_NAME).exists()


def test_plan_gogurt_marker_reports_invalid_marker_name(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="invalid gogurt marker name"):
        plan_gogurt_marker(
            EXAMPLE_CONFIG,
            "example-camera-card",
            tmp_path,
            marker_name="nested/.gogurt",
        )


def test_missing_gogurt_route_reports_available_routes() -> None:
    with pytest.raises(ConfigError, match="available: example-camera-card"):
        route_for_gogurt_marker(EXAMPLE_CONFIG, "missing")


def test_gogurt_cli_lists_runs_and_writes(tmp_path: Path) -> None:
    listed = RUNNER.invoke(app, ["list", "--config", str(EXAMPLE_CONFIG), "--json"])
    assert listed.exit_code == 0
    assert json.loads(listed.stdout) == [
        {
            "route": "example-camera-card",
            "command": [
                "{python}",
                "{config_dir}/scripts/fake_archive_device.py",
                "{mount_point}",
                "example-camera",
            ],
        }
    ]

    written = RUNNER.invoke(
        app,
        [
            "write",
            "example-camera-card",
            str(tmp_path),
            "--config",
            str(EXAMPLE_CONFIG),
        ],
    )
    assert written.exit_code == 0
    assert str(tmp_path / DEFAULT_GOGURT_MARKER_NAME) in written.stdout

    planned = RUNNER.invoke(
        app,
        ["run", str(tmp_path), "--config", str(EXAMPLE_CONFIG), "--dry-run"],
    )
    assert planned.exit_code == 0
    assert "gogurt action available" in planned.stderr
    assert "gogurt would run" in planned.stderr

    awaiting = RUNNER.invoke(
        app,
        ["run", str(tmp_path), "--config", str(EXAMPLE_CONFIG)],
    )
    assert awaiting.exit_code == 0
    assert "gogurt action awaiting confirmation" in awaiting.stderr

    autorun = RUNNER.invoke(
        app,
        ["run", str(tmp_path), "--config", str(EXAMPLE_CONFIG), "--autorun"],
    )
    assert autorun.exit_code == 0
    assert "gogurt launching" in autorun.stderr


def test_gogurt_cli_write_dry_run_does_not_create_marker(tmp_path: Path) -> None:
    result = RUNNER.invoke(
        app,
        [
            "write",
            "example-camera-card",
            str(tmp_path),
            "--config",
            str(EXAMPLE_CONFIG),
            "--dry-run",
        ],
    )

    assert result.exit_code == 0
    assert "gogurt write dry-run" in result.stdout
    assert "status: would_write" in result.stdout
    assert not (tmp_path / DEFAULT_GOGURT_MARKER_NAME).exists()
