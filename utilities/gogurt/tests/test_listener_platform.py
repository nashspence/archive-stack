from __future__ import annotations

import plistlib
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest
from gogurt.listener_platform import (
    LISTENER_LABEL,
    LaunchdUserAdapter,
    ListenerPaths,
    SystemdUserAdapter,
    TaskSchedulerUserAdapter,
    default_listener_paths,
    render_launchd_plist,
    render_systemd_unit,
    resolve_listener_executable,
)


def _paths(tmp_path: Path, registration: Path | None) -> ListenerPaths:
    state = tmp_path / "state"
    return ListenerPaths(
        state_dir=state,
        config_file=state / "listener.json",
        database_file=state / "listener.sqlite3",
        heartbeat_file=state / "heartbeat.json",
        lock_file=state / "listener.lock",
        log_file=state / "listener.log",
        registration_file=registration,
    )


def test_listener_paths_follow_each_user_platform_convention(tmp_path: Path) -> None:
    linux = default_listener_paths(
        platform="linux",
        environment={
            "XDG_STATE_HOME": str(tmp_path / "linux-state"),
            "XDG_CONFIG_HOME": str(tmp_path / "linux-config"),
        },
        home=tmp_path,
    )
    assert linux.state_dir == tmp_path / "linux-state" / "gogurt"
    assert linux.registration_file == (
        tmp_path / "linux-config" / "systemd" / "user" / "gogurt-listener.service"
    )

    macos = default_listener_paths(platform="darwin", environment={}, home=tmp_path)
    assert macos.state_dir == tmp_path / "Library" / "Application Support" / "Gogurt"
    assert macos.registration_file == (
        tmp_path / "Library" / "LaunchAgents" / f"{LISTENER_LABEL}.plist"
    )

    windows = default_listener_paths(
        platform="win32",
        environment={"LOCALAPPDATA": str(tmp_path / "LocalAppData")},
        home=tmp_path,
    )
    assert windows.state_dir == tmp_path / "LocalAppData" / "Gogurt"
    assert windows.registration_file is None


def test_windows_resolves_the_uv_console_launcher_extension(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launcher = tmp_path / "gogurt.exe"
    launcher.write_text("fixture", encoding="utf-8")
    monkeypatch.setattr("gogurt.listener_platform.sys.platform", "win32")
    monkeypatch.setenv("PATHEXT", ".exe;.cmd")

    assert resolve_listener_executable(str(tmp_path / "gogurt")) == launcher.resolve()


def test_native_registrations_bind_only_the_absolute_installed_command() -> None:
    command = (
        "/opt/gogurt/bin/gogurt",
        "listener",
        "_run",
        "--runtime-config",
        "/home/person/.local/state/gogurt/listener.json",
    )
    unit = render_systemd_unit(command).decode()
    assert 'ExecStart="/opt/gogurt/bin/gogurt" "listener" "_run"' in unit
    assert "WantedBy=default.target" in unit
    assert "PATH=" not in unit

    plist = plistlib.loads(render_launchd_plist(command))
    assert plist["ProgramArguments"] == list(command)
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] == {"SuccessfulExit": False}
    assert plist["StandardOutPath"] == "/dev/null"
    assert plist["StandardErrorPath"] == "/dev/null"


class RecordingSystemdAdapter(SystemdUserAdapter):
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def _run(
        self,
        command: Sequence[str],
        *,
        allowed: frozenset[int] = frozenset({0}),
    ) -> subprocess.CompletedProcess[str]:
        del allowed
        self.commands.append(list(command))
        return subprocess.CompletedProcess(list(command), 0, "active\n", "")


def test_systemd_registration_enables_login_resume_and_cleans_up(tmp_path: Path) -> None:
    registration = tmp_path / "config" / "gogurt-listener.service"
    paths = _paths(tmp_path, registration)
    adapter = RecordingSystemdAdapter()
    command = (str(tmp_path / "gogurt"), "listener", "_run")

    adapter.register(paths, command)
    assert registration.is_file()
    assert ["systemctl", "--user", "enable", registration.name] in adapter.commands
    assert ["systemctl", "--user", "restart", registration.name] in adapter.commands
    assert adapter.status(paths).running is True

    adapter.unregister(paths)
    assert not registration.exists()
    assert ["systemctl", "--user", "disable", "--now", registration.name] in adapter.commands


class RecordingLaunchdAdapter(LaunchdUserAdapter):
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    @staticmethod
    def _domain() -> str:
        return "gui/501"

    def _run(
        self,
        command: Sequence[str],
        *,
        allowed: frozenset[int] = frozenset({0}),
    ) -> subprocess.CompletedProcess[str]:
        del allowed
        self.commands.append(list(command))
        return subprocess.CompletedProcess(list(command), 0, "", "")


def test_launchd_registration_bootstraps_and_removes_the_agent(tmp_path: Path) -> None:
    registration = tmp_path / "Library" / "LaunchAgents" / f"{LISTENER_LABEL}.plist"
    paths = _paths(tmp_path, registration)
    adapter = RecordingLaunchdAdapter()
    command = (str(tmp_path / "gogurt"), "listener", "_run")

    adapter.register(paths, command)
    assert registration.is_file()
    assert ["launchctl", "bootstrap", "gui/501", str(registration)] in adapter.commands

    adapter.unregister(paths)
    assert not registration.exists()


class RecordingTaskAdapter(TaskSchedulerUserAdapter):
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def _run(
        self,
        command: Sequence[str],
        *,
        allowed: frozenset[int] = frozenset({0}),
    ) -> subprocess.CompletedProcess[str]:
        del allowed
        self.commands.append(list(command))
        return subprocess.CompletedProcess(list(command), 0, "Status: Running\n", "")


def test_task_registration_uses_current_user_onlogon_without_elevation(tmp_path: Path) -> None:
    paths = _paths(tmp_path, None)
    adapter = RecordingTaskAdapter()
    command = (str(tmp_path / "Gogurt Tool" / "gogurt.exe"), "listener", "_run")

    adapter.register(paths, command)
    create = adapter.commands[1]
    assert create[:4] == ["schtasks.exe", "/Create", "/TN", "Riverhog.Gogurt"]
    assert create[create.index("/SC") + 1] == "ONLOGON"
    assert create[create.index("/RL") + 1] == "LIMITED"
    assert "/IT" in create
    assert "/RU" not in create
    assert adapter.status(paths).running is True

    adapter.unregister(paths)
    assert any("/Delete" in item for item in adapter.commands)
