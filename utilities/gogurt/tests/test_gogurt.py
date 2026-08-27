from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import gogurt.cli as cli
import pytest
from config_validation import ConfigError
from gogurt.cli import app
from gogurt_core.core import (
    DEFAULT_GOGURT_MARKER_NAME,
    MAX_GOGURT_MARKER_BYTES,
    execute_gogurt_action,
    load_gogurt_actions,
    plan_gogurt_action,
    plan_gogurt_marker,
    route_for_gogurt_marker,
    validate_gogurt_marker_name,
    write_gogurt_marker,
)
from gogurt_core.providers import GogurtProviderReference
from gogurt_listener_runtime.listener import ListenerError
from typer.testing import CliRunner

from tests.gogurt_provider import path_mounted_volume_provider

RUNNER = CliRunner()
FIXTURE_ROOT = Path(__file__).resolve().parents[3] / "qualification/fixtures/gogurt"
FIXTURE_CONFIG = FIXTURE_ROOT / "gogurt-routes.yaml"
PROVIDER = path_mounted_volume_provider()


@pytest.fixture(autouse=True)
def _mounted_volume_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_resolve_mounted_volume", lambda _name: PROVIDER)


def test_console_main_reports_listener_errors_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail() -> None:
        raise ListenerError("native lifecycle failed")

    monkeypatch.setattr(cli, "app", fail)
    monkeypatch.setattr(sys, "argv", ["gogurt", "listener", "install"])

    with pytest.raises(SystemExit) as raised:
        cli.main()

    captured = capsys.readouterr()
    assert raised.value.code == 1
    assert captured.err == "gogurt: native lifecycle failed\n"
    assert captured.out == ""


def test_loads_portable_gogurt_actions_from_qualification_fixture() -> None:
    actions = load_gogurt_actions(FIXTURE_CONFIG)

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
    write_gogurt_marker(FIXTURE_CONFIG, "example-camera-card", mount, provider=PROVIDER)

    plan = plan_gogurt_action(FIXTURE_CONFIG, mount, provider=PROVIDER)

    assert plan == {
        "status": "ready",
        "route": "example-camera-card",
        "mount_point": str(mount.resolve()),
        "marker": str((mount / DEFAULT_GOGURT_MARKER_NAME).resolve()),
        "marker_name": DEFAULT_GOGURT_MARKER_NAME,
        "mounted_volume_provider": PROVIDER.reference.as_dict(),
        "command": [
            sys.executable,
            str((FIXTURE_ROOT / "scripts" / "fake_archive_device.py").resolve()),
            str(mount.resolve()),
            "example-camera",
        ],
        "marker_identity": plan["marker_identity"],
    }
    completed = execute_gogurt_action(plan, provider=PROVIDER, capture_output=True)
    assert completed.returncode == 0
    assert completed.stdout == f"archive example-camera from {mount.resolve()}\n"
    assert completed.stderr == ""


@pytest.mark.skipif(os.name == "nt", reason="POSIX portable marker mode")
def test_marker_is_portably_readable_nonsecret_metadata(tmp_path: Path) -> None:
    write_gogurt_marker(FIXTURE_CONFIG, "example-camera-card", tmp_path, provider=PROVIDER)

    marker = tmp_path / DEFAULT_GOGURT_MARKER_NAME
    assert stat.S_IMODE(marker.stat().st_mode) == 0o644


def test_action_plan_reports_an_unmarked_mount(tmp_path: Path) -> None:
    plan = plan_gogurt_action(FIXTURE_CONFIG, tmp_path, provider=PROVIDER)

    assert plan["status"] == "unmarked"
    assert plan["mount_point"] == str(tmp_path.resolve())
    assert plan["marker"] == str((tmp_path / DEFAULT_GOGURT_MARKER_NAME).resolve())


