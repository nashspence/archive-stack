from __future__ import annotations

import importlib.metadata
import json
import os
import signal
import sqlite3
import stat
import sys
import threading
import time
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path

import gogurt.listener as listener_module
import pytest
from config_validation import ConfigError
from gogurt.core import load_gogurt_actions, write_gogurt_marker
from gogurt.core import plan_gogurt_action as core_plan_gogurt_action
from gogurt.listener import (
    LISTENER_CONFIG_SCHEMA,
    ListenerConfig,
    ListenerError,
    ListenerLock,
    ListenerRuntime,
    ListenerStore,
    _logger,
    install_listener,
    listener_status,
    stop_listener,
    uninstall_listener,
)
from gogurt.listener_platform import ListenerPaths, NativeListenerStatus


def _paths(tmp_path: Path) -> ListenerPaths:
    state = tmp_path / "state"
    return ListenerPaths(
        state_dir=state,
        config_file=state / "listener.json",
        database_file=state / "listener.sqlite3",
        heartbeat_file=state / "heartbeat.json",
        lock_file=state / "listener.lock",
        log_file=state / "listener.log",
        stop_file=state / "stop.request",
        registration_file=tmp_path / "registration",
    )


def _fixture(tmp_path: Path) -> tuple[ListenerConfig, ListenerPaths, Path, Path]:
    mount = tmp_path / "mount"
    mount.mkdir()
    counter = tmp_path / "counter.txt"
    action = tmp_path / "action.py"
    action.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "path = Path(sys.argv[2])\n"
        "path.write_text(path.read_text() + 'run\\n' if path.exists() else 'run\\n')\n",
        encoding="utf-8",
    )
    routes = tmp_path / "routes.yaml"
    routes.write_text(
        "schema_version: 1\n"
        "kind: gogurt.routes\n"
        "routes:\n"
        "  camera:\n"
        "    command:\n"
        f"      - {json.dumps(sys.executable)}\n"
        f"      - {json.dumps(str(action))}\n"
        '      - "{mount_point}"\n'
        f"      - {json.dumps(str(counter))}\n",
        encoding="utf-8",
    )
    (mount / ".gogurt").write_text("camera\n", encoding="utf-8")
    paths = _paths(tmp_path)
    config = ListenerConfig(
        executable=Path(sys.executable),
        routes_file=routes,
        actions_dir=None,
        marker_name=".gogurt",
        interval_seconds=0.1,
        state_dir=paths.state_dir,
    )
    return config, paths, mount, counter


def _wait_for_runs(counter: Path, expected: int) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if counter.is_file() and len(counter.read_text(encoding="utf-8").splitlines()) >= expected:
            return
        time.sleep(0.02)
    raise AssertionError(f"Gogurt action did not reach {expected} runs")


def _heartbeat_payload(paths: ListenerPaths) -> dict[str, object]:
    payload = listener_module._read_heartbeat(paths.heartbeat_file)
    assert payload is not None
    return payload


def _wait_for_health_value(
    paths: ListenerPaths,
    adapter: FakeAdapter,
    expected: str,
) -> dict[str, object]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        status = listener_status(paths=paths, adapter=adapter)
        if status["health"] == expected:
            return status
        time.sleep(0.02)
    raise AssertionError(f"Gogurt listener did not report {expected}")


def test_listener_config_is_versioned_absolute_and_autorun(tmp_path: Path) -> None:
    config, paths, _mount, _counter = _fixture(tmp_path)
    config.write(paths.config_file)

    payload = json.loads(paths.config_file.read_text(encoding="utf-8"))
    assert payload["schema"] == LISTENER_CONFIG_SCHEMA
    assert payload["autorun"] is True
    assert Path(payload["executable"]).is_absolute()
    assert ListenerConfig.read(paths.config_file) == config

    payload["autorun"] = False
    paths.config_file.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ListenerError, match="explicitly enable autorun"):
        ListenerConfig.read(paths.config_file)


def test_listener_config_json_and_fields_are_strict(tmp_path: Path) -> None:
    config, paths, _mount, _counter = _fixture(tmp_path)
    payload = config.payload()
    invalid_payloads = [
        json.dumps(payload)[:-1] + ', "autorun": true}',
        json.dumps(payload | {"interval_seconds": True}),
        json.dumps(payload | {"interval_seconds": "2"}),
        json.dumps(payload | {"unexpected": "field"}),
        json.dumps(payload).replace('"interval_seconds": 0.1', '"interval_seconds": NaN'),
    ]

    for content in invalid_payloads:
        paths.config_file.parent.mkdir(parents=True, exist_ok=True)
        paths.config_file.write_text(content, encoding="utf-8")
        with pytest.raises(ListenerError):
            ListenerConfig.read(paths.config_file)

    bounded = payload | {"interval_seconds": 3600.0}
    paths.config_file.write_text(json.dumps(bounded), encoding="utf-8")
    assert ListenerConfig.read(paths.config_file).interval_seconds == 3600.0


def test_listener_runs_once_across_restart_and_again_after_remount(tmp_path: Path) -> None:
    config, paths, mount, counter = _fixture(tmp_path)
    first = ListenerRuntime(config, paths, discover=lambda: [mount])
    thread = threading.Thread(target=first.run)
    thread.start()
    try:
        _wait_for_runs(counter, 1)
    finally:
        first.request_stop()
        thread.join(timeout=5)
    assert not thread.is_alive()

    mounted = threading.Event()
    mounted.set()
    absence_observed = threading.Event()

    def discover() -> list[Path]:
        if mounted.is_set():
            return [mount]
        absence_observed.set()
        return []

    second = ListenerRuntime(config, paths, discover=discover)
    thread = threading.Thread(target=second.run)
    thread.start()
    try:
        time.sleep(0.3)
        assert counter.read_text(encoding="utf-8").splitlines() == ["run"]

        mounted.clear()
        assert absence_observed.wait(timeout=5)
        mounted.set()
        _wait_for_runs(counter, 2)
    finally:
        second.request_stop()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert counter.read_text(encoding="utf-8").splitlines() == ["run", "run"]


