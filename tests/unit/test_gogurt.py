from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gogurt.cli import app
from gogurt.core import (
    DEFAULT_GOGURT_MARKER_NAME,
    GENERATED_HEADER,
    GOGURT_EMOJI,
    load_gogurt_actions,
    plan_gogurt_marker,
    render_gogurt_triggers,
    route_for_gogurt_marker,
    write_gogurt_marker,
)
from riverhog_core.config_yaml import ConfigError

RUNNER = CliRunner()
EXAMPLE_ROOT = Path(__file__).resolve().parents[2] / "config" / "examples" / "gogurt"
EXAMPLE_CONFIG = EXAMPLE_ROOT / "gogurt-routes.yaml"


def test_loads_gogurt_actions_from_public_example() -> None:
    actions = load_gogurt_actions(EXAMPLE_CONFIG)

    assert [(action.route, action.script, action.args) for action in actions] == [
        ("example-camera-card", "fake-archive-device", ("example-camera",)),
    ]


def test_render_gogurt_triggers_removes_only_generated_files(tmp_path: Path) -> None:
    dest_dir = tmp_path / "triggers"
    dest_dir.mkdir()
    custom = dest_dir / "custom"
    old_generated = dest_dir / "old"
    custom.write_text("#!/bin/sh\necho custom\n", encoding="utf-8")
    old_generated.write_text(f"#!/bin/sh\n{GENERATED_HEADER}\necho old\n", encoding="utf-8")

    written = render_gogurt_triggers(
        EXAMPLE_CONFIG,
        dest_dir,
        EXAMPLE_ROOT / "scripts",
    )

    assert [path.name for path in written] == ["example-camera-card"]
    trigger = dest_dir / "example-camera-card"
    assert trigger.exists()
    assert os.access(trigger, os.X_OK)
    text = trigger.read_text(encoding="utf-8")
    assert str(EXAMPLE_ROOT / "scripts" / "fake-archive-device") in text
    assert GOGURT_EMOJI in text
    assert "gogurt launch available" in text
    assert "gogurt run this action? [y/N]" in text
    assert '"$1" example-camera' in text
    default_run = subprocess.run(
        [str(trigger), str(tmp_path / "mount")],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "gogurt launch available" in default_run.stderr
    assert "gogurt launch waiting for confirmation" in default_run.stderr
    assert default_run.stdout == ""
    autorun = subprocess.run(
        [str(trigger), str(tmp_path / "mount")],
        check=True,
        capture_output=True,
        env={**os.environ, "GOGURT_AUTORUN": "1"},
        text=True,
    )
    assert (
        f"{GOGURT_EMOJI} gogurt launching: "
        "route=example-camera-card action=fake-archive-device"
    ) in autorun.stderr
    assert f"mount={tmp_path / 'mount'}" in autorun.stderr
    assert f"archive example-camera from {tmp_path / 'mount'}" in autorun.stdout
    assert not (dest_dir / "disabled-example").exists()
    assert not old_generated.exists()
    assert custom.exists()


def test_write_gogurt_marker_refuses_to_replace_different_route(tmp_path: Path) -> None:
    marker = write_gogurt_marker(EXAMPLE_CONFIG, "example-camera-card", tmp_path)

    assert marker == tmp_path / DEFAULT_GOGURT_MARKER_NAME
    assert marker.read_text(encoding="utf-8") == "example-camera-card\n"
    assert route_for_gogurt_marker(EXAMPLE_CONFIG, "example-camera-card") == "example-camera-card"

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


def test_gogurt_cli_lists_renders_and_writes(tmp_path: Path) -> None:
    listed = RUNNER.invoke(app, ["list", "--config", str(EXAMPLE_CONFIG)])
    assert listed.exit_code == 0
    assert "example-camera-card: fake-archive-device example-camera" in listed.stdout

    dest_dir = tmp_path / "triggers"
    rendered = RUNNER.invoke(
        app,
        [
            "render",
            "--config",
            str(EXAMPLE_CONFIG),
            "--dest-dir",
            str(dest_dir),
            "--scripts-dir",
            str(EXAMPLE_ROOT / "scripts"),
        ],
    )
    assert rendered.exit_code == 0
    assert str(dest_dir / "example-camera-card") in rendered.stdout

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
    assert (tmp_path / DEFAULT_GOGURT_MARKER_NAME).read_text(encoding="utf-8") == (
        "example-camera-card\n"
    )


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
