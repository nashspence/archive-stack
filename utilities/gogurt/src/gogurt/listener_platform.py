from __future__ import annotations

import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PureWindowsPath
from typing import Protocol, cast

from gogurt.filesystem import PRIVATE_FILE_MODE, atomic_write, stage_bytes

LISTENER_LABEL = "io.github.nashspence.gogurt"
LAUNCHD_SETTLE_SECONDS = 20.0
WINDOWS_TASK_NAME_PREFIX = "Riverhog.Gogurt"
WINDOWS_TASK_STATE_DISABLED = 1
WINDOWS_TASK_STATE_RUNNING = 4
WINDOWS_TASK_NOT_FOUND_EXIT = 3
WINDOWS_TASK_NOT_FOUND_HRESULT = -2147024894
WINDOWS_TASK_RESTART_COUNT = 3
WINDOWS_TASK_RESTART_INTERVAL = "PT1M"
WINDOWS_STOP_SETTLE_SECONDS = 20.0
WINDOWS_TASK_XML_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"
_WINDOWS_SID_RE = re.compile(r"S-[0-9]+(?:-[0-9]+)+")


class ListenerPlatformError(RuntimeError):
    """The native per-user service manager rejected a listener operation."""


@dataclass(frozen=True, slots=True)
class ListenerPaths:
    state_dir: Path
    config_file: Path
    database_file: Path
    heartbeat_file: Path
    lock_file: Path
    log_file: Path
    stop_file: Path
    registration_file: Path | None


@dataclass(frozen=True, slots=True)
class NativeListenerStatus:
    installed: bool
    enabled: bool
    running: bool


def default_listener_paths(
    *,
    platform: str | None = None,
    environment: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> ListenerPaths:
    current = platform or sys.platform
    env = environment or os.environ
    user_home = (home or Path.home()).expanduser().resolve()
    if current == "darwin":
        state_dir = user_home / "Library" / "Application Support" / "Gogurt"
        registration = user_home / "Library" / "LaunchAgents" / f"{LISTENER_LABEL}.plist"
    elif current.startswith("linux"):
        state_root = Path(env.get("XDG_STATE_HOME", user_home / ".local" / "state"))
        config_root = Path(env.get("XDG_CONFIG_HOME", user_home / ".config"))
        state_dir = state_root.expanduser().resolve() / "gogurt"
        registration = (
            config_root.expanduser().resolve() / "systemd" / "user" / "gogurt-listener.service"
        )
    elif current == "win32":
        raw_local = env.get("LOCALAPPDATA")
        if not raw_local:
            raise ListenerPlatformError("LOCALAPPDATA is required for the Gogurt listener")
        state_dir = Path(raw_local).expanduser().resolve() / "Gogurt"
        registration = None
    else:
        raise ListenerPlatformError(f"Gogurt listener installation is unsupported on {current}")
    return ListenerPaths(
        state_dir=state_dir,
        config_file=state_dir / "listener.json",
        database_file=state_dir / "listener.sqlite3",
        heartbeat_file=state_dir / "heartbeat.json",
        lock_file=state_dir / "listener.lock",
        log_file=state_dir / "listener.log",
        stop_file=state_dir / "stop.request",
        registration_file=registration,
    )


def _write_private(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, content, mode=PRIVATE_FILE_MODE)


def _systemd_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_systemd_unit(command: Sequence[str]) -> bytes:
    rendered = " ".join(_systemd_quote(value) for value in command)
    return (
        "[Unit]\n"
        "Description=Gogurt mounted-volume listener\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={rendered}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "NoNewPrivileges=true\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    ).encode()


def render_launchd_plist(command: Sequence[str]) -> bytes:
    return plistlib.dumps(
        {
            "Label": LISTENER_LABEL,
            "ProgramArguments": list(command),
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},
            "ThrottleInterval": 5,
            "StandardOutPath": "/dev/null",
            "StandardErrorPath": "/dev/null",
        },
        fmt=plistlib.FMT_XML,
        sort_keys=False,
    )


