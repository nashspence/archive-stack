from __future__ import annotations

import importlib.metadata
import json
import os
import stat
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path

import gogurt.listener as listener_module
import pytest
from config_validation import ConfigError
from gogurt.core import load_gogurt_actions
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
    ]
    private_files.extend(paths.state_dir.glob("listener.sqlite3-*"))
    assert private_files
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in private_files)


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
                raise ListenerError("replacement unhealthy")
        return {"health": sorted(expected)[0]}

    monkeypatch.setattr(
        "gogurt.listener._wait_for_health",
        replacement_then_rollback_health,
    )

    with pytest.raises(ListenerError, match="replacement unhealthy"):
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
            heartbeat = json.loads(paths.heartbeat_file.read_text(encoding="utf-8"))
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
        "pid": 123,
        "started_at": "2026-08-14T00:00:00Z",
        "heartbeat_at": "2026-08-14T00:00:10Z",
        "queue_depth": 0,
        "active_dispatch": None,
        "dispatches": {"counts": {}, "attention": []},
        "configuration": {"status": "valid", "diagnostic": None},
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


def datetime_timestamp(value: str) -> float:
    return time.mktime(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ"))
