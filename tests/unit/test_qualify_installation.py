from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from riverhog_protocol.lifecycle_events import RiverhogEventPage

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts/qualify_installation.py"


def load_script() -> ModuleType:
    if str(SCRIPT.parent) not in sys.path:
        sys.path.insert(0, str(SCRIPT.parent))
    spec = importlib.util.spec_from_file_location("riverhog_qualify_installation", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_disposable_event_fixture_is_an_exact_riverhog_lifecycle_page() -> None:
    module = load_script()

    page = RiverhogEventPage.model_validate(module.EVENT_PAGE)

    assert page.model_dump(mode="json", exclude_none=True) == module.EVENT_PAGE


def test_distribution_builds_are_serialized_into_a_clean_output(
    tmp_path: Path,
) -> None:
    module = load_script()
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "stale.whl").write_bytes(b"stale")
    commands: list[list[str]] = []

    def record(
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str] | None = None,
        capture: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        assert cwd == tmp_path
        assert env is None
        assert capture is False
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    module._run = record

    module._build_distributions(
        tmp_path,
        [SimpleNamespace(name="alpha"), SimpleNamespace(name="bravo")],
    )

    assert list(dist.iterdir()) == []
    assert commands == [
        [
            "uv",
            "--no-config",
            "build",
            "--package",
            "alpha",
            "--no-create-gitignore",
        ],
        [
            "uv",
            "--no-config",
            "build",
            "--package",
            "bravo",
            "--no-create-gitignore",
        ],
    ]


def test_macos_qualification_mount_reports_the_exact_failed_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_script()
    commands: list[list[str]] = []

    def run(
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 1, "create output", "create error")

    monkeypatch.setattr(module.sys, "platform", "darwin")
    monkeypatch.setattr(module.subprocess, "run", run)

    with pytest.raises(
        module.QualificationError,
        match="macOS qualification disk-image creation failed with exit 1",
    ) as captured:
        with module._qualification_mount(tmp_path):
            pytest.fail("a failed disk-image creation exposed a mount")

    diagnostic = str(captured.value)
    assert "stdout='create output'" in diagnostic
    assert "stderr='create error'" in diagnostic
    assert commands == [
        [
            "hdiutil",
            "create",
            "-size",
            "16m",
            "-fs",
            "HFS+",
            "-volname",
            "GogurtQualification",
            str(tmp_path / "gogurt-listener.dmg"),
        ]
    ]