def windows_task_name(user_sid: str) -> str:
    """Return the stable machine-unique task name for one Windows user."""

    if _WINDOWS_SID_RE.fullmatch(user_sid) is None:
        raise ListenerPlatformError("Windows returned an invalid current-user SID")
    identity = sha256(user_sid.encode("ascii")).hexdigest()
    return f"{WINDOWS_TASK_NAME_PREFIX}.{identity}"


def render_windows_task_xml(command: Sequence[str], *, user_sid: str) -> bytes:
    """Render the complete current-user Task Scheduler contract."""

    if not command:
        raise ListenerPlatformError("Gogurt listener command is empty")
    windows_task_name(user_sid)
    namespace = WINDOWS_TASK_XML_NAMESPACE
    ET.register_namespace("", namespace)

    def child(parent: ET.Element, name: str, text: str | None = None) -> ET.Element:
        value = ET.SubElement(parent, f"{{{namespace}}}{name}")
        value.text = text
        return value

    task = ET.Element(f"{{{namespace}}}Task", {"version": "1.4"})
    registration = child(task, "RegistrationInfo")
    child(registration, "Description", "Gogurt mounted-volume listener")
    triggers = child(task, "Triggers")
    logon = child(triggers, "LogonTrigger")
    child(logon, "Enabled", "true")
    child(logon, "UserId", user_sid)
    principals = child(task, "Principals")
    principal = ET.SubElement(principals, f"{{{namespace}}}Principal", {"id": "CurrentUser"})
    child(principal, "UserId", user_sid)
    child(principal, "LogonType", "InteractiveToken")
    child(principal, "RunLevel", "LeastPrivilege")
    settings = child(task, "Settings")
    child(settings, "AllowStartOnDemand", "true")
    restart = child(settings, "RestartOnFailure")
    child(restart, "Interval", WINDOWS_TASK_RESTART_INTERVAL)
    child(restart, "Count", str(WINDOWS_TASK_RESTART_COUNT))
    child(settings, "MultipleInstancesPolicy", "IgnoreNew")
    child(settings, "DisallowStartIfOnBatteries", "false")
    child(settings, "StopIfGoingOnBatteries", "false")
    child(settings, "AllowHardTerminate", "false")
    child(settings, "StartWhenAvailable", "true")
    child(settings, "RunOnlyIfNetworkAvailable", "false")
    child(settings, "WakeToRun", "false")
    child(settings, "Enabled", "true")
    child(settings, "Hidden", "false")
    child(settings, "ExecutionTimeLimit", "PT0S")
    child(settings, "Priority", "7")
    child(settings, "RunOnlyIfIdle", "false")
    actions = ET.SubElement(task, f"{{{namespace}}}Actions", {"Context": "CurrentUser"})
    execute = child(actions, "Exec")
    child(execute, "Command", command[0])
    if len(command) > 1:
        child(execute, "Arguments", subprocess.list2cmdline(list(command[1:])))
    # Task Scheduler's XML-file import consumes the task schema as UTF-16.
    # Emit the matching BOM and declaration together; declaring UTF-8 here can
    # make schtasks reject an otherwise valid task as "unable to switch the
    # encoding".
    return cast(bytes, ET.tostring(task, encoding="utf-16", xml_declaration=True))


class ListenerAdapter(Protocol):
    def register(self, paths: ListenerPaths, command: Sequence[str]) -> None: ...

    def status(self, paths: ListenerPaths) -> NativeListenerStatus: ...

    def start(self, paths: ListenerPaths) -> None: ...

    def stop(self, paths: ListenerPaths) -> None: ...

    def unregister(self, paths: ListenerPaths) -> None: ...


class _CommandAdapter:
    def _run(
        self,
        command: Sequence[str],
        *,
        allowed: frozenset[int] = frozenset({0}),
    ) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode not in allowed:
            detail = completed.stderr.strip() or completed.stdout.strip() or "no diagnostic"
            raise ListenerPlatformError(f"{' '.join(command)} failed: {detail}")
        return completed