def test_action_plan_rejects_unsafe_markers(tmp_path: Path) -> None:
    marker = tmp_path / DEFAULT_GOGURT_MARKER_NAME
    target = tmp_path / "route.txt"
    target.write_text("example-camera-card\n", encoding="utf-8")
    marker.symlink_to(target)
    with pytest.raises(ConfigError, match="regular file"):
        plan_gogurt_action(FIXTURE_CONFIG, tmp_path, provider=PROVIDER)

    marker.unlink()
    marker.write_bytes(b"x" * (MAX_GOGURT_MARKER_BYTES + 1))
    with pytest.raises(ConfigError, match="exceeds"):
        plan_gogurt_action(FIXTURE_CONFIG, tmp_path, provider=PROVIDER)

    marker.write_bytes(b"\xff\n")
    with pytest.raises(ConfigError, match="strict UTF-8"):
        plan_gogurt_action(FIXTURE_CONFIG, tmp_path, provider=PROVIDER)

    marker.write_text(" example-camera-card\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="surrounding whitespace"):
        plan_gogurt_action(FIXTURE_CONFIG, tmp_path, provider=PROVIDER)


def test_action_execution_revalidates_marker_identity(tmp_path: Path) -> None:
    write_gogurt_marker(FIXTURE_CONFIG, "example-camera-card", tmp_path, provider=PROVIDER)
    plan = plan_gogurt_action(FIXTURE_CONFIG, tmp_path, provider=PROVIDER)
    marker = tmp_path / DEFAULT_GOGURT_MARKER_NAME
    replacement = tmp_path / ".replacement"
    replacement.write_text("example-camera-card\n", encoding="utf-8")
    os.replace(replacement, marker)

    with pytest.raises(ConfigError, match="changed before action execution"):
        execute_gogurt_action(plan, provider=PROVIDER)


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

    plan = plan_gogurt_action(config, mount, provider=PROVIDER, actions_dir=actions)

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


def test_route_command_rejects_mount_directory_as_the_executable(tmp_path: Path) -> None:
    config = tmp_path / "gogurt-routes.yaml"
    config.write_text(
        (
            "schema_version: 1\n"
            "kind: gogurt.routes\n"
            "routes:\n"
            "  camera:\n"
            "    command:\n"
            '      - "{mount_point}"\n'
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="cannot use .*mount_point.* as its executable"):
        load_gogurt_actions(config)


def test_write_gogurt_marker_refuses_to_replace_different_route(tmp_path: Path) -> None:
    marker = write_gogurt_marker(
        FIXTURE_CONFIG,
        "example-camera-card",
        tmp_path,
        provider=PROVIDER,
    )

    assert marker == tmp_path / DEFAULT_GOGURT_MARKER_NAME
    assert marker.read_text(encoding="utf-8") == "example-camera-card\n"
    assert route_for_gogurt_marker(FIXTURE_CONFIG, "example-camera-card") == ("example-camera-card")

    marker.write_text("other\n", encoding="utf-8")
    with pytest.raises(FileExistsError):
        write_gogurt_marker(
            FIXTURE_CONFIG,
            "example-camera-card",
            tmp_path,
            provider=PROVIDER,
        )

    write_gogurt_marker(
        FIXTURE_CONFIG,
        "example-camera-card",
        tmp_path,
        provider=PROVIDER,
        force=True,
    )
    assert marker.read_text(encoding="utf-8") == "example-camera-card\n"


def test_same_route_marker_write_is_an_identity_preserving_noop(tmp_path: Path) -> None:
    marker = write_gogurt_marker(
        FIXTURE_CONFIG,
        "example-camera-card",
        tmp_path,
        provider=PROVIDER,
    )
    before = marker.stat()
    before_plan = plan_gogurt_action(FIXTURE_CONFIG, tmp_path, provider=PROVIDER)

    preview = plan_gogurt_marker(
        FIXTURE_CONFIG,
        "example-camera-card",
        tmp_path,
        provider=PROVIDER,
    )
    repeated = write_gogurt_marker(
        FIXTURE_CONFIG,
        "example-camera-card",
        tmp_path,
        provider=PROVIDER,
    )

    after = marker.stat()
    after_plan = plan_gogurt_action(FIXTURE_CONFIG, tmp_path, provider=PROVIDER)
    assert preview["status"] == "would_keep"
    assert repeated == marker
    assert marker.read_bytes() == b"example-camera-card\n"
    assert (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) == (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    assert after_plan["marker_identity"] == before_plan["marker_identity"]


@pytest.mark.parametrize(
    "marker_name",
    ["CON", "con.txt", "bad name", "marker.", "märk", "x" * 256],
)
def test_marker_names_follow_the_portable_filename_contract(marker_name: str) -> None:
    with pytest.raises(ConfigError, match="invalid gogurt marker name"):
        validate_gogurt_marker_name(marker_name)

    validate_gogurt_marker_name(DEFAULT_GOGURT_MARKER_NAME)


@pytest.mark.parametrize("route_name", ["Camera", "camera_card", "-camera", "camera-"])
def test_route_identifiers_must_be_canonical_slugs(tmp_path: Path, route_name: str) -> None:
    config = tmp_path / "routes.yaml"
    config.write_text(
        "schema_version: 1\n"
        "kind: gogurt.routes\n"
        "routes:\n"
        f"  {json.dumps(route_name)}:\n"
        "    command:\n"
        "      - echo\n"
        '      - "{mount_point}"\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigError):
        load_gogurt_actions(config)


def test_write_gogurt_marker_refuses_a_symlink_target(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("example-camera-card\n", encoding="utf-8")
    (tmp_path / DEFAULT_GOGURT_MARKER_NAME).symlink_to(target)

    with pytest.raises(ConfigError, match="regular file"):
        write_gogurt_marker(
            FIXTURE_CONFIG,
            "example-camera-card",
            tmp_path,
            provider=PROVIDER,
            force=True,
        )


def test_write_gogurt_marker_uses_exclusive_temporary_creation(tmp_path: Path) -> None:
    protected = tmp_path / "protected"
    protected.write_text("unchanged\n", encoding="utf-8")
    former_temporary = tmp_path / (f".{DEFAULT_GOGURT_MARKER_NAME}.{os.getpid()}.tmp")
    former_temporary.symlink_to(protected)

    marker = write_gogurt_marker(
        FIXTURE_CONFIG,
        "example-camera-card",
        tmp_path,
        provider=PROVIDER,
    )

    assert marker.read_text(encoding="utf-8") == "example-camera-card\n"
    assert protected.read_text(encoding="utf-8") == "unchanged\n"
    assert former_temporary.is_symlink()


def test_write_gogurt_marker_accepts_the_exact_portable_component_limit(
    tmp_path: Path,
) -> None:
    marker_name = "m" * 255

    marker = write_gogurt_marker(
        FIXTURE_CONFIG,
        "example-camera-card",
        tmp_path,
        provider=PROVIDER,
        marker_name=marker_name,
    )

    assert marker.name == marker_name
    assert marker.read_bytes() == b"example-camera-card\n"


def test_plan_gogurt_marker_does_not_write(tmp_path: Path) -> None:
    plan = plan_gogurt_marker(
        FIXTURE_CONFIG,
        "example-camera-card",
        tmp_path,
        provider=PROVIDER,
    )

    assert plan["dry_run"] is True
    assert plan["status"] == "would_write"
    assert plan["route"] == "example-camera-card"
    assert not (tmp_path / DEFAULT_GOGURT_MARKER_NAME).exists()


def test_plan_gogurt_marker_reports_invalid_marker_name(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="invalid gogurt marker name"):
        plan_gogurt_marker(
            FIXTURE_CONFIG,
            "example-camera-card",
            tmp_path,
            provider=PROVIDER,
            marker_name="nested/.gogurt",
        )


def test_missing_gogurt_route_reports_available_routes() -> None:
    with pytest.raises(ConfigError, match="available: example-camera-card"):
        route_for_gogurt_marker(FIXTURE_CONFIG, "missing")


def test_gogurt_cli_lists_runs_and_writes(tmp_path: Path) -> None:
    listed = RUNNER.invoke(app, ["list", "--config", str(FIXTURE_CONFIG), "--json"])
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
            str(FIXTURE_CONFIG),
        ],
    )
    assert written.exit_code == 0
    assert str(tmp_path / DEFAULT_GOGURT_MARKER_NAME) in written.stdout

    planned = RUNNER.invoke(
        app,
        ["run", str(tmp_path), "--config", str(FIXTURE_CONFIG), "--dry-run"],
    )
    assert planned.exit_code == 0
    assert "gogurt action available" in planned.stderr
    assert "gogurt would run" in planned.stderr

    awaiting = RUNNER.invoke(
        app,
        ["run", str(tmp_path), "--config", str(FIXTURE_CONFIG)],
    )
    assert awaiting.exit_code == 0
    assert "gogurt action awaiting confirmation" in awaiting.stderr

    autorun = RUNNER.invoke(
        app,
        ["run", str(tmp_path), "--config", str(FIXTURE_CONFIG), "--autorun"],
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
            str(FIXTURE_CONFIG),
            "--dry-run",
        ],
    )

    assert result.exit_code == 0
    assert "gogurt write dry-run" in result.stdout
    assert "status: would_write" in result.stdout
    assert not (tmp_path / DEFAULT_GOGURT_MARKER_NAME).exists()


def test_listener_cli_has_matching_human_and_json_lifecycle_surfaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mounted_volume_reference = GogurtProviderReference(
        kind="mounted-volume",
        name="fixture-mount",
        provider_id="fixture-mounted-volume-provider/v1",
    )
    host_reference = GogurtProviderReference(
        kind="listener-host",
        name="fixture-host",
        provider_id="fixture-listener-host-provider/v1",
    )
    mounted_volume = SimpleNamespace(reference=mounted_volume_reference, discover=lambda: ())
    host = SimpleNamespace(
        reference=host_reference,
        paths=lambda: object(),
        adapter=lambda: object(),
        executable=lambda: Path(sys.executable),
    )
    payload = {
        "schema": "gogurt-listener-status/v1",
        "version": "1.0.0",
        "platform": "linux",
        "installed": True,
        "enabled": True,
        "running": True,
        "health": "healthy",
        "diagnostic": None,
    }
    monkeypatch.setattr("gogurt.cli.listener_status", lambda **_kwargs: payload)
    monkeypatch.setattr("gogurt.cli.start_listener", lambda **_kwargs: payload)
    monkeypatch.setattr("gogurt.cli.stop_listener", lambda **_kwargs: payload)
    monkeypatch.setattr("gogurt.cli.restart_listener", lambda **_kwargs: payload)
    monkeypatch.setattr("gogurt.cli.uninstall_listener", lambda **_kwargs: payload)
    monkeypatch.setattr("gogurt.cli.install_listener", lambda *_args, **_kwargs: payload)
    monkeypatch.setattr("gogurt.cli._resolve_mounted_volume", lambda _name: mounted_volume)
    monkeypatch.setattr("gogurt.cli._resolve_listener_host", lambda _name: host)
    monkeypatch.setattr(
        "gogurt.cli._installed_listener_composition",
        lambda _name: (host, mounted_volume),
    )
    expected = payload | {
        "mounted_volume_provider": mounted_volume_reference.as_dict(),
        "listener_host_provider": host_reference.as_dict(),
    }

    for command in ("status", "start", "stop", "restart", "uninstall"):
        args = ["listener", command, "--listener-host-provider", "fixture-host"]
        human = RUNNER.invoke(app, args)
        structured = RUNNER.invoke(app, [*args, "--json"])
        assert human.exit_code == 0
        assert "health: healthy" in human.stdout
        assert structured.exit_code == 0
        assert json.loads(structured.stdout) == expected

    installed = RUNNER.invoke(
        app,
        [
            "listener",
            "install",
            "--config",
            str(FIXTURE_CONFIG),
            "--autorun",
            "--mounted-volume-provider",
            "fixture-mount",
            "--listener-host-provider",
            "fixture-host",
            "--json",
        ],
    )
    assert installed.exit_code == 0
    assert json.loads(installed.stdout) == expected

    failed_payload = payload | {
        "health": "failed",
        "diagnostic": "global configuration: ConfigError: routes are invalid",
    }
    monkeypatch.setattr("gogurt.cli.listener_status", lambda **_kwargs: failed_payload)
    status_args = ["listener", "status", "--listener-host-provider", "fixture-host"]
    human_failure = RUNNER.invoke(app, status_args)
    json_failure = RUNNER.invoke(app, [*status_args, "--json"])
    assert human_failure.exit_code == 0
    assert "health: failed" in human_failure.stdout
    assert "diagnostic: global configuration" in human_failure.stdout
    assert json.loads(json_failure.stdout) == expected | {
        "health": "failed",
        "diagnostic": "global configuration: ConfigError: routes are invalid",
    }

    refused = RUNNER.invoke(
        app,
        ["listener", "install", "--config", str(FIXTURE_CONFIG)],
    )
    assert refused.exit_code == 1
    assert isinstance(refused.exception, ConfigError)
