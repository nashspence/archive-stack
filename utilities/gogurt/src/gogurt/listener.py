from __future__ import annotations

import importlib.metadata
import json
import logging
import logging.handlers
import os
import queue
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

from config_validation import ConfigError

from gogurt.core import (
    DEFAULT_GOGURT_MARKER_NAME,
    load_gogurt_actions,
    plan_gogurt_action,
    revalidate_gogurt_action,
)
from gogurt.listener_platform import (
    ListenerAdapter,
    ListenerPaths,
    ListenerPlatformError,
    default_listener_paths,
    listener_adapter,
    resolve_listener_executable,
)
from gogurt.mounts import discover_mount_points

LISTENER_CONFIG_SCHEMA = "gogurt-listener-config/v1"
LISTENER_HEARTBEAT_SCHEMA = "gogurt-listener-heartbeat/v1"
LISTENER_STATUS_SCHEMA = "gogurt-listener-status/v1"
LISTENER_STATE_SCHEMA = 1
LISTENER_MAX_ATTEMPTS = 3
LISTENER_RETRY_SECONDS = (5.0, 30.0)
LISTENER_LOG_BYTES = 1_048_576
LISTENER_LOG_BACKUPS = 3
LISTENER_OPERATIONS = ("install", "status", "start", "stop", "restart", "uninstall")


def listener_release_contract() -> dict[str, object]:
    """Return the generated-release contract implemented by this listener."""

    return {
        "schema": "gogurt-listener-contract/v1",
        "root": "gogurt",
        "scope": "current-user",
        "resume": "next-login",
        "autorun": "explicit-required",
        "operations": list(LISTENER_OPERATIONS),
        "platforms": {
            "linux-x64": "systemd-user",
            "macos-arm64": "launchd-user",
            "windows-x64": "task-scheduler-user",
        },
        "status_schema": LISTENER_STATUS_SCHEMA,
        "dispatch": {
            "completed": "not-replayed-across-ordinary-restart",
            "running_after_crash": "uncertain-no-automatic-replay",
            "known_failure": "bounded-retry",
            "downstream": "idempotency-required-where-replay-is-possible",
        },
        "logs": {
            "bytes_per_file": LISTENER_LOG_BYTES,
            "backups": LISTENER_LOG_BACKUPS,
        },
    }


class ListenerError(RuntimeError):
    """The Gogurt listener contract could not be satisfied."""


def _now_text(now: float | None = None) -> str:
    value = time.time() if now is None else now
    return datetime.fromtimestamp(value, tz=UTC).isoformat().replace("+00:00", "Z")


def _package_version() -> str:
    return importlib.metadata.version("gogurt")


