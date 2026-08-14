from __future__ import annotations

import importlib.metadata
import json
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path

import pytest
from gogurt.core import load_gogurt_actions
from gogurt.listener import (
    LISTENER_CONFIG_SCHEMA,
    ListenerConfig,
    ListenerError,
    ListenerLock,
    ListenerRuntime,
    ListenerStore,
    install_listener,
    listener_status,
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


def test_listener_runs_once_across_restart_and_again_after_remount(tmp_path: Path) -> None:
    config, paths, mount, counter = _fixture(tmp_path)
    first = ListenerRuntime(config, paths, discover=lambda: [mount])
    thread = threading.Thread(target=first.run)
    thread.start()
    _wait_for_runs(counter, 1)
    first.request_stop()
    thread.join(timeout=5)
    assert not thread.is_alive()

    second = ListenerRuntime(config, paths, discover=lambda: [mount])
    thread = threading.Thread(target=second.run)
    thread.start()
    time.sleep(0.3)
    assert counter.read_text(encoding="utf-8").splitlines() == ["run"]

    second.discover = lambda: []
    time.sleep(0.2)
    second.discover = lambda: [mount]
    _wait_for_runs(counter, 2)
    second.request_stop()
    thread.join(timeout=5)
    assert counter.read_text(encoding="utf-8").splitlines() == ["run", "run"]


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
    _wait_for_runs(counter, 1)
    runtime.request_stop()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert ListenerStore(paths.database_file).summary()["counts"] == {"uncertain": 1}

    restarted = ListenerRuntime(config, paths, discover=lambda: [mount])
    thread = threading.Thread(target=restarted.run)
    thread.start()
    time.sleep(0.4)
    restarted.request_stop()
    thread.join(timeout=5)
    assert counter.read_text(encoding="utf-8").splitlines() == ["run"]


def test_listener_lock_prevents_concurrent_runtime_ownership(tmp_path: Path) -> None:
    path = tmp_path / "listener.lock"
    with ListenerLock(path):
        with pytest.raises(ListenerError, match="already owns"):
            with ListenerLock(path):
                pass


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
        self.running = False

    def restart(self, paths: ListenerPaths) -> None:
        del paths
        self.running = True

    def unregister(self, paths: ListenerPaths) -> None:
        del paths
        self.installed = False
        self.running = False


def test_install_binds_absolute_executable_and_rolls_back_failed_startup(tmp_path: Path) -> None:
    config, paths, _mount, _counter = _fixture(tmp_path)
    executable = tmp_path / "installed" / "gogurt"
    executable.parent.mkdir()
    executable.write_text("fixture", encoding="utf-8")
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
    assert not paths.config_file.exists()


def test_failed_replacement_restores_the_previous_listener(tmp_path: Path) -> None:
    config, paths, _mount, _counter = _fixture(tmp_path)
    executable = tmp_path / "installed" / "gogurt"
    executable.parent.mkdir()
    executable.write_text("fixture", encoding="utf-8")
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


def test_listener_status_reports_health_and_dispatch_attention(tmp_path: Path) -> None:
    config, paths, _mount, _counter = _fixture(tmp_path)
    config.write(paths.config_file)
    heartbeat = {
        "schema": "gogurt-listener-heartbeat/v1",
        "version": importlib.metadata.version("gogurt"),
        "pid": 123,
        "started_at": "2026-08-14T00:00:00Z",
        "heartbeat_at": "2026-08-14T00:00:10Z",
        "queue_depth": 0,
        "active_dispatch": None,
        "dispatches": {"counts": {}, "attention": []},
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


def datetime_timestamp(value: str) -> float:
    return time.mktime(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ"))