class SystemdUserAdapter(_CommandAdapter):
    def _require_registration(self, paths: ListenerPaths) -> Path:
        if paths.registration_file is None:
            raise ListenerPlatformError("systemd listener registration path is absent")
        return paths.registration_file

    def register(self, paths: ListenerPaths, command: Sequence[str]) -> None:
        registration = self._require_registration(paths)
        _write_private(registration, render_systemd_unit(command))
        self._run(["systemctl", "--user", "daemon-reload"])
        self._run(["systemctl", "--user", "enable", registration.name])
        self._run(["systemctl", "--user", "start", registration.name])

    def status(self, paths: ListenerPaths) -> NativeListenerStatus:
        registration = self._require_registration(paths)
        loaded = self._run(
            [
                "systemctl",
                "--user",
                "show",
                registration.name,
                "--property=LoadState",
                "--value",
            ],
            allowed=frozenset({0, 1, 3, 4}),
        )
        manager_loaded = loaded.returncode == 0 and loaded.stdout.strip() not in {
            "",
            "not-found",
        }
        enabled = (
            self._run(
                ["systemctl", "--user", "is-enabled", registration.name],
                allowed=frozenset({0, 1, 3, 4}),
            ).returncode
            == 0
        )
        running = (
            self._run(
                ["systemctl", "--user", "is-active", registration.name],
                allowed=frozenset({0, 1, 3, 4}),
            ).returncode
            == 0
        )
        return NativeListenerStatus(
            installed=registration.is_file() or manager_loaded or enabled or running,
            enabled=enabled,
            running=running,
        )

    def start(self, paths: ListenerPaths) -> None:
        registration = self._require_registration(paths)
        if not self.status(paths).installed:
            raise ListenerPlatformError("Gogurt listener is not installed")
        self._run(["systemctl", "--user", "start", registration.name])

    def stop(self, paths: ListenerPaths) -> None:
        registration = self._require_registration(paths)
        if self.status(paths).installed:
            self._run(
                ["systemctl", "--user", "stop", registration.name],
                allowed=frozenset({0, 5}),
            )

    def unregister(self, paths: ListenerPaths) -> None:
        registration = self._require_registration(paths)
        # A loaded unit remains manager authority even when its definition file
        # has vanished. Stop it explicitly before disabling it: combining
        # disable with --now can fail to issue the stop when systemd can no
        # longer resolve the on-disk unit, leaving Restart=on-failure free to
        # relaunch a cooperatively settling listener.
        self.stop(paths)
        self._run(
            ["systemctl", "--user", "disable", registration.name],
            allowed=frozenset({0, 1, 5}),
        )
        registration.unlink(missing_ok=True)
        self._run(["systemctl", "--user", "daemon-reload"])
        self._run(
            ["systemctl", "--user", "reset-failed", registration.name],
            allowed=frozenset({0, 1}),
        )