def _atomic_write(path: Path, content: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True, slots=True)
class ListenerConfig:
    executable: Path
    routes_file: Path
    actions_dir: Path | None
    marker_name: str
    interval_seconds: float
    state_dir: Path
    autorun: bool = True

    def payload(self) -> dict[str, object]:
        return {
            "schema": LISTENER_CONFIG_SCHEMA,
            "version": _package_version(),
            "executable": str(self.executable),
            "routes_file": str(self.routes_file),
            "actions_dir": str(self.actions_dir) if self.actions_dir is not None else None,
            "marker_name": self.marker_name,
            "interval_seconds": self.interval_seconds,
            "state_dir": str(self.state_dir),
            "autorun": self.autorun,
        }

    def write(self, path: Path) -> None:
        content = (json.dumps(self.payload(), indent=2, sort_keys=True) + "\n").encode("utf-8")
        _atomic_write(path, content)

    @classmethod
    def read(cls, path: Path) -> ListenerConfig:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, UnicodeError) as exc:
            raise ListenerError(f"invalid Gogurt listener config: {path}") from exc
        expected = {
            "schema",
            "version",
            "executable",
            "routes_file",
            "actions_dir",
            "marker_name",
            "interval_seconds",
            "state_dir",
            "autorun",
        }
        if not isinstance(raw, dict) or set(raw) != expected:
            raise ListenerError(f"invalid Gogurt listener config fields: {path}")
        if raw["schema"] != LISTENER_CONFIG_SCHEMA or raw["version"] != _package_version():
            raise ListenerError(
                f"Gogurt listener config version differs from the executable: {path}"
            )
        if raw["autorun"] is not True:
            raise ListenerError("installed Gogurt listener config must explicitly enable autorun")
        interval = float(raw["interval_seconds"])
        if interval < 0.1:
            raise ListenerError("Gogurt listener interval must be at least 0.1 seconds")
        executable = Path(str(raw["executable"]))
        routes_file = Path(str(raw["routes_file"]))
        state_dir = Path(str(raw["state_dir"]))
        raw_actions = raw["actions_dir"]
        actions_dir = Path(str(raw_actions)) if raw_actions is not None else None
        if not all(path.is_absolute() for path in (executable, routes_file, state_dir)):
            raise ListenerError("Gogurt listener paths must be absolute")
        if actions_dir is not None and not actions_dir.is_absolute():
            raise ListenerError("Gogurt listener actions directory must be absolute")
        return cls(
            executable=executable,
            routes_file=routes_file,
            actions_dir=actions_dir,
            marker_name=str(raw["marker_name"]),
            interval_seconds=interval,
            state_dir=state_dir,
        )


def _service_command(config: ListenerConfig, config_file: Path) -> tuple[str, ...]:
    return (
        str(config.executable),
        "listener",
        "_run",
        "--runtime-config",
        str(config_file),
    )


class ListenerStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def create(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS listener_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS observed_mounts (
                    mount_point TEXT PRIMARY KEY,
                    present INTEGER NOT NULL CHECK (present IN (0, 1)),
                    generation INTEGER NOT NULL,
                    marker_identity TEXT
                );
                CREATE TABLE IF NOT EXISTS dispatches (
                    dispatch_id TEXT PRIMARY KEY,
                    mount_point TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    marker_identity TEXT NOT NULL,
                    route TEXT NOT NULL,
                    plan_json TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (
                        state IN ('queued', 'running', 'retry', 'uncertain', 'completed', 'failed')
                    ),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    observed_at REAL NOT NULL,
                    started_at REAL,
                    completed_at REAL,
                    next_retry_at REAL,
                    exit_code INTEGER,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS dispatches_state_idx
                    ON dispatches(state, next_retry_at, observed_at);
                """
            )
            row = connection.execute(
                "SELECT value FROM listener_meta WHERE key = 'schema'"
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO listener_meta(key, value) VALUES ('schema', ?)",
                    (str(LISTENER_STATE_SCHEMA),),
                )
            elif row["value"] != str(LISTENER_STATE_SCHEMA):
                raise ListenerError("Gogurt listener state schema is unsupported")
            connection.execute(
                """
                UPDATE dispatches
                SET state = 'uncertain',
                    error = 'listener exited while the action process had custody'
                WHERE state = 'running'
                """
            )
            connection.commit()

    def observe(
        self,
        mount_points: Sequence[Path],
        planner: Callable[[Path], Mapping[str, object]],
        *,
        now: float,
    ) -> list[str]:
        current = {str(path.resolve()) for path in mount_points}
        queued: list[str] = []
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            known_rows = connection.execute("SELECT * FROM observed_mounts").fetchall()
            known = {str(row["mount_point"]): row for row in known_rows}
            for path_value, row in known.items():
                if path_value not in current and int(row["present"]) == 1:
                    connection.execute(
                        "UPDATE observed_mounts SET present = 0 WHERE mount_point = ?",
                        (path_value,),
                    )
            for path_value in sorted(current, key=str.casefold):
                row = known.get(path_value)
                if row is None:
                    generation = 1
                    connection.execute(
                        """
                        INSERT INTO observed_mounts(
                            mount_point, present, generation, marker_identity
                        )
                        VALUES (?, 1, ?, NULL)
                        """,
                        (path_value, generation),
                    )
                    previous_identity = None
                else:
                    generation = int(row["generation"])
                    if int(row["present"]) == 0:
                        generation += 1
                        connection.execute(
                            """
                            UPDATE observed_mounts
                            SET present = 1, generation = ?, marker_identity = NULL
                            WHERE mount_point = ?
                            """,
                            (generation, path_value),
                        )
                        previous_identity = None
                    else:
                        previous_identity = row["marker_identity"]
                plan = planner(Path(path_value))
                if plan.get("status") == "unmarked":
                    continue
                marker_identity = plan.get("marker_identity")
                route = plan.get("route")
                if not isinstance(marker_identity, str) or not isinstance(route, str):
                    raise ListenerError("Gogurt returned an invalid listener action plan")
                if previous_identity == marker_identity:
                    continue
                dispatch_id = sha256(
                    f"{path_value}\0{generation}\0{marker_identity}".encode()
                ).hexdigest()
                connection.execute(
                    """
                    INSERT OR IGNORE INTO dispatches(
                        dispatch_id, mount_point, generation, marker_identity, route,
                        plan_json, state, observed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?)
                    """,
                    (
                        dispatch_id,
                        path_value,
                        generation,
                        marker_identity,
                        route,
                        json.dumps(dict(plan), sort_keys=True),
                        now,
                    ),
                )
                connection.execute(
                    """
                    UPDATE observed_mounts SET marker_identity = ? WHERE mount_point = ?
                    """,
                    (marker_identity, path_value),
                )
                queued.append(dispatch_id)
            connection.commit()
        return queued

    def runnable(self, *, now: float) -> list[str]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT dispatch_id FROM dispatches
                WHERE state = 'queued'
                   OR (state = 'retry' AND next_retry_at <= ?)
                ORDER BY observed_at, dispatch_id
                """,
                (now,),
            ).fetchall()
        return [str(row["dispatch_id"]) for row in rows]

    def start_dispatch(self, dispatch_id: str, *, now: float) -> dict[str, object] | None:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT plan_json FROM dispatches
                WHERE dispatch_id = ? AND state IN ('queued', 'retry')
                """,
                (dispatch_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                return None
            connection.execute(
                """
                UPDATE dispatches
                SET state = 'running', attempts = attempts + 1, started_at = ?,
                    next_retry_at = NULL, exit_code = NULL, error = NULL
                WHERE dispatch_id = ?
                """,
                (now, dispatch_id),
            )
            connection.commit()
        plan = json.loads(str(row["plan_json"]))
        if not isinstance(plan, dict):
            raise ListenerError("stored Gogurt listener plan is invalid")
        return plan

    def finish_dispatch(
        self,
        dispatch_id: str,
        *,
        return_code: int | None,
        error: str | None,
        uncertain: bool = False,
        now: float,
    ) -> str:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT attempts FROM dispatches WHERE dispatch_id = ? AND state = 'running'",
                (dispatch_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise ListenerError(f"active Gogurt dispatch is absent: {dispatch_id}")
            attempts = int(row["attempts"])
            if uncertain:
                state = "uncertain"
                next_retry = None
                error = error or "listener stopped while the action process had custody"
            elif return_code == 0 and error is None:
                state = "completed"
                next_retry = None
            elif attempts < LISTENER_MAX_ATTEMPTS:
                state = "retry"
                retry_index = min(attempts - 1, len(LISTENER_RETRY_SECONDS) - 1)
                next_retry = now + LISTENER_RETRY_SECONDS[retry_index]
            else:
                state = "failed"
                next_retry = None
            connection.execute(
                """
                UPDATE dispatches
                SET state = ?, completed_at = ?, next_retry_at = ?, exit_code = ?, error = ?
                WHERE dispatch_id = ?
                """,
                (state, now, next_retry, return_code, error, dispatch_id),
            )
            connection.commit()
        return state

    def summary(self) -> dict[str, object]:
        if not self.path.is_file():
            return {"counts": {}, "attention": []}
        with closing(self._connect()) as connection:
            counts = {
                str(row["state"]): int(row["count"])
                for row in connection.execute(
                    "SELECT state, COUNT(*) AS count FROM dispatches GROUP BY state"
                )
            }
            rows = connection.execute(
                """
                SELECT dispatch_id, mount_point, route, state, attempts, exit_code, error
                FROM dispatches
                WHERE state IN ('retry', 'uncertain', 'failed')
                ORDER BY observed_at DESC LIMIT 20
                """
            ).fetchall()
        return {
            "counts": counts,
            "attention": [
                {
                    "dispatch_id": str(row["dispatch_id"]),
                    "mount_point": str(row["mount_point"]),
                    "route": str(row["route"]),
                    "state": str(row["state"]),
                    "attempts": int(row["attempts"]),
                    "exit_code": row["exit_code"],
                    "error": row["error"],
                }
                for row in rows
            ],
        }


class ListenerLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._stream: Any = None

    def __enter__(self) -> ListenerLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self.path.open("a+b")
        self._stream.seek(0)
        self._stream.write(b"0")
        self._stream.flush()
        try:
            if os.name == "nt":
                import msvcrt

                self._stream.seek(0)
                windows_locking = cast(Any, msvcrt)
                windows_locking.locking(self._stream.fileno(), windows_locking.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._stream.close()
            self._stream = None
            raise ListenerError("another Gogurt listener already owns this state") from exc
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        if self._stream is None:
            return
        if os.name == "nt":
            import msvcrt

            self._stream.seek(0)
            windows_locking = cast(Any, msvcrt)
            windows_locking.locking(self._stream.fileno(), windows_locking.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(self._stream.fileno(), fcntl.LOCK_UN)
        self._stream.close()
        self._stream = None


class ListenerRuntime:
    def __init__(
        self,
        config: ListenerConfig,
        paths: ListenerPaths,
        *,
        discover: Callable[[], Sequence[Path]] = discover_mount_points,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        logger: logging.Logger | None = None,
    ) -> None:
        self.config = config
        self.paths = paths
        self.discover = discover
        self.clock = clock
        self.sleep = sleep
        self.logger = logger or logging.getLogger("gogurt.listener")
        self.store = ListenerStore(paths.database_file)
        self.stop_event = threading.Event()
        self.dispatch_queue: queue.Queue[str] = queue.Queue()
        self._queued: set[str] = set()
        self._queue_lock = threading.Lock()
        self._active_lock = threading.Lock()
        self._active_dispatch: str | None = None
        self._active_process: subprocess.Popen[bytes] | None = None
        self.started_at = self.clock()

    def request_stop(self) -> None:
        self.stop_event.set()
        with self._active_lock:
            process = self._active_process
        if process is not None and process.poll() is None:
            process.terminate()

    def _planner(self, mount_point: Path) -> Mapping[str, object]:
        try:
            return plan_gogurt_action(
                self.config.routes_file,
                mount_point,
                actions_dir=self.config.actions_dir,
                marker_name=self.config.marker_name,
            )
        except (
            ConfigError,
            FileNotFoundError,
            NotADirectoryError,
            PermissionError,
            UnicodeError,
        ) as exc:
            self.logger.warning("mount=%s skipped: %s", mount_point, exc)
            return {"status": "unmarked"}

    def _enqueue(self, dispatch_ids: Sequence[str]) -> None:
        with self._queue_lock:
            for dispatch_id in dispatch_ids:
                if dispatch_id in self._queued:
                    continue
                self._queued.add(dispatch_id)
                self.dispatch_queue.put(dispatch_id)

    def _heartbeat(self) -> None:
        summary = self.store.summary()
        with self._active_lock:
            active = self._active_dispatch
        payload = {
            "schema": LISTENER_HEARTBEAT_SCHEMA,
            "version": _package_version(),
            "pid": os.getpid(),
            "started_at": _now_text(self.started_at),
            "heartbeat_at": _now_text(self.clock()),
            "queue_depth": self.dispatch_queue.qsize(),
            "active_dispatch": active,
            "dispatches": summary,
        }
        _atomic_write(
            self.paths.heartbeat_file,
            (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )

    def _worker(self) -> None:
        while not self.stop_event.is_set():
            try:
                dispatch_id = self.dispatch_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            with self._queue_lock:
                self._queued.discard(dispatch_id)
            plan = self.store.start_dispatch(dispatch_id, now=self.clock())
            if plan is None:
                self.dispatch_queue.task_done()
                continue
            with self._active_lock:
                self._active_dispatch = dispatch_id
            self._heartbeat()
            return_code: int | None = None
            error: str | None = None
            try:
                command = revalidate_gogurt_action(plan)
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                with self._active_lock:
                    self._active_process = process
                return_code = process.wait()
            except (ConfigError, OSError, ValueError) as exc:
                error = str(exc)
            finally:
                with self._active_lock:
                    self._active_process = None
                    self._active_dispatch = None
            state = self.store.finish_dispatch(
                dispatch_id,
                return_code=return_code,
                error=error,
                uncertain=self.stop_event.is_set() and (return_code != 0 or error is not None),
                now=self.clock(),
            )
            self.logger.info(
                "dispatch=%s state=%s exit=%s error=%s",
                dispatch_id,
                state,
                return_code,
                error,
            )
            self.dispatch_queue.task_done()
            self._heartbeat()

    def run_once(self) -> None:
        now = self.clock()
        try:
            queued = self.store.observe(self.discover(), self._planner, now=now)
        except (ConfigError, FileNotFoundError, NotADirectoryError, PermissionError) as exc:
            self.logger.warning("mount observation skipped: %s", exc)
            queued = []
        self._enqueue(queued)
        self._enqueue(self.store.runnable(now=now))
        self._heartbeat()

    def run(self) -> None:
        self.store.create()
        worker = threading.Thread(target=self._worker, name="gogurt-dispatch", daemon=True)
        worker.start()
        try:
            while not self.stop_event.is_set():
                self.run_once()
                self.stop_event.wait(self.config.interval_seconds)
        finally:
            self.request_stop()
            worker.join(timeout=10)
            self._heartbeat()


def _logger(paths: ListenerPaths) -> logging.Logger:
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("gogurt.listener")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    handler = logging.handlers.RotatingFileHandler(
        paths.log_file,
        maxBytes=LISTENER_LOG_BYTES,
        backupCount=LISTENER_LOG_BACKUPS,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger


def run_listener(config_file: Path) -> None:
    config = ListenerConfig.read(config_file)
    paths = ListenerPaths(
        state_dir=config.state_dir,
        config_file=config_file,
        database_file=config.state_dir / "listener.sqlite3",
        heartbeat_file=config.state_dir / "heartbeat.json",
        lock_file=config.state_dir / "listener.lock",
        log_file=config.state_dir / "listener.log",
        registration_file=None,
    )
    runtime = ListenerRuntime(config, paths, logger=_logger(paths))

    def stop(_signum: int, _frame: object) -> None:
        runtime.request_stop()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with ListenerLock(paths.lock_file):
        runtime.logger.info("listener started pid=%s version=%s", os.getpid(), _package_version())
        runtime.run()
        runtime.logger.info("listener stopped pid=%s", os.getpid())


def _read_heartbeat(path: Path) -> dict[str, object] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, UnicodeError):
        return None
    if not isinstance(value, dict):
        return None
    if value.get("schema") != LISTENER_HEARTBEAT_SCHEMA:
        return None
    if value.get("version") != _package_version():
        return None
    return value


def listener_status(
    *,
    paths: ListenerPaths | None = None,
    adapter: ListenerAdapter | None = None,
    now: float | None = None,
) -> dict[str, object]:
    resolved_paths = paths or default_listener_paths()
    native_adapter = adapter or listener_adapter()
    native = native_adapter.status(resolved_paths)
    heartbeat = _read_heartbeat(resolved_paths.heartbeat_file)
    config: ListenerConfig | None = None
    config_error: str | None = None
    if resolved_paths.config_file.is_file():
        try:
            config = ListenerConfig.read(resolved_paths.config_file)
        except ListenerError as exc:
            config_error = str(exc)
    current = time.time() if now is None else now
    heartbeat_age: float | None = None
    if heartbeat is not None:
        try:
            heartbeat_time = datetime.fromisoformat(
                str(heartbeat["heartbeat_at"]).replace("Z", "+00:00")
            ).timestamp()
            heartbeat_age = max(0.0, current - heartbeat_time)
        except (KeyError, TypeError, ValueError):
            heartbeat = None
    if not native.installed:
        health = "absent"
    elif not native.running:
        health = "stopped"
    elif config_error is not None:
        health = "failed"
    elif heartbeat is None:
        health = "starting"
    else:
        interval = config.interval_seconds if config is not None else 2.0
        healthy = heartbeat_age is not None and heartbeat_age <= max(10, interval * 3)
        health = "healthy" if healthy else "stale"
    state = ListenerStore(resolved_paths.database_file).summary()
    return {
        "schema": LISTENER_STATUS_SCHEMA,
        "version": _package_version(),
        "platform": sys.platform,
        "installed": native.installed,
        "enabled": native.enabled,
        "running": native.running,
        "health": health,
        "config_file": str(resolved_paths.config_file),
        "state_dir": str(resolved_paths.state_dir),
        "executable": str(config.executable) if config is not None else None,
        "heartbeat_age_seconds": heartbeat_age,
        "heartbeat": heartbeat,
        "dispatches": state,
        "diagnostic": config_error,
    }


def _wait_for_health(
    expected: frozenset[str],
    *,
    paths: ListenerPaths,
    adapter: ListenerAdapter,
    previous_pid: int | None = None,
    timeout_seconds: float = 20,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    status = listener_status(paths=paths, adapter=adapter)
    while time.monotonic() < deadline:
        heartbeat = status.get("heartbeat")
        current_pid = heartbeat.get("pid") if isinstance(heartbeat, dict) else None
        if status["health"] in expected and (previous_pid is None or current_pid != previous_pid):
            return status
        time.sleep(0.2)
        status = listener_status(paths=paths, adapter=adapter)
    raise ListenerError(f"Gogurt listener did not become healthy: {status['health']}")


def _heartbeat_pid(paths: ListenerPaths) -> int | None:
    heartbeat = _read_heartbeat(paths.heartbeat_file)
    if heartbeat is None:
        return None
    value = heartbeat.get("pid")
    return int(value) if isinstance(value, int) else None


def install_listener(
    routes_file: Path,
    *,
    actions_dir: Path | None,
    marker_name: str = DEFAULT_GOGURT_MARKER_NAME,
    interval_seconds: float = 2.0,
    executable: Path | None = None,
    paths: ListenerPaths | None = None,
    adapter: ListenerAdapter | None = None,
    wait_for_health: bool = True,
) -> dict[str, object]:
    routes = routes_file.expanduser().resolve()
    load_gogurt_actions(routes)
    actions = actions_dir.expanduser().resolve() if actions_dir is not None else None
    if actions is not None and not actions.is_dir():
        raise NotADirectoryError(actions)
    if interval_seconds < 0.1:
        raise ListenerError("Gogurt listener interval must be at least 0.1 seconds")
    resolved_paths = paths or default_listener_paths()
    native_adapter = adapter or listener_adapter()
    resolved_executable = (executable or resolve_listener_executable()).expanduser().resolve()
    config = ListenerConfig(
        executable=resolved_executable,
        routes_file=routes,
        actions_dir=actions,
        marker_name=marker_name,
        interval_seconds=interval_seconds,
        state_dir=resolved_paths.state_dir,
    )
    previous: ListenerConfig | None = None
    if resolved_paths.config_file.is_file():
        previous = ListenerConfig.read(resolved_paths.config_file)
    previous_pid = _heartbeat_pid(resolved_paths)
    config.write(resolved_paths.config_file)
    try:
        native_adapter.register(
            resolved_paths,
            _service_command(config, resolved_paths.config_file),
        )
        if wait_for_health:
            return _wait_for_health(
                frozenset({"healthy"}),
                paths=resolved_paths,
                adapter=native_adapter,
                previous_pid=previous_pid,
            )
        return listener_status(paths=resolved_paths, adapter=native_adapter)
    except (ListenerError, ListenerPlatformError, OSError) as exc:
        rollback_errors: list[str] = []
        try:
            native_adapter.unregister(resolved_paths)
        except (ListenerPlatformError, OSError) as rollback_exc:
            rollback_errors.append(f"unregister: {rollback_exc}")
        if previous is None:
            try:
                resolved_paths.config_file.unlink(missing_ok=True)
            except OSError as rollback_exc:
                rollback_errors.append(f"remove config: {rollback_exc}")
        else:
            try:
                previous.write(resolved_paths.config_file)
                native_adapter.register(
                    resolved_paths,
                    _service_command(previous, resolved_paths.config_file),
                )
            except (ListenerPlatformError, OSError) as rollback_exc:
                rollback_errors.append(f"restore prior listener: {rollback_exc}")
        if rollback_errors:
            detail = "; ".join(rollback_errors)
            raise ListenerError(
                f"Gogurt listener installation failed ({exc}); rollback failed ({detail})"
            ) from exc
        raise


def start_listener(
    *, paths: ListenerPaths | None = None, adapter: ListenerAdapter | None = None
) -> dict[str, object]:
    resolved_paths = paths or default_listener_paths()
    native_adapter = adapter or listener_adapter()
    ListenerConfig.read(resolved_paths.config_file)
    previous_pid = _heartbeat_pid(resolved_paths)
    native_adapter.start(resolved_paths)
    return _wait_for_health(
        frozenset({"healthy"}),
        paths=resolved_paths,
        adapter=native_adapter,
        previous_pid=previous_pid,
    )


def stop_listener(
    *, paths: ListenerPaths | None = None, adapter: ListenerAdapter | None = None
) -> dict[str, object]:
    resolved_paths = paths or default_listener_paths()
    native_adapter = adapter or listener_adapter()
    native_adapter.stop(resolved_paths)
    return listener_status(paths=resolved_paths, adapter=native_adapter)


def restart_listener(
    *, paths: ListenerPaths | None = None, adapter: ListenerAdapter | None = None
) -> dict[str, object]:
    resolved_paths = paths or default_listener_paths()
    native_adapter = adapter or listener_adapter()
    ListenerConfig.read(resolved_paths.config_file)
    previous_pid = _heartbeat_pid(resolved_paths)
    native_adapter.restart(resolved_paths)
    return _wait_for_health(
        frozenset({"healthy"}),
        paths=resolved_paths,
        adapter=native_adapter,
        previous_pid=previous_pid,
    )


def uninstall_listener(
    *, paths: ListenerPaths | None = None, adapter: ListenerAdapter | None = None
) -> dict[str, object]:
    resolved_paths = paths or default_listener_paths()
    native_adapter = adapter or listener_adapter()
    native_adapter.unregister(resolved_paths)
    if resolved_paths.state_dir.is_dir():
        shutil.rmtree(resolved_paths.state_dir)
    return listener_status(paths=resolved_paths, adapter=native_adapter)