def test_macos_qualification_mount_checks_detachment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_script()
    commands: list[list[str]] = []

    def run(
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if command[1] == "detach":
            return subprocess.CompletedProcess(command, 9, "detach output", "detach error")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(module.sys, "platform", "darwin")
    monkeypatch.setattr(module.os, "getpid", lambda: 123)
    monkeypatch.setattr(module.subprocess, "run", run)

    with pytest.raises(
        module.QualificationError,
        match="macOS qualification disk-image detachment failed with exit 9",
    ) as captured:
        with module._qualification_mount(tmp_path) as mounted:
            assert mounted == Path("/Volumes/GogurtQualification-123")

    diagnostic = str(captured.value)
    assert "stdout='detach output'" in diagnostic
    assert "stderr='detach error'" in diagnostic
    assert commands[-1] == [
        "hdiutil",
        "detach",
        "-force",
        "/Volumes/GogurtQualification-123",
    ]


def test_qualification_mount_preserves_body_and_cleanup_failures(tmp_path: Path) -> None:
    module = load_script()

    def fail_cleanup() -> None:
        raise module.QualificationError("detach failed")

    with pytest.raises(module.QualificationError) as captured:
        with module._settled_qualification_mount(tmp_path, fail_cleanup):
            raise RuntimeError("lifecycle failed")

    assert str(captured.value) == (
        "qualification failed with RuntimeError: lifecycle failed; "
        "mount cleanup also failed with QualificationError: detach failed"
    )


def test_listener_is_settled_before_qualification_mount_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_script()
    events: list[str] = []

    @contextmanager
    def mount(_scratch: Path) -> Iterator[Path]:
        events.append("attach")
        try:
            yield tmp_path / "mounted"
        finally:
            events.append("detach")

    monkeypatch.setattr(module, "_qualification_mount", mount)

    with module._qualification_listener_mount(
        tmp_path,
        observe_failure=lambda _exc: events.append("observe-failure"),
        settle_listener=lambda: events.append("settle-listener"),
    ):
        events.append("lifecycle")

    assert events == ["attach", "lifecycle", "settle-listener", "detach"]


def test_listener_failure_is_retained_before_settlement_and_mount_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_script()
    events: list[str] = []

    @contextmanager
    def mount(_scratch: Path) -> Iterator[Path]:
        events.append("attach")
        try:
            yield tmp_path / "mounted"
        finally:
            events.append("detach")

    monkeypatch.setattr(module, "_qualification_mount", mount)

    with pytest.raises(RuntimeError, match="lifecycle failed"):
        with module._qualification_listener_mount(
            tmp_path,
            observe_failure=lambda _exc: events.append("observe-failure"),
            settle_listener=lambda: events.append("settle-listener"),
        ):
            events.append("lifecycle")
            raise RuntimeError("lifecycle failed")

    assert events == [
        "attach",
        "lifecycle",
        "observe-failure",
        "settle-listener",
        "detach",
    ]


def test_gogurt_failure_evidence_is_bounded_to_status_and_listener_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_script()
    state = tmp_path / "state"
    state.mkdir()
    (state / "listener.log").write_text("safe lifecycle diagnostic\n", encoding="utf-8")
    (state / "listener.log.1").write_text("older diagnostic\n", encoding="utf-8")
    (state / "listener.fatal.log").write_text("fatal diagnostic\n", encoding="utf-8")
    (state / "listener.sqlite3").write_bytes(b"private state")
    (state / "listener.json").write_text("private config", encoding="utf-8")
    evidence = tmp_path / "evidence"

    def status(
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        if command[:2] == ["launchctl", "print"]:
            return subprocess.CompletedProcess(
                command,
                0,
                "state = running\n\tpid = 123\n\truns = 4\n\tlast exit code = 1\n",
                "private native diagnostic",
            )
        return subprocess.CompletedProcess(command, 0, '{"health":"failed"}\n', "")

    monkeypatch.setattr(module.subprocess, "run", status)
    monkeypatch.setattr(module.sys, "platform", "darwin")
    monkeypatch.setattr(module.os, "getuid", lambda: 501)
    module._retain_gogurt_failure_evidence(
        tmp_path / "gogurt",
        state_dir=state,
        scratch=tmp_path,
        environment={},
        evidence_dir=evidence,
        failure=RuntimeError("bounded failure"),
        phase="lifecycle",
        native_transitions=(
            {
                "elapsed_milliseconds": 125,
                "returncode": 0,
                "fields": {"pid": 123, "runs": 4, "state": "running"},
            },
        ),
    )

    assert {path.name for path in evidence.iterdir()} == {
        "lifecycle-failure.txt",
        "lifecycle-native.json",
        "lifecycle-native-transitions.json",
        "lifecycle-status.json",
        "listener.log",
        "listener.log.1",
        "listener.fatal.log",
    }
    assert "bounded failure" in (evidence / "lifecycle-failure.txt").read_text(encoding="utf-8")
    assert json.loads((evidence / "lifecycle-native.json").read_text(encoding="utf-8")) == {
        "fields": {"last exit code": 1, "pid": 123, "runs": 4, "state": "running"},
        "returncode": 0,
    }
    assert json.loads(
        (evidence / "lifecycle-native-transitions.json").read_text(encoding="utf-8")
    ) == {
        "events": [
            {
                "elapsed_milliseconds": 125,
                "fields": {"pid": 123, "runs": 4, "state": "running"},
                "returncode": 0,
            }
        ],
        "schema": "gogurt-native-lifecycle-trace/v1",
    }
    assert "private state" not in "".join(
        path.read_text(encoding="utf-8") for path in evidence.iterdir()
    )


def test_linux_gogurt_failure_evidence_retains_only_bounded_native_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_script()
    state = tmp_path / "state"
    state.mkdir()
    (state / "listener.log").write_text("safe lifecycle diagnostic\n", encoding="utf-8")
    evidence = tmp_path / "evidence"

    def status(
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        if command[:3] == ["systemctl", "--user", "show"]:
            return subprocess.CompletedProcess(
                command,
                0,
                "ActiveState=active\n"
                "SubState=auto-restart\n"
                "Result=core-dump\n"
                "ExecMainCode=3\n"
                "ExecMainStatus=7\n"
                "NRestarts=2\n"
                "Environment=private-value\n",
                "private native diagnostic",
            )
        return subprocess.CompletedProcess(command, 0, '{"health":"failed"}\n', "")

    monkeypatch.setattr(module.subprocess, "run", status)
    monkeypatch.setattr(module.sys, "platform", "linux")
    module._retain_gogurt_failure_evidence(
        tmp_path / "gogurt",
        state_dir=state,
        scratch=tmp_path,
        environment={},
        evidence_dir=evidence,
        failure=RuntimeError("bounded failure"),
        phase="lifecycle",
    )

    assert json.loads((evidence / "lifecycle-native.json").read_text(encoding="utf-8")) == {
        "fields": {
            "ActiveState": "active",
            "ExecMainCode": 3,
            "ExecMainStatus": 7,
            "NRestarts": 2,
            "Result": "core-dump",
            "SubState": "auto-restart",
            "TerminationKind": "core-dump",
            "TerminationSignal": "SIGBUS",
        },
        "returncode": 0,
    }
    assert "private-value" not in "".join(
        path.read_text(encoding="utf-8") for path in evidence.iterdir()
    )


def test_native_lifecycle_trace_records_only_state_transitions(
    tmp_path: Path,
) -> None:
    module = load_script()
    snapshots = iter(
        [
            {"returncode": 3, "fields": {}},
            {"returncode": 3, "fields": {}},
            {"returncode": 0, "fields": {"pid": 91, "state": "running"}},
        ]
    )
    times = iter([1.0, 1.25, 1.5])
    trace = module._NativeLifecycleTrace(
        scratch=tmp_path,
        environment={},
        probe=lambda: next(snapshots),
        clock=lambda: next(times),
    )

    trace._capture()
    trace._capture()
    trace._capture()

    assert trace.events == [
        {"elapsed_milliseconds": 250, "returncode": 3, "fields": {}},
        {
            "elapsed_milliseconds": 500,
            "returncode": 0,
            "fields": {"pid": 91, "state": "running"},
        },
    ]


def test_windows_native_snapshot_retains_only_numeric_task_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_script()

    def status(
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        assert command[:3] == ["powershell.exe", "-NoProfile", "-NonInteractive"]
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps(
                {
                    "returncode": 0,
                    "fields": {
                        "state": 4,
                        "last_result": -1,
                        "instance_pids": [43, 41, 43],
                        "private": "not retained",
                    },
                    "private": "not retained",
                }
            ),
            "private native diagnostic",
        )

    monkeypatch.setattr(module.subprocess, "run", status)
    monkeypatch.setattr(module.sys, "platform", "win32")
    monkeypatch.setattr(
        module,
        "_windows_task_name",
        lambda **_kwargs: "Riverhog.Gogurt.0123456789abcdef",
    )

    assert module._native_listener_snapshot(scratch=tmp_path, environment={}) == {
        "returncode": 0,
        "fields": {"state": 4, "last_result": -1, "instance_pids": [41, 43]},
    }


def test_windows_task_definition_verifies_normalized_current_user_semantics(
    tmp_path: Path,
) -> None:
    module = load_script()
    executable = tmp_path / "gogurt.exe"
    executable.write_bytes(b"fixture")
    sid = "S-1-5-21-101-202-303-1001"
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Task xmlns="{module.WINDOWS_TASK_XML_NAMESPACE}">
  <Triggers><LogonTrigger><UserId>runner\\person</UserId></LogonTrigger></Triggers>
  <Principals><Principal>
    <UserId>person</UserId><LogonType>InteractiveToken</LogonType>
  </Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>0</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>0</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure><Interval>PT1M</Interval><Count>3</Count></RestartOnFailure>
  </Settings>
  <Actions><Exec><Command>{executable}</Command></Exec></Actions>
</Task>
"""

    def run(
        command: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        if command[0] == "powershell.exe":
            return subprocess.CompletedProcess(command, 0, sid, "")
        if command[0] == "schtasks.exe":
            return subprocess.CompletedProcess(command, 0, xml, "")
        assert command == ["whoami.exe"]
        return subprocess.CompletedProcess(command, 0, "runner\\person\n", "")

    module._run = run
    module._verify_windows_task_definition(
        executable,
        scratch=tmp_path,
        environment={"USERNAME": "person", "USERDOMAIN": "runner"},
    )


def test_windows_trace_defers_to_durable_failure_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_script()
    monkeypatch.setattr(module.sys, "platform", "win32")
    monkeypatch.setattr(
        module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("Windows lifecycle tracing started a process"),
    )
    trace = module._native_lifecycle_trace(scratch=tmp_path, environment={})

    trace.start()
    events = trace.stop()

    assert events == ()
