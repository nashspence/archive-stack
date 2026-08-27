"""Windows Task Scheduler listener-host integration for Gogurt."""

from __future__ import annotations

import ctypes
import os
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from hashlib import sha256
from pathlib import Path, PureWindowsPath
from typing import Any, cast

from gogurt_listener_runtime.filesystem import PRIVATE_FILE_MODE, atomic_write, stage_bytes
from gogurt_listener_runtime.platform import (
    ListenerAdapter,
    ListenerHostProviderBinding,
    ListenerPlatformError,
    ListenerRuntimePaths,
    NativeListenerStatus,
)

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


def default_listener_paths(
    *,
    environment: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> ListenerRuntimePaths:
    del home
    env = environment or os.environ
    raw_local = env.get("LOCALAPPDATA")
    if not raw_local:
        raise ListenerPlatformError("LOCALAPPDATA is required for the Gogurt listener")
    state_dir = Path(raw_local).expanduser().resolve() / "Gogurt"
    return ListenerRuntimePaths(
        state_dir=state_dir,
        config_file=state_dir / "listener.json",
        database_file=state_dir / "listener.sqlite3",
        heartbeat_file=state_dir / "heartbeat.json",
        lock_file=state_dir / "listener.lock",
        log_file=state_dir / "listener.log",
        stop_file=state_dir / "stop.request",
    )


def windows_task_name(user_sid: str) -> str:
    if _WINDOWS_SID_RE.fullmatch(user_sid) is None:
        raise ListenerPlatformError("Windows returned an invalid current-user SID")
    return f"{WINDOWS_TASK_NAME_PREFIX}.{sha256(user_sid.encode('ascii')).hexdigest()}"


def render_windows_task_xml(command: Sequence[str], *, user_sid: str) -> bytes:
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
    return cast(bytes, ET.tostring(task, encoding="utf-16", xml_declaration=True))


class TaskSchedulerUserAdapter:
    def _run(
        self,
        command: Sequence[str],
        *,
        allowed: frozenset[int] = frozenset({0}),
    ) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(list(command), check=False, capture_output=True, text=True)
        if completed.returncode not in allowed:
            detail = completed.stderr.strip() or completed.stdout.strip() or "no diagnostic"
            raise ListenerPlatformError(f"{' '.join(command)} failed: {detail}")
        return completed

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
        user_sid = self._run(self._identity_command()).stdout.strip()
        windows_task_name(user_sid)
        return user_sid

    @staticmethod
    def _state_command(task_name: str) -> list[str]:
        system_root = PureWindowsPath(os.environ.get("SystemRoot", r"C:\Windows"))
        powershell = system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        script = (
            "$ErrorActionPreference = 'Stop'; try { "
            "$service = New-Object -ComObject 'Schedule.Service'; $service.Connect(); "
            f"$task = $service.GetFolder('\\').GetTask('{task_name}'); "
            "[Console]::Out.Write([int]$task.State); exit 0 } catch { "
            f"if ($_.Exception.HResult -eq {WINDOWS_TASK_NOT_FOUND_HRESULT}) {{ exit "
            f"{WINDOWS_TASK_NOT_FOUND_EXIT} }}; throw }}"
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
            self._state_command(task_name), allowed=frozenset({0, WINDOWS_TASK_NOT_FOUND_EXIT})
        )
        if completed.returncode != 0:
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
    def _clear_stop_request(paths: ListenerRuntimePaths) -> None:
        paths.stop_file.unlink(missing_ok=True)

    def register(self, paths: ListenerRuntimePaths, command: Sequence[str]) -> None:
        user_sid = self._current_user_sid()
        task_name = windows_task_name(user_sid)
        self._clear_stop_request(paths)
        temporary = stage_bytes(
            paths.state_dir / "listener-task.xml",
            render_windows_task_xml(command, user_sid=user_sid),
            mode=PRIVATE_FILE_MODE,
        )
        try:
            self._run(["schtasks.exe", "/Create", "/TN", task_name, "/XML", str(temporary), "/F"])
        finally:
            temporary.unlink(missing_ok=True)
        self._run(["schtasks.exe", "/Run", "/TN", task_name])

    def status(self, paths: ListenerRuntimePaths) -> NativeListenerStatus:
        del paths
        return self._query_state(self._task_name())

    def start(self, paths: ListenerRuntimePaths) -> None:
        task_name = self._task_name()
        if not self._query_state(task_name).installed:
            raise ListenerPlatformError("Gogurt listener is not installed")
        self._clear_stop_request(paths)
        self._run(["schtasks.exe", "/Run", "/TN", task_name])

    def stop(self, paths: ListenerRuntimePaths) -> None:
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

    def unregister(self, paths: ListenerRuntimePaths) -> None:
        task_name = self._task_name()
        if self._query_state(task_name).running:
            self.stop(paths)
        self._run(
            ["schtasks.exe", "/Delete", "/TN", task_name, "/F"],
            allowed=frozenset({0, 1}),
        )

    @staticmethod
    def process_is_running(pid: int) -> bool:
        kernel32 = cast(Any, ctypes).windll.kernel32
        handle = kernel32.OpenProcess(0x00100000, False, pid)
        if not handle:
            return bool(kernel32.GetLastError() == 5)
        try:
            return bool(kernel32.WaitForSingleObject(handle, 0) == 0x00000102)
        finally:
            kernel32.CloseHandle(handle)


def listener_adapter() -> ListenerAdapter:
    return TaskSchedulerUserAdapter()


def resolve_listener_executable(raw: str | None = None) -> Path:
    candidate = raw or sys.argv[0]
    source = Path(candidate).expanduser()
    resolved = shutil.which(candidate)
    candidates = [Path(resolved)] if resolved is not None else [source]
    if source.suffix == "":
        candidates.extend(
            Path(f"{source}{extension}")
            for extension in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";")
            if extension
        )
    for value in candidates:
        path = value.resolve()
        if path.is_file():
            return path
    raise ListenerPlatformError(
        f"installed Gogurt executable is absent or not executable: {source.resolve()}"
    )


LISTENER_HOST_PROVIDER_BINDING = ListenerHostProviderBinding(
    provider_id="gogurt-windows-listener-host-provider/v1",
    paths=default_listener_paths,
    adapter=listener_adapter,
    executable=resolve_listener_executable,
)


__all__ = [
    "LISTENER_HOST_PROVIDER_BINDING",
    "TaskSchedulerUserAdapter",
    "WINDOWS_TASK_RESTART_COUNT",
    "WINDOWS_TASK_RESTART_INTERVAL",
    "WINDOWS_TASK_XML_NAMESPACE",
    "default_listener_paths",
    "listener_adapter",
    "render_windows_task_xml",
    "resolve_listener_executable",
    "windows_task_name",
]
