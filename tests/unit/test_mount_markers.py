from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from riverhog_cli.main import app
from riverhog_core.config_yaml import ConfigError
from riverhog_core.mount_markers import (
    DEFAULT_MARKER_NAME,
    GENERATED_HEADER,
    load_mount_marker_actions,
    render_mount_marker_triggers,
    route_for_marker,
    write_mount_marker,
)

RUNNER = CliRunner()
EXAMPLE_ROOT = Path(__file__).resolve().parents[2] / "config" / "examples" / "mount-marker"
EXAMPLE_CONFIG = EXAMPLE_ROOT / "mount-marker-routes.yaml"


def test_loads_mount_marker_actions_from_public_example() -> None:
    actions = load_mount_marker_actions(EXAMPLE_CONFIG)

    assert [(action.marker, action.script, action.args) for action in actions] == [
        ("example-camera-card", "fake-archive-device", ("example-camera",)),
    ]


def test_render_mount_marker_triggers_removes_only_generated_files(tmp_path: Path) -> None:
    dest_dir = tmp_path / "triggers"
    dest_dir.mkdir()
    custom = dest_dir / "custom"
    old_generated = dest_dir / "old"
    custom.write_text("#!/bin/sh\necho custom\n", encoding="utf-8")
    old_generated.write_text(f"#!/bin/sh\n{GENERATED_HEADER}\necho old\n", encoding="utf-8")

    written = render_mount_marker_triggers(
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
    assert '"$1" example-camera' in text
    assert not (dest_dir / "disabled-example").exists()
    assert not old_generated.exists()
    assert custom.exists()


def test_write_mount_marker_refuses_to_replace_different_route(tmp_path: Path) -> None:
    marker = write_mount_marker(EXAMPLE_CONFIG, "example-camera-card", tmp_path)

    assert marker == tmp_path / DEFAULT_MARKER_NAME
    assert marker.read_text(encoding="utf-8") == "example-camera-card\n"
    assert route_for_marker(EXAMPLE_CONFIG, "example-camera-card") == "example-camera-card"

    marker.write_text("other\n", encoding="utf-8")
    with pytest.raises(FileExistsError):
        write_mount_marker(EXAMPLE_CONFIG, "example-camera-card", tmp_path)

    write_mount_marker(EXAMPLE_CONFIG, "example-camera-card", tmp_path, force=True)
    assert marker.read_text(encoding="utf-8") == "example-camera-card\n"


def test_missing_mount_marker_route_reports_available_routes() -> None:
    with pytest.raises(ConfigError, match="available: example-camera-card"):
        route_for_marker(EXAMPLE_CONFIG, "missing")


def test_mount_marker_cli_lists_renders_and_writes(tmp_path: Path) -> None:
    listed = RUNNER.invoke(app, ["mount-marker", "list", "--config", str(EXAMPLE_CONFIG)])
    assert listed.exit_code == 0
    assert "example-camera-card: fake-archive-device example-camera" in listed.stdout

    dest_dir = tmp_path / "triggers"
    rendered = RUNNER.invoke(
        app,
        [
            "mount-marker",
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
            "mount-marker",
            "write",
            "example-camera-card",
            str(tmp_path),
            "--config",
            str(EXAMPLE_CONFIG),
        ],
    )
    assert written.exit_code == 0
    assert str(tmp_path / DEFAULT_MARKER_NAME) in written.stdout
    assert (tmp_path / DEFAULT_MARKER_NAME).read_text(encoding="utf-8") == (
        "example-camera-card\n"
    )
