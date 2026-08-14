from __future__ import annotations

import plistlib
import subprocess
from collections.abc import Sequence
from pathlib import Path

import gogurt.filesystem as filesystem_module
import pytest
from gogurt.listener_platform import (
    LISTENER_LABEL,
    WINDOWS_TASK_NOT_FOUND_EXIT,
    WINDOWS_TASK_NOT_FOUND_HRESULT,
    LaunchdUserAdapter,
    ListenerPaths,
    ListenerPlatformError,
    NativeListenerStatus,
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


def test_windows_existing_private_file_is_validated_without_reopening(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sidecar = tmp_path / "listener.sqlite3-shm"
    sidecar.touch()

    def refuse_open(*_args: object, **_kwargs: object) -> int:
        raise AssertionError("Windows existing state must not be reopened for POSIX modes")

    monkeypatch.setattr("gogurt.filesystem.os.name", "nt")
    monkeypatch.setattr("gogurt.filesystem.os.open", refuse_open)

    filesystem_module.ensure_private_file(sidecar)


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
        self.task_state = "4"
        self.task_exists = True
        self.query_failure = False

    def _run(
        self,
        command: Sequence[str],
        *,
        allowed: frozenset[int] = frozenset({0}),
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(list(command))
        if "-Command" in command:
            return_code = (
                1 if self.query_failure else 0 if self.task_exists else WINDOWS_TASK_NOT_FOUND_EXIT
            )
            completed = subprocess.CompletedProcess(
                list(command),
                return_code,
                self.task_state if self.task_exists else "localized task-not-found text",
                "scheduler query failed" if self.query_failure else "",
            )
            if completed.returncode not in allowed:
                raise ListenerPlatformError("Task Scheduler query failed")
            return completed
        return subprocess.CompletedProcess(list(command), 0, "", "")


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
    running = adapter.status(paths)
    assert running == NativeListenerStatus(installed=True, enabled=True, running=True)
    state_command = adapter.commands[-1]
    assert state_command[0].endswith("WindowsPowerShell\\v1.0\\powershell.exe")
    assert "Running" not in " ".join(state_command)
    assert str(WINDOWS_TASK_NOT_FOUND_HRESULT) in " ".join(state_command)
    assert f"exit {WINDOWS_TASK_NOT_FOUND_EXIT}" in " ".join(state_command)

    adapter.task_state = "3"
    ready = adapter.status(paths)
    assert ready == NativeListenerStatus(installed=True, enabled=True, running=False)

    adapter.task_state = "1"
    disabled = adapter.status(paths)
    assert disabled == NativeListenerStatus(installed=True, enabled=False, running=False)

    adapter.task_exists = False
    absent = adapter.status(paths)
    assert absent == NativeListenerStatus(installed=False, enabled=False, running=False)

    adapter.task_exists = True
    adapter.query_failure = True
    with pytest.raises(ListenerPlatformError, match="query failed"):
        adapter.status(paths)

    adapter.query_failure = False
    adapter.unregister(paths)
    assert any("/Delete" in item for item in adapter.commands)