class LaunchdUserAdapter(_CommandAdapter):
    @staticmethod
    def _domain() -> str:
        getuid = getattr(os, "getuid", None)
        if getuid is None:
            raise ListenerPlatformError("launchd user identity is unavailable")
        return f"gui/{getuid()}"

    def _require_registration(self, paths: ListenerPaths) -> Path:
        if paths.registration_file is None:
            raise ListenerPlatformError("launchd listener registration path is absent")
        return paths.registration_file

    def _target(self) -> str:
        return f"{self._domain()}/{LISTENER_LABEL}"

    def _print(self) -> subprocess.CompletedProcess[str]:
        return self._run(
            ["launchctl", "print", self._target()],
            allowed=frozenset({0, 3, 113}),
        )

    def _wait_unloaded(self) -> None:
        deadline = time.monotonic() + LAUNCHD_SETTLE_SECONDS
        while True:
            state = self._print()
            if state.returncode != 0:
                return
            if time.monotonic() >= deadline:
                raise ListenerPlatformError("launchd did not unload the Gogurt listener")
            time.sleep(0.1)

    @staticmethod
    def _is_running(completed: subprocess.CompletedProcess[str]) -> bool:
        if completed.returncode != 0:
            return False
        state = re.search(r"(?m)^\s*state\s*=\s*([^\s]+)\s*$", completed.stdout)
        pid = re.search(r"(?m)^\s*pid\s*=\s*([0-9]+)\s*$", completed.stdout)
        return (
            state is not None
            and state.group(1).casefold() == "running"
            and pid is not None
            and int(pid.group(1)) > 0
        )

    def register(self, paths: ListenerPaths, command: Sequence[str]) -> None:
        registration = self._require_registration(paths)
        _write_private(registration, render_launchd_plist(command))
        self._run(["launchctl", "bootstrap", self._domain(), str(registration)])

    def status(self, paths: ListenerPaths) -> NativeListenerStatus:
        registration = self._require_registration(paths)
        state = self._print()
        loaded = state.returncode == 0
        installed = registration.is_file() or loaded
        return NativeListenerStatus(
            installed=installed,
            enabled=installed,
            running=self._is_running(state),
        )

    def start(self, paths: ListenerPaths) -> None:
        registration = self._require_registration(paths)
        state = self._print()
        if state.returncode != 0:
            if not registration.is_file():
                raise ListenerPlatformError("Gogurt listener is not installed")
            self._run(["launchctl", "bootstrap", self._domain(), str(registration)])
        elif not self._is_running(state):
            self._run(["launchctl", "kickstart", self._target()])

    def stop(self, paths: ListenerPaths) -> None:
        self._require_registration(paths)
        if self._print().returncode == 0:
            self._run(
                ["launchctl", "bootout", self._target()],
                allowed=frozenset({0, 3, 113}),
            )
            self._wait_unloaded()

    def unregister(self, paths: ListenerPaths) -> None:
        registration = self._require_registration(paths)
        self.stop(paths)
        registration.unlink(missing_ok=True)