def test_same_route_marker_write_does_not_create_a_second_dispatch(tmp_path: Path) -> None:
    config, paths, mount, _counter = _fixture(tmp_path)
    store = ListenerStore(paths.database_file)
    store.create()
    [dispatch_id] = store.observe(
        [mount],
        lambda point: core_plan_gogurt_action(config.routes_file, point),
        now=1,
    )
    assert store.start_dispatch(dispatch_id, now=2) is not None
    assert store.finish_dispatch(dispatch_id, return_code=0, error=None, now=3) == "completed"

    marker = mount / config.marker_name
    marker_identity = core_plan_gogurt_action(config.routes_file, mount)["marker_identity"]
    assert write_gogurt_marker(config.routes_file, "camera", mount) == marker
    queued = store.observe(
        [mount],
        lambda point: core_plan_gogurt_action(config.routes_file, point),
        now=4,
    )

    assert queued == []
    assert core_plan_gogurt_action(config.routes_file, mount)["marker_identity"] == marker_identity
    with closing(sqlite3.connect(paths.database_file)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM dispatches").fetchone() == (1,)


@pytest.mark.parametrize("dispatch_state", ["completed", "running"])
def test_discovery_failure_preserves_mount_generation_without_replay(
    tmp_path: Path,
    dispatch_state: str,
) -> None:
    config, paths, mount, _counter = _fixture(tmp_path)
    runtime = ListenerRuntime(config, paths, discover=lambda: [mount])
    runtime.store.create()
    [dispatch_id] = runtime.store.observe([mount], runtime._planner, now=1)
    assert runtime.store.start_dispatch(dispatch_id, now=2) is not None
    if dispatch_state == "completed":
        assert (
            runtime.store.finish_dispatch(
                dispatch_id,
                return_code=0,
                error=None,
                now=3,
            )
            == "completed"
        )

    def fail_discovery() -> Sequence[Path]:
        raise OSError("temporary discovery failure")

    runtime.discover = fail_discovery
    runtime.run_once()
    with closing(sqlite3.connect(paths.database_file)) as connection:
        assert connection.execute("SELECT present, generation FROM observed_mounts").fetchone() == (
            1,
            1,
        )
        assert connection.execute("SELECT COUNT(*) FROM dispatches").fetchone() == (1,)

    runtime.discover = lambda: [mount]
    runtime.run_once()
    with closing(sqlite3.connect(paths.database_file)) as connection:
        assert connection.execute("SELECT present, generation FROM observed_mounts").fetchone() == (
            1,
            1,
        )
        assert connection.execute("SELECT COUNT(*) FROM dispatches").fetchone() == (1,)
    assert runtime.dispatch_queue.empty()

    runtime.discover = lambda: []
    runtime.run_once()
    runtime.discover = lambda: [mount]
    runtime.run_once()
    with closing(sqlite3.connect(paths.database_file)) as connection:
        assert connection.execute("SELECT present, generation FROM observed_mounts").fetchone() == (
            1,
            2,
        )
        assert connection.execute("SELECT COUNT(*) FROM dispatches").fetchone() == (2,)
    assert not runtime.dispatch_queue.empty()


def test_per_mount_access_failure_preserves_generation_and_completed_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, paths, mount, _counter = _fixture(tmp_path)
    runtime = ListenerRuntime(config, paths, discover=lambda: [mount])
    runtime.store.create()
    [dispatch_id] = runtime.store.observe([mount], runtime._planner, now=1)
    assert runtime.store.start_dispatch(dispatch_id, now=2) is not None
    assert runtime.store.finish_dispatch(dispatch_id, return_code=0, error=None, now=3) == (
        "completed"
    )

    def inaccessible(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise OSError("mounted media is temporarily inaccessible")

    monkeypatch.setattr(listener_module, "plan_gogurt_action", inaccessible)
    runtime.run_once()
    monkeypatch.setattr(listener_module, "plan_gogurt_action", core_plan_gogurt_action)
    runtime.run_once()

    with closing(sqlite3.connect(paths.database_file)) as connection:
        assert connection.execute("SELECT present, generation FROM observed_mounts").fetchone() == (
            1,
            1,
        )
        assert connection.execute("SELECT COUNT(*) FROM dispatches").fetchone() == (1,)
    assert runtime.dispatch_queue.empty()


def test_unexpected_worker_failure_terminates_with_failed_runtime_health(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, paths, _mount, _counter = _fixture(tmp_path)
    runtime = ListenerRuntime(config, paths, discover=lambda: [])

    def fail_worker() -> None:
        raise sqlite3.OperationalError("worker state failed")

    monkeypatch.setattr(runtime, "_worker", fail_worker)

    with pytest.raises(ListenerError, match="dispatch worker"):
        runtime.run()

    heartbeat = _heartbeat_payload(paths)
    assert heartbeat["runtime"]["status"] == "failed"
    assert "dispatch worker" in heartbeat["runtime"]["diagnostic"]


def test_listener_process_logs_an_unhandled_runtime_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, paths, _mount, _counter = _fixture(tmp_path)
    config.write(paths.config_file)

    def fail_runtime(_runtime: ListenerRuntime) -> None:
        raise ListenerError("qualification fatal fixture")

    monkeypatch.setattr(ListenerRuntime, "run", fail_runtime)
    enabled: list[tuple[int, bool]] = []
    disabled: list[bool] = []
    monkeypatch.setattr(
        listener_module.faulthandler,
        "enable",
        lambda *, file, all_threads: enabled.append((os.fstat(file.fileno()).st_ino, all_threads)),
    )
    monkeypatch.setattr(
        listener_module.faulthandler,
        "disable",
        lambda: disabled.append(True),
    )

    with pytest.raises(ListenerError, match="qualification fatal fixture"):
        listener_module.run_listener(paths.config_file)

    logger = listener_module.logging.getLogger("gogurt.listener")
    for handler in logger.handlers:
        handler.close()
    logger.handlers.clear()
    log = paths.log_file.read_text(encoding="utf-8")
    assert "listener failed pid=" in log
    assert "runtime: ListenerError: qualification fatal fixture" in log
    assert len(enabled) == 1
    assert enabled[0][1] is True
    assert enabled[0][0] == (paths.state_dir / "listener.fatal.log").stat().st_ino
    assert disabled == [True]
    assert (paths.state_dir / "listener.fatal.log").is_file()


def test_interrupted_dispatch_becomes_observable_uncertain_state(tmp_path: Path) -> None:
    config, paths, mount, _counter = _fixture(tmp_path)
    store = ListenerStore(paths.database_file)
    store.create()
    dispatches = store.observe(
        [mount],
        lambda value: {
            "status": "ready",
            "route": "camera",
            "mount_point": str(value),
            "marker": str(value / ".gogurt"),
            "marker_identity": "identity",
            "command": [sys.executable, "-c", "pass"],
        },
        now=1,
    )
    assert len(dispatches) == 1
    assert store.start_dispatch(dispatches[0], now=2) is not None

    ListenerStore(paths.database_file).create()
    summary = store.summary()
    assert summary["counts"] == {"uncertain": 1}
    attention = summary["attention"]
    assert isinstance(attention, list)
    assert isinstance(attention[0], dict)
    assert attention[0]["state"] == "uncertain"
    assert config.autorun is True


def test_controlled_stop_does_not_replay_interrupted_custody(tmp_path: Path) -> None:
    config, paths, mount, counter = _fixture(tmp_path)
    action = Path(load_gogurt_actions(config.routes_file)[0].command[1])
    action.write_text(
        "from pathlib import Path\n"
        "import sys,time\n"
        "path = Path(sys.argv[2])\n"
        "path.write_text(path.read_text() + 'run\\n' if path.exists() else 'run\\n')\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    runtime = ListenerRuntime(config, paths, discover=lambda: [mount])
    thread = threading.Thread(target=runtime.run)
    thread.start()
    try:
        _wait_for_runs(counter, 1)
    finally:
        runtime.request_stop()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert ListenerStore(paths.database_file).summary()["counts"] == {"uncertain": 1}

    restarted = ListenerRuntime(config, paths, discover=lambda: [mount])
    thread = threading.Thread(target=restarted.run)
    thread.start()
    try:
        time.sleep(0.4)
    finally:
        restarted.request_stop()
        thread.join(timeout=5)
    assert counter.read_text(encoding="utf-8").splitlines() == ["run"]


def test_process_signal_can_stop_while_heartbeat_samples_action_custody(
    tmp_path: Path,
) -> None:
    config, paths, _mount, _counter = _fixture(tmp_path)
    runtime = ListenerRuntime(config, paths, discover=lambda: [])
    settled = threading.Event()

    def interrupt_heartbeat_sample() -> None:
        # Python invokes a process-control signal handler on the interrupted
        # main-thread stack. Model that exact re-entry while heartbeat holds
        # the custody lock, without sending a real signal to the test runner.
        with runtime._active_lock:
            runtime.request_stop()
        settled.set()

    thread = threading.Thread(target=interrupt_heartbeat_sample, daemon=True)
    thread.start()
    thread.join(timeout=1)

    assert settled.is_set()
    assert runtime.stop_event.is_set()


def test_official_heartbeat_observation_does_not_own_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, paths, _mount, _counter = _fixture(tmp_path)
    runtime = ListenerRuntime(config, paths, discover=lambda: [])
    runtime.store.create()
    runtime._heartbeat()
    real_atomic_write = listener_module.atomic_write
    observed_during_publication: dict[str, object] | None = None

    def publish(destination: Path, content: bytes, *, mode: int) -> None:
        nonlocal observed_during_publication
        observed_during_publication = listener_module._read_heartbeat(paths.heartbeat_file)
        real_atomic_write(destination, content, mode=mode)

    monkeypatch.setattr(listener_module, "atomic_write", publish)
    runtime._heartbeat()

    assert observed_during_publication is not None
    assert observed_during_publication["runtime"] == {
        "diagnostic": None,
        "status": "running",
    }
    assert _heartbeat_payload(paths)["runtime"] == {
        "diagnostic": None,
        "status": "running",
    }


def test_shutdown_force_settles_an_action_that_ignores_termination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, paths, mount, pid_file = _fixture(tmp_path)
    action = Path(load_gogurt_actions(config.routes_file)[0].command[1])
    action.write_text(
        "from pathlib import Path\n"
        "import os,signal,sys,time\n"
        "signal.signal(signal.SIGTERM, lambda *_args: None)\n"
        "path=Path(sys.argv[2])\n"
        "staged=path.with_name(path.name + '.tmp')\n"
        "staged.write_text(str(os.getpid()), encoding='utf-8')\n"
        "os.replace(staged, path)\n"
        "while True:\n"
        "    time.sleep(1)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(listener_module, "LISTENER_ACTION_TERMINATE_SECONDS", 0.1)
    monkeypatch.setattr(listener_module, "LISTENER_ACTION_KILL_SECONDS", 0.5)
    runtime = ListenerRuntime(config, paths, discover=lambda: [mount])
    failures: list[BaseException] = []

    def run() -> None:
        try:
            runtime.run()
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    action_pid: int | None = None
    try:
        deadline = time.monotonic() + 5
        while not pid_file.is_file() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert pid_file.is_file()
        action_pid = int(pid_file.read_text(encoding="utf-8"))
        runtime.request_stop()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert failures == []
        assert listener_module._process_is_running(action_pid) is False
        assert ListenerStore(paths.database_file).summary()["counts"] == {"uncertain": 1}
    finally:
        runtime.request_stop()
        thread.join(timeout=5)
        if action_pid is not None and listener_module._process_is_running(action_pid):
            os.kill(action_pid, signal.SIGKILL)


def test_cooperative_stop_request_settles_active_custody_independently_of_poll_interval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, paths, mount, pid_file = _fixture(tmp_path)
    config = ListenerConfig(
        executable=config.executable,
        routes_file=config.routes_file,
        actions_dir=config.actions_dir,
        marker_name=config.marker_name,
        interval_seconds=3600,
        state_dir=config.state_dir,
    )
    action = Path(load_gogurt_actions(config.routes_file)[0].command[1])
    action.write_text(
        "from pathlib import Path\n"
        "import os,signal,sys,time\n"
        "signal.signal(signal.SIGTERM, lambda *_args: None)\n"
        "Path(sys.argv[2]).write_text(str(os.getpid()), encoding='utf-8')\n"
        "while True:\n"
        "    time.sleep(1)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(listener_module, "LISTENER_ACTION_TERMINATE_SECONDS", 0.1)
    monkeypatch.setattr(listener_module, "LISTENER_ACTION_KILL_SECONDS", 0.5)
    runtime = ListenerRuntime(config, paths, discover=lambda: [mount])
    thread = threading.Thread(target=runtime.run)
    thread.start()
    action_pid: int | None = None
    try:
        deadline = time.monotonic() + 5
        while not pid_file.is_file() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert pid_file.is_file()
        action_pid = int(pid_file.read_text(encoding="utf-8"))
        paths.stop_file.write_text("stop\n", encoding="utf-8")
        thread.join(timeout=5)

        assert not thread.is_alive()
        assert listener_module._process_is_running(action_pid) is False
        assert ListenerStore(paths.database_file).summary()["counts"] == {"uncertain": 1}
    finally:
        runtime.request_stop()
        thread.join(timeout=5)
        if action_pid is not None and listener_module._process_is_running(action_pid):
            os.kill(action_pid, signal.SIGKILL)


def test_listener_lock_prevents_concurrent_runtime_ownership(tmp_path: Path) -> None:
    path = tmp_path / "listener.lock"
    with ListenerLock(path):
        with pytest.raises(ListenerError, match="already owns"):
            with ListenerLock(path):
                pass


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission contract")
def test_listener_state_is_private_from_creation_and_normalizes_existing_files(
    tmp_path: Path,
) -> None:
    config, paths, _mount, _counter = _fixture(tmp_path)
    executable = tmp_path / "installed" / "gogurt"
    executable.parent.mkdir()
    executable.write_text("fixture", encoding="utf-8")
    executable.chmod(0o755)
    paths.state_dir.mkdir(mode=0o755)
    paths.config_file.write_bytes(config.content())
    for path in (
        paths.database_file,
        paths.heartbeat_file,
        paths.lock_file,
        paths.log_file,
        paths.state_dir / "listener.log.1",
        paths.state_dir / "listener.fatal.log",
        paths.state_dir / "listener.fatal.log.1",
    ):
        path.touch()
        path.chmod(0o644)
    paths.state_dir.chmod(0o755)

    previous_umask = os.umask(0o022)
    logger = None
    try:
        install_listener(
            config.routes_file,
            actions_dir=None,
            executable=executable,
            paths=paths,
            adapter=FakeAdapter(),
            wait_for_health=False,
        )
        ListenerStore(paths.database_file).create()
        with ListenerLock(paths.lock_file):
            pass
        runtime = ListenerRuntime(config, paths, discover=lambda: [])
        runtime.run_once()
        logger = _logger(paths)
        logger.info("private log proof")
    finally:
        os.umask(previous_umask)
        if logger is not None:
            for handler in logger.handlers:
                handler.close()
            logger.handlers.clear()

    assert stat.S_IMODE(paths.state_dir.stat().st_mode) == 0o700
    private_files = [
        paths.config_file,
        paths.database_file,
        paths.heartbeat_file,
        paths.lock_file,
        paths.log_file,
        paths.state_dir / "listener.log.1",
        paths.state_dir / "listener.fatal.log",
        paths.state_dir / "listener.fatal.log.1",
    ]
    private_files.extend(paths.state_dir.glob("listener.sqlite3-*"))
    assert private_files
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in private_files)


def test_listener_state_security_leaves_sqlite_sidecars_under_sqlite_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _config, paths, _mount, _counter = _fixture(tmp_path)
    paths.state_dir.mkdir()
    wal = paths.state_dir / "listener.sqlite3-wal"
    shm = paths.state_dir / "listener.sqlite3-shm"
    wal.touch()
    shm.touch()
    secured: list[Path] = []

    monkeypatch.setattr(
        listener_module,
        "ensure_private_files",
        lambda paths: secured.extend(paths),
    )

    listener_module._secure_listener_state(paths)

    assert paths.database_file in secured
    assert wal not in secured
    assert shm not in secured


def test_listener_store_negotiates_wal_only_during_initialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ListenerStore(tmp_path / "listener.sqlite3")
    store.create()
    with closing(sqlite3.connect(store.path)) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)

    statements: list[str] = []
    real_connect = sqlite3.connect

    def traced_connect(database: str | Path, timeout: float = 5.0) -> sqlite3.Connection:
        connection = real_connect(database, timeout=timeout)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(sqlite3, "connect", traced_connect)
    assert store.summary() == {"counts": {}, "attention": []}
    assert not any("journal_mode" in statement.casefold() for statement in statements)


def test_listener_store_does_not_normalize_sqlite_owned_sidecars(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse_sidecar_normalization(_paths: object) -> None:
        raise AssertionError("active SQLite sidecars must remain under SQLite ownership")

    monkeypatch.setattr(listener_module, "ensure_private_files", refuse_sidecar_normalization)

    store = ListenerStore(tmp_path / "listener.sqlite3")
    store.create()
    assert store.summary() == {"counts": {}, "attention": []}


def test_listener_runtime_keeps_one_database_connection_until_settled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, paths, _mount, _counter = _fixture(tmp_path)
    active_connections = 0
    observed_during_poll: list[int] = []
    real_connect = sqlite3.connect

    class TrackedConnection(sqlite3.Connection):
        def __init__(self, *args: object, **kwargs: object) -> None:
            nonlocal active_connections
            super().__init__(*args, **kwargs)
            active_connections += 1

        def close(self) -> None:
            nonlocal active_connections
            if active_connections > 0:
                active_connections -= 1
            super().close()

    def tracked_connect(database: str | Path, timeout: float = 5.0) -> sqlite3.Connection:
        return real_connect(database, timeout=timeout, factory=TrackedConnection)

    monkeypatch.setattr(sqlite3, "connect", tracked_connect)
    runtime = ListenerRuntime(config, paths, discover=lambda: [])

    def one_poll() -> None:
        observed_during_poll.append(active_connections)
        runtime.request_stop()

    monkeypatch.setattr(runtime, "run_once", one_poll)

    runtime.run()

    assert observed_during_poll == [1]
    assert active_connections == 0


class FakeAdapter:
    def __init__(
        self,
        *,
        fail_registration: bool = False,
        fail_register_calls: set[int] | None = None,
    ) -> None:
        self.installed = False
        self.running = False
        self.fail_registration = fail_registration
        self.fail_register_calls = fail_register_calls or set()
        self.register_calls = 0
        self.stop_calls = 0
        self.unregister_calls = 0
        self.commands: list[list[str]] = []

    def register(self, paths: ListenerPaths, command: Sequence[str]) -> None:
        del paths
        self.register_calls += 1
        self.commands.append(list(command))
        if self.fail_registration or self.register_calls in self.fail_register_calls:
            raise OSError("startup failed")
        self.installed = True
        self.running = True

    def status(self, paths: ListenerPaths) -> NativeListenerStatus:
        del paths
        return NativeListenerStatus(self.installed, self.installed, self.running)

    def start(self, paths: ListenerPaths) -> None:
        del paths
        self.running = True

    def stop(self, paths: ListenerPaths) -> None:
        del paths
        self.stop_calls += 1
        self.running = False

    def unregister(self, paths: ListenerPaths) -> None:
        del paths
        self.unregister_calls += 1
        self.installed = False
        self.running = False


def test_install_binds_absolute_executable_and_rolls_back_failed_startup(tmp_path: Path) -> None:
    config, paths, _mount, _counter = _fixture(tmp_path)
    executable = tmp_path / "installed" / "gogurt"
    executable.parent.mkdir()
    executable.write_text("fixture", encoding="utf-8")
    executable.chmod(0o755)
    adapter = FakeAdapter()

    status = install_listener(
        config.routes_file,
        actions_dir=None,
        executable=executable,
        paths=paths,
        adapter=adapter,
        wait_for_health=False,
    )
    assert status["installed"] is True
    with closing(sqlite3.connect(paths.database_file)) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert connection.execute(
            "SELECT value FROM listener_meta WHERE key = 'schema'"
        ).fetchone() == ("1",)
    assert adapter.commands == [
        [
            str(executable.resolve()),
            "listener",
            "_run",
            "--runtime-config",
            str(paths.config_file),
        ]
    ]

    uninstall_listener(paths=paths, adapter=adapter)
    assert not paths.state_dir.exists()

    failing = FakeAdapter(fail_registration=True)
    with pytest.raises(OSError, match="startup failed"):
        install_listener(
            config.routes_file,
            actions_dir=None,
            executable=executable,
            paths=paths,
            adapter=failing,
            wait_for_health=False,
        )
    assert failing.installed is False
    assert not paths.config_file.exists()


def test_exact_healthy_install_is_idempotent_without_native_process_churn(
    tmp_path: Path,
) -> None:
    config, paths, _mount, _counter = _fixture(tmp_path)
    executable = tmp_path / "installed" / "gogurt"
    executable.parent.mkdir()
    executable.write_text("fixture", encoding="utf-8")
    executable.chmod(0o755)
    adapter = FakeAdapter()

    install_listener(
        config.routes_file,
        actions_dir=None,
        interval_seconds=0.1,
        executable=executable,
        paths=paths,
        adapter=adapter,
        wait_for_health=False,
    )
    persisted = ListenerConfig.read(paths.config_file)
    runtime = ListenerRuntime(persisted, paths, discover=lambda: [])
    runtime.store.create()
    runtime._heartbeat()
    existing_content = paths.config_file.read_bytes()

    status = install_listener(
        config.routes_file,
        actions_dir=None,
        interval_seconds=0.1,
        executable=executable,
        paths=paths,
        adapter=adapter,
    )

    assert status["health"] == "healthy"
    assert adapter.register_calls == 1
    assert adapter.stop_calls == 0
    assert paths.config_file.read_bytes() == existing_content


def test_install_rejects_invalid_marker_name_before_registration(tmp_path: Path) -> None:
    config, paths, _mount, _counter = _fixture(tmp_path)
    executable = tmp_path / "installed" / "gogurt"
    executable.parent.mkdir()
    executable.write_text("fixture", encoding="utf-8")
    executable.chmod(0o755)
    adapter = FakeAdapter()

    with pytest.raises(ConfigError, match="invalid gogurt marker name"):
        install_listener(
            config.routes_file,
            actions_dir=None,
            marker_name="nested/.gogurt",
            executable=executable,
            paths=paths,
            adapter=adapter,
            wait_for_health=False,
        )

    assert adapter.register_calls == 0
    assert adapter.stop_calls == 0
    assert not paths.config_file.exists()


def test_install_rejects_missing_static_action_before_registration(tmp_path: Path) -> None:
    config, paths, _mount, _counter = _fixture(tmp_path)
    config.routes_file.write_text(
        "schema_version: 1\n"
        "kind: gogurt.routes\n"
        "routes:\n"
        "  camera:\n"
        "    command:\n"
        "      - definitely-absent-gogurt-action\n"
        '      - "{mount_point}"\n',
        encoding="utf-8",
    )
    executable = tmp_path / "installed" / "gogurt"
    executable.parent.mkdir()
    executable.write_text("fixture", encoding="utf-8")
    executable.chmod(0o755)
    adapter = FakeAdapter()

    with pytest.raises(FileNotFoundError, match="action executable not found"):
        install_listener(
            config.routes_file,
            actions_dir=None,
            executable=executable,
            paths=paths,
            adapter=adapter,
            wait_for_health=False,
        )

    assert adapter.register_calls == 0
    assert adapter.stop_calls == 0
    assert not paths.config_file.exists()


def test_impossible_mount_executable_is_rejected_before_and_after_registration(
    tmp_path: Path,
) -> None:
    config, paths, _mount, _counter = _fixture(tmp_path)
    config.routes_file.write_text(
        "schema_version: 1\n"
        "kind: gogurt.routes\n"
        "routes:\n"
        "  camera:\n"
        "    command:\n"
        '      - "{mount_point}"\n',
        encoding="utf-8",
    )
    executable = tmp_path / "installed" / "gogurt"
    executable.parent.mkdir()
    executable.write_text("fixture", encoding="utf-8")
    executable.chmod(0o755)
    adapter = FakeAdapter()

    with pytest.raises(ConfigError, match="cannot use .*mount_point.* as its executable"):
        install_listener(
            config.routes_file,
            actions_dir=None,
            executable=executable,
            paths=paths,
            adapter=adapter,
            wait_for_health=False,
        )
    assert adapter.register_calls == 0

    persisted = ListenerConfig(
        executable=executable.resolve(),
        routes_file=config.routes_file,
        actions_dir=None,
        marker_name=".gogurt",
        interval_seconds=0.1,
        state_dir=paths.state_dir,
    )
    persisted.write(paths.config_file)
    runtime = ListenerRuntime(persisted, paths, discover=lambda: [])
    runtime.store.create()
    runtime.run_once()
    heartbeat = _heartbeat_payload(paths)
    assert heartbeat["configuration"]["status"] == "failed"
    assert "cannot use" in heartbeat["configuration"]["diagnostic"]


def test_missing_installed_static_action_reports_failed_health_without_dispatch(
    tmp_path: Path,
) -> None:
    config, paths, mount, _counter = _fixture(tmp_path)
    actions_dir = tmp_path / "actions"
    actions_dir.mkdir()
    action = actions_dir / "archive-camera"
    action.write_text("fixture", encoding="utf-8")
    action.chmod(0o755)
    config.routes_file.write_text(
        "schema_version: 1\n"
        "kind: gogurt.routes\n"
        "routes:\n"
        "  camera:\n"
        "    command:\n"
        "      - archive-camera\n"
        '      - "{mount_point}"\n',
        encoding="utf-8",
    )
    runtime_config = ListenerConfig(
        executable=config.executable,
        routes_file=config.routes_file,
        actions_dir=actions_dir,
        marker_name=config.marker_name,
        interval_seconds=config.interval_seconds,
        state_dir=config.state_dir,
    )
    runtime_config.write(paths.config_file)
    runtime = ListenerRuntime(runtime_config, paths, discover=lambda: [mount])
    runtime.store.create()

    action.unlink()
    runtime.run_once()

    heartbeat = _heartbeat_payload(paths)
    assert heartbeat["configuration"]["status"] == "failed"
    assert "action executable not found" in heartbeat["configuration"]["diagnostic"]
    assert heartbeat["dispatches"]["counts"] == {}
    assert runtime.dispatch_queue.empty()


@pytest.mark.parametrize("replacement", [False, True])
def test_native_status_failure_preserves_the_existing_installation_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement: bool,
) -> None:
    config, paths, _mount, _counter = _fixture(tmp_path)
    executable = tmp_path / "installed" / "gogurt"
    executable.parent.mkdir()
    executable.write_text("fixture", encoding="utf-8")
    executable.chmod(0o755)
    adapter = FakeAdapter()
    previous_content: bytes | None = None
    if replacement:
        install_listener(
            config.routes_file,
            actions_dir=None,
            executable=executable,
            paths=paths,
            adapter=adapter,
            wait_for_health=False,
        )
        previous_content = paths.config_file.read_bytes()

    def fail_status(_paths: ListenerPaths) -> NativeListenerStatus:
        raise OSError("native status unavailable")

    monkeypatch.setattr(adapter, "status", fail_status)

    with pytest.raises(OSError, match="native status unavailable"):
        install_listener(
            config.routes_file,
            actions_dir=None,
            interval_seconds=0.2,
            executable=executable,
            paths=paths,
            adapter=adapter,
            wait_for_health=False,
        )

    assert adapter.stop_calls == 0
    assert adapter.unregister_calls == 0
    if replacement:
        assert adapter.installed is True
        assert adapter.running is True
        assert paths.config_file.read_bytes() == previous_content
    else:
        assert adapter.installed is False
        assert not paths.config_file.exists()


def test_install_rollback_aggregates_bounded_single_line_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, paths, _mount, _counter = _fixture(tmp_path)
    executable = tmp_path / "installed" / "gogurt"
    executable.parent.mkdir()
    executable.write_text("fixture", encoding="utf-8")
    executable.chmod(0o755)
    adapter = FakeAdapter(fail_registration=True)

    def fail_unregister(_paths: ListenerPaths) -> None:
        raise OSError("cleanup failed\n" + "bounded-detail-" * 100)

    monkeypatch.setattr(adapter, "unregister", fail_unregister)

    with pytest.raises(ListenerError) as raised:
        install_listener(
            config.routes_file,
            actions_dir=None,
            executable=executable,
            paths=paths,
            adapter=adapter,
            wait_for_health=False,
        )

    diagnostic = str(raised.value)
    assert "Gogurt listener installation failed: OSError: startup failed" in diagnostic
    assert "remove failed registration: OSError: cleanup failed" in diagnostic
    assert "\n" not in diagnostic
    assert len(diagnostic) <= 1200
    assert not paths.config_file.exists()


def test_first_install_rolls_back_sqlite_initialization_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, paths, _mount, _counter = _fixture(tmp_path)
    executable = tmp_path / "installed" / "gogurt"
    executable.parent.mkdir()
    executable.write_text("fixture", encoding="utf-8")
    executable.chmod(0o755)
    adapter = FakeAdapter()

    def fail_create(_store: ListenerStore) -> None:
        raise sqlite3.OperationalError("schema unavailable")

    monkeypatch.setattr(ListenerStore, "create", fail_create)

    with pytest.raises(sqlite3.OperationalError, match="schema unavailable"):
        install_listener(
            config.routes_file,
            actions_dir=None,
            executable=executable,
            paths=paths,
            adapter=adapter,
            wait_for_health=False,
        )

    assert adapter.installed is False
    assert adapter.unregister_calls == 1
    assert not paths.config_file.exists()


def test_replacement_rolls_back_sqlite_initialization_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, paths, _mount, _counter = _fixture(tmp_path)
    executable = tmp_path / "installed" / "gogurt"
    executable.parent.mkdir()
    executable.write_text("fixture", encoding="utf-8")
    executable.chmod(0o755)
    adapter = FakeAdapter()
    install_listener(
        config.routes_file,
        actions_dir=None,
        interval_seconds=0.1,
        executable=executable,
        paths=paths,
        adapter=adapter,
        wait_for_health=False,
    )
    previous_content = paths.config_file.read_bytes()

    def instant_health(expected: frozenset[str], **_kwargs: object) -> dict[str, object]:
        return {"health": sorted(expected)[0]}

    def fail_create(_store: ListenerStore) -> None:
        raise sqlite3.OperationalError("schema unavailable")

    monkeypatch.setattr(listener_module, "_wait_for_health", instant_health)
    monkeypatch.setattr(ListenerStore, "create", fail_create)

    with pytest.raises(sqlite3.OperationalError, match="schema unavailable"):
        install_listener(
            config.routes_file,
            actions_dir=None,
            interval_seconds=0.2,
            executable=executable,
            paths=paths,
            adapter=adapter,
            wait_for_health=False,
        )

    assert paths.config_file.read_bytes() == previous_content
    assert adapter.installed is True
    assert adapter.running is True
    assert adapter.register_calls == 2


def test_failed_replacement_restores_the_previous_listener(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, paths, _mount, _counter = _fixture(tmp_path)
    executable = tmp_path / "installed" / "gogurt"
    executable.parent.mkdir()
    executable.write_text("fixture", encoding="utf-8")
    executable.chmod(0o755)
    adapter = FakeAdapter(fail_register_calls={2})
    install_listener(
        config.routes_file,
        actions_dir=None,
        interval_seconds=0.1,
        executable=executable,
        paths=paths,
        adapter=adapter,
        wait_for_health=False,
    )
    health_checks: list[frozenset[str]] = []

    def instant_health(expected: frozenset[str], **_kwargs: object) -> dict[str, object]:
        health_checks.append(expected)
        return {"health": sorted(expected)[0]}

    monkeypatch.setattr("gogurt.listener._wait_for_health", instant_health)

    with pytest.raises(OSError, match="startup failed"):
        install_listener(
            config.routes_file,
            actions_dir=None,
            interval_seconds=0.2,
            executable=executable,
            paths=paths,
            adapter=adapter,
            wait_for_health=False,
        )

    assert ListenerConfig.read(paths.config_file).interval_seconds == 0.1
    assert adapter.installed is True
    assert adapter.running is True
    assert adapter.register_calls == 3
    assert health_checks[-1] == frozenset({"healthy"})


def test_replacement_staging_failure_leaves_previous_listener_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, paths, _mount, _counter = _fixture(tmp_path)
    executable = tmp_path / "installed" / "gogurt"
    executable.parent.mkdir()
    executable.write_text("fixture", encoding="utf-8")
    executable.chmod(0o755)
    adapter = FakeAdapter()
    install_listener(
        config.routes_file,
        actions_dir=None,
        executable=executable,
        paths=paths,
        adapter=adapter,
        wait_for_health=False,
    )
    previous_content = paths.config_file.read_bytes()

    def fail_staging(*_args: object, **_kwargs: object) -> Path:
        raise OSError("staging failed")

    monkeypatch.setattr("gogurt.listener.stage_bytes", fail_staging)

    with pytest.raises(OSError, match="staging failed"):
        install_listener(
            config.routes_file,
            actions_dir=None,
            interval_seconds=0.2,
            executable=executable,
            paths=paths,
            adapter=adapter,
            wait_for_health=False,
        )

    assert adapter.stop_calls == 0
    assert adapter.running is True
    assert paths.config_file.read_bytes() == previous_content


def test_replacement_promotion_failure_restores_proven_healthy_listener(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, paths, _mount, _counter = _fixture(tmp_path)
    executable = tmp_path / "installed" / "gogurt"
    executable.parent.mkdir()
    executable.write_text("fixture", encoding="utf-8")
    executable.chmod(0o755)
    adapter = FakeAdapter()
    install_listener(
        config.routes_file,
        actions_dir=None,
        executable=executable,
        paths=paths,
        adapter=adapter,
        wait_for_health=False,
    )
    previous_content = paths.config_file.read_bytes()
    health_checks: list[frozenset[str]] = []

    def instant_health(expected: frozenset[str], **_kwargs: object) -> dict[str, object]:
        health_checks.append(expected)
        return {"health": sorted(expected)[0]}

    monkeypatch.setattr("gogurt.listener._wait_for_health", instant_health)
    monkeypatch.setattr(
        "gogurt.listener.promote_staged",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("promotion failed")),
    )

    with pytest.raises(OSError, match="promotion failed"):
        install_listener(
            config.routes_file,
            actions_dir=None,
            interval_seconds=0.2,
            executable=executable,
            paths=paths,
            adapter=adapter,
            wait_for_health=False,
        )

    assert paths.config_file.read_bytes() == previous_content
    assert adapter.installed is True
    assert adapter.running is True
    assert adapter.register_calls == 2
    assert health_checks[-1] == frozenset({"healthy"})


def test_unhealthy_replacement_restores_proven_healthy_listener(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, paths, _mount, _counter = _fixture(tmp_path)
    executable = tmp_path / "installed" / "gogurt"
    executable.parent.mkdir()
    executable.write_text("fixture", encoding="utf-8")
    executable.chmod(0o755)
    adapter = FakeAdapter()
    install_listener(
        config.routes_file,
        actions_dir=None,
        executable=executable,
        paths=paths,
        adapter=adapter,
        wait_for_health=False,
    )
    previous_content = paths.config_file.read_bytes()
    healthy_checks = 0

    def replacement_then_rollback_health(
        expected: frozenset[str], **_kwargs: object
    ) -> dict[str, object]:
        nonlocal healthy_checks
        if expected == frozenset({"healthy"}):
            healthy_checks += 1
            if healthy_checks == 1:
                raise RuntimeError("replacement health probe failed")
        return {"health": sorted(expected)[0]}

    monkeypatch.setattr(
        "gogurt.listener._wait_for_health",
        replacement_then_rollback_health,
    )

    with pytest.raises(RuntimeError, match="replacement health probe failed"):
        install_listener(
            config.routes_file,
            actions_dir=None,
            interval_seconds=0.2,
            executable=executable,
            paths=paths,
            adapter=adapter,
        )

    assert paths.config_file.read_bytes() == previous_content
    assert adapter.installed is True
    assert adapter.running is True
    assert adapter.register_calls == 3
    assert healthy_checks == 2


def test_interrupted_first_install_rolls_back_then_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, paths, _mount, _counter = _fixture(tmp_path)
    executable = tmp_path / "installed" / "gogurt"
    executable.parent.mkdir()
    executable.write_text("fixture", encoding="utf-8")
    executable.chmod(0o755)
    adapter = FakeAdapter()

    def interrupt_registration(_paths: ListenerPaths, _command: Sequence[str]) -> None:
        adapter.register_calls += 1
        raise KeyboardInterrupt

    monkeypatch.setattr(adapter, "register", interrupt_registration)

    with pytest.raises(KeyboardInterrupt):
        install_listener(
            config.routes_file,
            actions_dir=None,
            executable=executable,
            paths=paths,
            adapter=adapter,
            wait_for_health=False,
        )

    assert adapter.unregister_calls == 1
    assert adapter.installed is False
    assert not paths.config_file.exists()


def test_interrupted_replacement_restores_prior_health_then_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, paths, _mount, _counter = _fixture(tmp_path)
    executable = tmp_path / "installed" / "gogurt"
    executable.parent.mkdir()
    executable.write_text("fixture", encoding="utf-8")
    executable.chmod(0o755)
    adapter = FakeAdapter()
    install_listener(
        config.routes_file,
        actions_dir=None,
        interval_seconds=0.1,
        executable=executable,
        paths=paths,
        adapter=adapter,
        wait_for_health=False,
    )
    previous_content = paths.config_file.read_bytes()
    original_register = adapter.register
    health_checks: list[frozenset[str]] = []

    def interrupt_replacement(paths_value: ListenerPaths, command: Sequence[str]) -> None:
        if adapter.register_calls == 1:
            adapter.register_calls += 1
            raise KeyboardInterrupt
        original_register(paths_value, command)

    def instant_health(expected: frozenset[str], **_kwargs: object) -> dict[str, object]:
        health_checks.append(expected)
        return {"health": sorted(expected)[0]}

    monkeypatch.setattr(adapter, "register", interrupt_replacement)
    monkeypatch.setattr(listener_module, "_wait_for_health", instant_health)

    with pytest.raises(KeyboardInterrupt):
        install_listener(
            config.routes_file,
            actions_dir=None,
            interval_seconds=0.2,
            executable=executable,
            paths=paths,
            adapter=adapter,
            wait_for_health=False,
        )

    assert paths.config_file.read_bytes() == previous_content
    assert adapter.installed is True
    assert adapter.running is True
    assert adapter.register_calls == 3
    assert health_checks[-1] == frozenset({"healthy"})


def test_global_route_failure_reports_failed_then_recovers_and_dispatches(
    tmp_path: Path,
) -> None:
    config, paths, mount, counter = _fixture(tmp_path)
    config.write(paths.config_file)
    routes_content = config.routes_file.read_text(encoding="utf-8")
    config.routes_file.write_text("not: [valid", encoding="utf-8")
    adapter = FakeAdapter()
    adapter.installed = True
    adapter.running = True
    runtime = ListenerRuntime(config, paths, discover=lambda: [mount])
    thread = threading.Thread(target=runtime.run)
    thread.start()
    try:
        failed = _wait_for_health_value(paths, adapter, "failed")
        assert "global configuration" in str(failed["diagnostic"])
        assert not counter.exists()

        config.routes_file.write_text(routes_content, encoding="utf-8")
        _wait_for_runs(counter, 1)
        healthy = _wait_for_health_value(paths, adapter, "healthy")
        assert healthy["diagnostic"] is None
        assert counter.read_text(encoding="utf-8").splitlines() == ["run"]
    finally:
        config.routes_file.write_text(routes_content, encoding="utf-8")
        runtime.request_stop()
        thread.join(timeout=5)
    assert not thread.is_alive()


def test_invalid_and_unavailable_mounts_are_isolated_from_valid_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, paths, valid_mount, counter = _fixture(tmp_path)
    invalid_mount = tmp_path / "invalid-mount"
    invalid_mount.mkdir()
    (invalid_mount / ".gogurt").write_bytes(b"\xff")
    unavailable_mount = tmp_path / "unavailable-mount"
    unavailable_mount.mkdir()
    (unavailable_mount / ".gogurt").write_text("camera\n", encoding="utf-8")
    unmarked_mount = tmp_path / "unmarked-mount"
    unmarked_mount.mkdir()
    original_plan = core_plan_gogurt_action

    def plan_with_unavailable_mount(
        config_file: str | os.PathLike[str],
        mount_point: str | os.PathLike[str],
        *,
        actions_dir: str | os.PathLike[str] | None = None,
        marker_name: str = ".gogurt",
    ) -> dict[str, object]:
        if Path(str(mount_point)) == unavailable_mount:
            raise OSError("media disappeared")
        return original_plan(
            config_file,
            mount_point,
            actions_dir=actions_dir,
            marker_name=marker_name,
        )

    monkeypatch.setattr(listener_module, "plan_gogurt_action", plan_with_unavailable_mount)
    runtime = ListenerRuntime(
        config,
        paths,
        discover=lambda: [invalid_mount, unavailable_mount, unmarked_mount, valid_mount],
    )
    thread = threading.Thread(target=runtime.run)
    thread.start()
    try:
        _wait_for_runs(counter, 1)
        assert thread.is_alive()
        deadline = time.monotonic() + 5
        attention: list[object] = []
        while time.monotonic() < deadline:
            heartbeat = _heartbeat_payload(paths)
            attention = heartbeat["mount_attention"]
            if len(attention) == 2:
                break
            time.sleep(0.02)
        attention_paths = {item["mount_point"] for item in attention if isinstance(item, dict)}
        assert attention_paths == {str(invalid_mount), str(unavailable_mount)}
        assert str(unmarked_mount) not in attention_paths
        deadline = time.monotonic() + 5
        counts: object = None
        while time.monotonic() < deadline:
            counts = ListenerStore(paths.database_file).summary()["counts"]
            if counts == {"completed": 1}:
                break
            time.sleep(0.02)
        assert counts == {"completed": 1}
    finally:
        runtime.request_stop()
        thread.join(timeout=5)
    assert not thread.is_alive()


def test_listener_status_reports_health_and_dispatch_attention(tmp_path: Path) -> None:
    config, paths, _mount, _counter = _fixture(tmp_path)
    config.write(paths.config_file)
    heartbeat = {
        "schema": "gogurt-listener-heartbeat/v1",
        "version": importlib.metadata.version("gogurt"),
        "pid": os.getpid(),
        "started_at": "2026-08-14T00:00:00Z",
        "heartbeat_at": "2026-08-14T00:00:10Z",
        "queue_depth": 0,
        "active_dispatch": None,
        "dispatches": {"counts": {}, "attention": []},
        "configuration": {"status": "valid", "diagnostic": None},
        "runtime": {"status": "running", "diagnostic": None},
        "mount_attention": [],
    }
    paths.heartbeat_file.write_text(json.dumps(heartbeat), encoding="utf-8")
    adapter = FakeAdapter()
    adapter.installed = True
    adapter.running = True

    status = listener_status(
        paths=paths,
        adapter=adapter,
        now=datetime_timestamp("2026-08-14T00:00:11Z"),
    )
    assert status["health"] == "healthy"
    assert status["installed"] is True
    assert status["running"] is True


@pytest.mark.parametrize(
    ("field", "value", "diagnostic"),
    [
        ("pid", True, "PID is invalid"),
        ("pid", 1 << 40, "PID is invalid"),
        ("queue_depth", 1 << 80, "integer is outside"),
        ("started_at", "999999-01-01T00:00:00Z", "start time is invalid"),
        ("heartbeat_at", "not-a-time", "time is invalid"),
    ],
)
def test_listener_status_bounds_malformed_heartbeat_representations(
    tmp_path: Path,
    field: str,
    value: object,
    diagnostic: str,
) -> None:
    config, paths, _mount, _counter = _fixture(tmp_path)
    config.write(paths.config_file)
    heartbeat: dict[str, object] = {
        "schema": "gogurt-listener-heartbeat/v1",
        "version": importlib.metadata.version("gogurt"),
        "pid": os.getpid(),
        "started_at": "2026-08-14T00:00:00Z",
        "heartbeat_at": "2026-08-14T00:00:10Z",
        "queue_depth": 0,
        "active_dispatch": None,
        "dispatches": {"counts": {}, "attention": []},
        "configuration": {"status": "valid", "diagnostic": None},
        "runtime": {"status": "running", "diagnostic": None},
        "mount_attention": [],
    }
    heartbeat[field] = value
    paths.heartbeat_file.write_text(json.dumps(heartbeat), encoding="utf-8")
    adapter = FakeAdapter()
    adapter.installed = True
    adapter.running = True

    status = listener_status(
        paths=paths,
        adapter=adapter,
        now=datetime_timestamp("2026-08-14T00:00:11Z"),
    )

    assert status["health"] == "failed"
    assert diagnostic in str(status["diagnostic"])


def test_listener_status_rejects_a_future_heartbeat_as_false_liveness(tmp_path: Path) -> None:
    config, paths, _mount, _counter = _fixture(tmp_path)
    config.write(paths.config_file)
    heartbeat = {
        "schema": "gogurt-listener-heartbeat/v1",
        "version": importlib.metadata.version("gogurt"),
        "pid": os.getpid(),
        "started_at": "2026-08-14T00:00:00Z",
        "heartbeat_at": "2026-08-14T01:00:00Z",
        "queue_depth": 0,
        "active_dispatch": None,
        "dispatches": {"counts": {}, "attention": []},
        "configuration": {"status": "valid", "diagnostic": None},
        "runtime": {"status": "running", "diagnostic": None},
        "mount_attention": [],
    }
    paths.heartbeat_file.write_text(json.dumps(heartbeat), encoding="utf-8")
    adapter = FakeAdapter()
    adapter.installed = True
    adapter.running = True

    status = listener_status(
        paths=paths,
        adapter=adapter,
        now=datetime_timestamp("2026-08-14T00:00:11Z"),
    )

    assert status["health"] == "failed"
    assert "time is in the future" in str(status["diagnostic"])


def test_listener_status_bounds_an_overflowing_config_integer(tmp_path: Path) -> None:
    config, paths, _mount, _counter = _fixture(tmp_path)
    content = (
        config.content()
        .decode("utf-8")
        .replace(
            '"interval_seconds": 0.1',
            f'"interval_seconds": {1 << 80}',
        )
    )
    paths.config_file.parent.mkdir(parents=True)
    paths.config_file.write_text(content, encoding="utf-8")
    adapter = FakeAdapter()
    adapter.installed = True
    adapter.running = True

    status = listener_status(paths=paths, adapter=adapter)

    assert status["health"] == "failed"
    assert "integer is outside" in str(status["diagnostic"])


def test_listener_status_reports_corrupt_state_without_crashing(tmp_path: Path) -> None:
    config, paths, _mount, _counter = _fixture(tmp_path)
    paths.state_dir.mkdir(parents=True)
    config.write(paths.config_file)
    paths.database_file.write_bytes(b"not a sqlite database")
    paths.heartbeat_file.write_text(
        json.dumps(
            {
                "schema": "gogurt-listener-heartbeat/v1",
                "version": importlib.metadata.version("gogurt"),
                "pid": os.getpid(),
                "started_at": "2026-08-14T00:00:00Z",
                "heartbeat_at": "2026-08-14T00:00:10Z",
                "queue_depth": 0,
                "active_dispatch": None,
                "dispatches": {"counts": {}, "attention": []},
                "configuration": {"status": "valid", "diagnostic": None},
                "runtime": {"status": "running", "diagnostic": None},
                "mount_attention": [],
            }
        ),
        encoding="utf-8",
    )
    adapter = FakeAdapter()
    adapter.installed = True
    adapter.running = True

    status = listener_status(
        paths=paths,
        adapter=adapter,
        now=datetime_timestamp("2026-08-14T00:00:11Z"),
    )

    assert status["health"] == "failed"
    assert status["dispatches"] == {"counts": {}, "attention": []}
    assert "listener state" in str(status["diagnostic"])


def test_listener_status_bounds_a_locked_state_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, paths, _mount, _counter = _fixture(tmp_path)
    config.write(paths.config_file)
    paths.database_file.touch()
    adapter = FakeAdapter()
    adapter.installed = True
    adapter.running = True
    observed_timeouts: list[float] = []

    def locked_connect(
        _store: ListenerStore,
        *,
        timeout_seconds: float = 30,
    ) -> sqlite3.Connection:
        observed_timeouts.append(timeout_seconds)
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(ListenerStore, "_connect", locked_connect)

    status = listener_status(paths=paths, adapter=adapter)

    assert status["health"] == "failed"
    assert "database is locked" in str(status["diagnostic"])
    assert observed_timeouts == [listener_module.LISTENER_STATUS_DB_TIMEOUT_SECONDS]


def test_listener_status_reports_unreadable_heartbeat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, paths, _mount, _counter = _fixture(tmp_path)
    config.write(paths.config_file)
    paths.heartbeat_file.touch()
    adapter = FakeAdapter()
    adapter.installed = True
    adapter.running = True
    real_read_text = Path.read_text

    def unreadable(path: Path, *args: object, **kwargs: object) -> str:
        if path == paths.heartbeat_file:
            raise PermissionError("heartbeat is unreadable")
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)

    status = listener_status(paths=paths, adapter=adapter)

    assert status["health"] == "failed"
    assert "heartbeat is unreadable" in str(status["diagnostic"])


def test_restarted_native_process_cannot_reuse_a_predecessor_heartbeat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, paths, _mount, _counter = _fixture(tmp_path)
    paths.state_dir.mkdir(parents=True)
    config.write(paths.config_file)
    paths.heartbeat_file.write_text(
        json.dumps(
            {
                "schema": "gogurt-listener-heartbeat/v1",
                "version": importlib.metadata.version("gogurt"),
                "pid": 4321,
                "started_at": "2026-08-14T00:00:00Z",
                "heartbeat_at": "2026-08-14T00:00:10Z",
                "queue_depth": 0,
                "active_dispatch": None,
                "dispatches": {"counts": {"uncertain": 1}, "attention": []},
                "configuration": {"status": "valid", "diagnostic": None},
                "runtime": {"status": "running", "diagnostic": None},
                "mount_attention": [],
            }
        ),
        encoding="utf-8",
    )
    adapter = FakeAdapter()
    adapter.installed = True
    adapter.running = True
    monkeypatch.setattr(listener_module, "_process_is_running", lambda _pid: False)

    status = listener_status(
        paths=paths,
        adapter=adapter,
        now=datetime_timestamp("2026-08-14T00:00:11Z"),
    )

    assert status["health"] == "starting"
    assert status["heartbeat"] is None


def test_uninstall_removes_corrupt_state_without_reading_it(tmp_path: Path) -> None:
    _config, paths, _mount, _counter = _fixture(tmp_path)
    paths.state_dir.mkdir(parents=True)
    paths.database_file.write_bytes(b"not a sqlite database")
    paths.heartbeat_file.write_bytes(b"not json")
    adapter = FakeAdapter()
    adapter.installed = True
    adapter.running = True

    status = uninstall_listener(paths=paths, adapter=adapter)

    assert status["health"] == "absent"
    assert status["installed"] is False
    assert adapter.unregister_calls == 1
    assert not paths.state_dir.exists()


@pytest.mark.parametrize("root_kind", ["file", "symlink"])
def test_uninstall_removes_non_directory_state_root_without_following_it(
    tmp_path: Path,
    root_kind: str,
) -> None:
    _config, paths, _mount, _counter = _fixture(tmp_path)
    preserved = tmp_path / "preserved"
    preserved.mkdir()
    (preserved / "content").write_text("keep", encoding="utf-8")
    if root_kind == "file":
        paths.state_dir.write_text("damaged", encoding="utf-8")
    else:
        paths.state_dir.symlink_to(preserved, target_is_directory=True)
    adapter = FakeAdapter()
    adapter.installed = True
    adapter.running = True

    status = uninstall_listener(paths=paths, adapter=adapter)

    assert status["health"] == "absent"
    assert not paths.state_dir.exists()
    assert (preserved / "content").read_text(encoding="utf-8") == "keep"


def test_listener_status_tolerates_native_startup_before_schema_commit(tmp_path: Path) -> None:
    config, paths, _mount, _counter = _fixture(tmp_path)
    paths.state_dir.mkdir(parents=True)
    config.write(paths.config_file)
    paths.database_file.touch()
    adapter = FakeAdapter()
    adapter.installed = True
    adapter.running = True

    status = listener_status(paths=paths, adapter=adapter)

    assert status["health"] == "starting"
    assert status["dispatches"] == {"counts": {}, "attention": []}


def test_listener_stop_reports_settled_native_state(tmp_path: Path) -> None:
    _config, paths, _mount, _counter = _fixture(tmp_path)
    adapter = FakeAdapter()
    adapter.installed = True
    adapter.running = True

    result: list[dict[str, object]] = []

    def stop() -> None:
        result.append(stop_listener(paths=paths, adapter=adapter))

    with ListenerLock(paths.lock_file):
        thread = threading.Thread(target=stop)
        thread.start()
        time.sleep(0.1)
        assert thread.is_alive()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert result[0]["health"] == "stopped"
    assert result[0]["running"] is False


def test_listener_stop_waits_for_durable_action_custody_settlement(tmp_path: Path) -> None:
    config, paths, mount, _counter = _fixture(tmp_path)
    store = ListenerStore(paths.database_file)
    store.create()
    [dispatch_id] = store.observe(
        [mount],
        lambda point: core_plan_gogurt_action(config.routes_file, point),
        now=1,
    )
    assert store.start_dispatch(dispatch_id, now=2) is not None
    adapter = FakeAdapter()
    adapter.installed = True
    adapter.running = True
    result: list[dict[str, object]] = []
    thread = threading.Thread(
        target=lambda: result.append(stop_listener(paths=paths, adapter=adapter))
    )

    thread.start()
    time.sleep(0.1)
    assert thread.is_alive()
    store.mark_running_uncertain(dispatch_id, error="settled by listener", now=3)
    thread.join(timeout=5)

    assert not thread.is_alive()
    dispatches = result[0]["dispatches"]
    assert isinstance(dispatches, dict)
    assert dispatches["counts"] == {"uncertain": 1}


def datetime_timestamp(value: str) -> float:
    return time.mktime(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ"))
