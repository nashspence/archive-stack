from __future__ import annotations

import os
import plistlib
import stat
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


def test_vanished_sqlite_sidecar_does_not_abort_remaining_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vanished = tmp_path / "listener.sqlite3-shm"
    remaining = tmp_path / "listener.sqlite3-wal"
    vanished.touch()
    remaining.touch()
    vanished.chmod(0o644)
    remaining.chmod(0o644)
    real_open = os.open

    def remove_before_open(path: Path, flags: int, mode: int = 0o777) -> int:
        if Path(path) == vanished and vanished.exists():
            vanished.unlink()
        return real_open(path, flags, mode)

    monkeypatch.setattr(filesystem_module.os, "open", remove_before_open)

    filesystem_module.ensure_private_files([vanished, remaining])

    assert not vanished.exists()
    assert stat.S_IMODE(remaining.stat().st_mode) == 0o600


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
    assert plist.get("ProcessType", "Standard") == "Standard"
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
    assert adapter.commands == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", registration.name],
        ["systemctl", "--user", "start", registration.name],
    ]
    assert adapter.status(paths).running is True

    adapter.unregister(paths)
    assert not registration.exists()
    assert ["systemctl", "--user", "disable", "--now", registration.name] in adapter.commands


class RecordingLaunchdAdapter(LaunchdUserAdapter):
    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.loaded = False
        self.running = False

    @staticmethod
    def _domain() -> str:
        return "gui/501"

    def _run(
        self,
        command: Sequence[str],
        *,
        allowed: frozenset[int] = frozenset({0}),
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(list(command))
        operation = command[1]
        if operation == "bootout":
            self.loaded = False
            self.running = False
            return subprocess.CompletedProcess(list(command), 0, "", "")
        if operation == "bootstrap":
            self.loaded = True
            self.running = True
            return subprocess.CompletedProcess(list(command), 0, "", "")
        if operation == "kickstart":
            self.running = True
            return subprocess.CompletedProcess(list(command), 0, "", "")
        if operation == "print" and self.loaded:
            state = "running" if self.running else "waiting"
            pid = "\n\tpid = 4321" if self.running else ""
            return subprocess.CompletedProcess(
                list(command),
                0,
                f"gui/501/{LISTENER_LABEL} = {{\n\tstate = {state}{pid}\n}}\n",
                "",
            )
        completed = subprocess.CompletedProcess(list(command), 3, "", "not loaded")
        if completed.returncode not in allowed:
            raise ListenerPlatformError("launchd query failed")
        return completed


def test_launchd_registration_bootstraps_and_removes_the_agent(tmp_path: Path) -> None:
    registration = tmp_path / "Library" / "LaunchAgents" / f"{LISTENER_LABEL}.plist"
    paths = _paths(tmp_path, registration)
    adapter = RecordingLaunchdAdapter()
    command = (str(tmp_path / "gogurt"), "listener", "_run")

    adapter.register(paths, command)
    assert registration.is_file()
    assert adapter.commands == [["launchctl", "bootstrap", "gui/501", str(registration)]]
    assert adapter.status(paths) == NativeListenerStatus(
        installed=True,
        enabled=True,
        running=True,
    )

    adapter.running = False
    assert adapter.status(paths) == NativeListenerStatus(
        installed=True,
        enabled=True,
        running=False,
    )
    adapter.start(paths)
    assert adapter.commands[-1] == [
        "launchctl",
        "kickstart",
        f"gui/501/{LISTENER_LABEL}",
    ]
    assert adapter.running is True

    adapter.loaded = False
    adapter.running = False
    adapter.start(paths)
    assert adapter.commands[-1] == [
        "launchctl",
        "bootstrap",
        "gui/501",
        str(registration),
    ]

    adapter.unregister(paths)
    assert not registration.exists()
    assert adapter.loaded is False
    assert adapter.commands[-2:] == [
        ["launchctl", "bootout", f"gui/501/{LISTENER_LABEL}"],
        ["launchctl", "print", f"gui/501/{LISTENER_LABEL}"],
    ]


def test_launchd_stop_waits_until_the_native_job_is_unloaded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registration = tmp_path / "Library" / "LaunchAgents" / f"{LISTENER_LABEL}.plist"
    paths = _paths(tmp_path, registration)
    adapter = RecordingLaunchdAdapter()
    adapter.register(paths, (str(tmp_path / "gogurt"), "listener", "_run"))
    observations = iter(
        (
            subprocess.CompletedProcess(["launchctl", "print"], 0, "state = waiting", ""),
            subprocess.CompletedProcess(["launchctl", "print"], 3, "", "not loaded"),
        )
    )
    monkeypatch.setattr(adapter, "_print", lambda: next(observations))
    monkeypatch.setattr("gogurt.listener_platform.time.sleep", lambda _seconds: None)

    adapter.stop(paths)

    with pytest.raises(StopIteration):
        next(observations)


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
    assert [item[1] for item in adapter.commands] == ["/Create", "/Run"]
    create = adapter.commands[0]
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
    joined_state_command = " ".join(state_command)
    assert "} catch { if ($_.Exception.HResult" in joined_state_command
    assert str(WINDOWS_TASK_NOT_FOUND_HRESULT) in joined_state_command
    assert f"exit {WINDOWS_TASK_NOT_FOUND_EXIT}" in joined_state_command

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