class TaskSchedulerUserAdapter(_CommandAdapter):
    @staticmethod
    def _identity_command() -> list[str]:
        system_root = PureWindowsPath(os.environ.get("SystemRoot", r"C:\Windows"))
        powershell = system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        return [
            str(powershell),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "[Console]::Out.Write([Security.Principal.WindowsIdentity]::GetCurrent().User.Value)",
        ]

    def _current_user_sid(self) -> str:
        completed = self._run(self._identity_command())
        user_sid = completed.stdout.strip()
        windows_task_name(user_sid)
        return user_sid

    @staticmethod
    def _state_command(task_name: str) -> list[str]:
        system_root = PureWindowsPath(os.environ.get("SystemRoot", r"C:\Windows"))
        powershell = system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        script = (
            "$ErrorActionPreference = 'Stop'; "
            "try { "
            "$service = New-Object -ComObject 'Schedule.Service'; "
            "$service.Connect(); "
            f"$task = $service.GetFolder('\\').GetTask('{task_name}'); "
            "[Console]::Out.Write([int]$task.State); "
            "exit 0 "
            "} catch { "
            f"if ($_.Exception.HResult -eq {WINDOWS_TASK_NOT_FOUND_HRESULT}) {{ exit "
            f"{WINDOWS_TASK_NOT_FOUND_EXIT} }}; "
            "throw "
            "}"
        )
        return [
            str(powershell),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
        ]

    def _task_name(self) -> str:
        return windows_task_name(self._current_user_sid())

    def _query_state(self, task_name: str) -> NativeListenerStatus:
        completed = self._run(
            self._state_command(task_name),
            allowed=frozenset({0, WINDOWS_TASK_NOT_FOUND_EXIT}),
        )
        installed = completed.returncode == 0
        if not installed:
            return NativeListenerStatus(installed=False, enabled=False, running=False)
        try:
            state = int(completed.stdout.strip())
        except ValueError as exc:
            raise ListenerPlatformError("Task Scheduler returned an invalid numeric state") from exc
        if state not in range(5):
            raise ListenerPlatformError(
                f"Task Scheduler returned an unknown numeric state: {state}"
            )
        return NativeListenerStatus(
            installed=True,
            enabled=state != WINDOWS_TASK_STATE_DISABLED,
            running=state == WINDOWS_TASK_STATE_RUNNING,
        )

    @staticmethod
    def _clear_stop_request(paths: ListenerPaths) -> None:
        paths.stop_file.unlink(missing_ok=True)

    def register(self, paths: ListenerPaths, command: Sequence[str]) -> None:
        # The listener replacement transaction quiesces and proves the prior
        # process absent before registration. Keep registration construction-
        # only: an additional asynchronous /End can terminate the new /Run.
        user_sid = self._current_user_sid()
        task_name = windows_task_name(user_sid)
        self._clear_stop_request(paths)
        temporary = stage_bytes(
            paths.state_dir / "listener-task.xml",
            render_windows_task_xml(command, user_sid=user_sid),
            mode=PRIVATE_FILE_MODE,
        )
        try:
            self._run(
                [
                    "schtasks.exe",
                    "/Create",
                    "/TN",
                    task_name,
                    "/XML",
                    str(temporary),
                    "/F",
                ]
            )
        finally:
            temporary.unlink(missing_ok=True)
        self._run(["schtasks.exe", "/Run", "/TN", task_name])

    def status(self, paths: ListenerPaths) -> NativeListenerStatus:
        del paths
        return self._query_state(self._task_name())

    def start(self, paths: ListenerPaths) -> None:
        task_name = self._task_name()
        if not self._query_state(task_name).installed:
            raise ListenerPlatformError("Gogurt listener is not installed")
        self._clear_stop_request(paths)
        self._run(["schtasks.exe", "/Run", "/TN", task_name])

    def stop(self, paths: ListenerPaths) -> None:
        task_name = self._task_name()
        state = self._query_state(task_name)
        if not state.installed or not state.running:
            return
        atomic_write(paths.stop_file, b"stop\n", mode=PRIVATE_FILE_MODE)
        deadline = time.monotonic() + WINDOWS_STOP_SETTLE_SECONDS
        while time.monotonic() < deadline:
            if not self._query_state(task_name).running:
                return
            time.sleep(0.1)
        raise ListenerPlatformError(
            "Gogurt listener did not settle its action custody before the Windows stop deadline"
        )

    def unregister(self, paths: ListenerPaths) -> None:
        task_name = self._task_name()
        if self._query_state(task_name).running:
            self.stop(paths)
        self._run(
            ["schtasks.exe", "/Delete", "/TN", task_name, "/F"],
            allowed=frozenset({0, 1}),
        )


def listener_adapter(platform: str | None = None) -> ListenerAdapter:
    current = platform or sys.platform
    if current == "darwin":
        return LaunchdUserAdapter()
    if current.startswith("linux"):
        return SystemdUserAdapter()
    if current == "win32":
        return TaskSchedulerUserAdapter()
    raise ListenerPlatformError(f"Gogurt listener installation is unsupported on {current}")


def resolve_listener_executable(raw: str | None = None) -> Path:
    candidate = raw or sys.argv[0]
    source = Path(candidate).expanduser()
    resolved = shutil.which(candidate)
    candidates = [Path(resolved)] if resolved is not None else [source]
    if sys.platform == "win32" and source.suffix == "":
        extensions = os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";")
        candidates.extend(Path(f"{source}{extension}") for extension in extensions if extension)
    for value in candidates:
        path = value.resolve()
        if path.is_file() and (sys.platform == "win32" or os.access(path, os.X_OK)):
            return path
    path = source.resolve()
    raise ListenerPlatformError(f"installed Gogurt executable is absent or not executable: {path}")
