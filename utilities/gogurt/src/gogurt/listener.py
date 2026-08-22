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
import stat
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
    plan_gogurt_action,
    revalidate_gogurt_action,
    validate_gogurt_action_executables,
    validate_gogurt_marker_name,
)
from gogurt.filesystem import (
    PRIVATE_FILE_MODE,
    atomic_write,
    ensure_private_directory,
    ensure_private_file,
    ensure_private_files,
    open_private_text_append,
    promote_staged,
    stage_bytes,
)
from gogurt.listener_platform import (
    ListenerAdapter,
    ListenerPaths,
    default_listener_paths,
    listener_adapter,
    resolve_listener_executable,
)
from gogurt.mounts import discover_mount_points, validate_gogurt_interval

LISTENER_CONFIG_SCHEMA = "gogurt-listener-config/v1"
LISTENER_HEARTBEAT_SCHEMA = "gogurt-listener-heartbeat/v1"
LISTENER_STATUS_SCHEMA = "gogurt-listener-status/v1"
LISTENER_STATE_SCHEMA = 1
LISTENER_MAX_ATTEMPTS = 3
LISTENER_RETRY_SECONDS = (5.0, 30.0)
LISTENER_LOG_BYTES = 1_048_576
LISTENER_LOG_BACKUPS = 3
LISTENER_MOUNT_ATTENTION_LIMIT = 20
LISTENER_DIAGNOSTIC_LIMIT = 512
LISTENER_STATUS_DB_TIMEOUT_SECONDS = 0.25
LISTENER_ACTION_TERMINATE_SECONDS = 2.0
LISTENER_ACTION_KILL_SECONDS = 2.0
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
        "health": {
            "healthy": "current-heartbeat-valid-global-configuration-and-live-worker",
            "failed": "global-configuration-or-runtime-prevents-dispatch",
            "mount_attention": "isolated-bounded-nonfatal-diagnostics",
        },
        "replacement": "validated-staged-transaction-with-healthy-rollback",
        "state": {
            "posix": "private-directory-and-files",
            "windows": "current-user-native-acl",
        },
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


def _listener_interval(value: object) -> float:
    try:
        return validate_gogurt_interval(value)
    except ValueError as exc:
        raise ListenerError(str(exc)) from exc


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise ListenerError(f"Gogurt listener config contains duplicate field: {key}")
        payload[key] = value
    return payload


def _reject_json_constant(value: str) -> object:
    raise ListenerError(f"Gogurt listener config contains nonfinite number: {value}")


def _now_text(now: float | None = None) -> str:
    value = time.time() if now is None else now
    return datetime.fromtimestamp(value, tz=UTC).isoformat().replace("+00:00", "Z")


def _package_version() -> str:
    return importlib.metadata.version("gogurt")


@dataclass(frozen=True, slots=True)
class ListenerConfig:
    executable: Path
    routes_file: Path
    actions_dir: Path | None
    marker_name: str
    interval_seconds: float
    state_dir: Path
    autorun: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "interval_seconds", _listener_interval(self.interval_seconds))

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

    def content(self) -> bytes:
        return (
            json.dumps(self.payload(), allow_nan=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")

    def write(self, path: Path) -> None:
        atomic_write(path, self.content(), mode=PRIVATE_FILE_MODE)

    @classmethod
    def read(cls, path: Path) -> ListenerConfig:
        try:
            raw = json.loads(
                path.read_text(encoding="utf-8"),
                object_pairs_hook=_strict_json_object,
                parse_constant=_reject_json_constant,
            )
        except ListenerError:
            raise
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
        interval = _listener_interval(raw["interval_seconds"])
        path_fields = ("executable", "routes_file", "state_dir")
        if any(not isinstance(raw[field], str) or not raw[field] for field in path_fields):
            raise ListenerError("Gogurt listener config paths must be nonempty strings")
        executable = Path(raw["executable"])
        routes_file = Path(raw["routes_file"])
        state_dir = Path(raw["state_dir"])
        raw_actions = raw["actions_dir"]
        if raw_actions is not None and (not isinstance(raw_actions, str) or not raw_actions):
            raise ListenerError(
                "Gogurt listener config actions directory must be a nonempty string or null"
            )
        actions_dir = Path(raw_actions) if isinstance(raw_actions, str) else None
        if not all(path.is_absolute() for path in (executable, routes_file, state_dir)):
            raise ListenerError("Gogurt listener paths must be absolute")
        if actions_dir is not None and not actions_dir.is_absolute():
            raise ListenerError("Gogurt listener actions directory must be absolute")
        if not isinstance(raw["marker_name"], str):
            raise ListenerError("installed Gogurt listener marker name must be a string")
        try:
            validate_gogurt_marker_name(raw["marker_name"])
        except ConfigError as exc:
            raise ListenerError("installed Gogurt listener marker name is invalid") from exc
        return cls(
            executable=executable,
            routes_file=routes_file,
            actions_dir=actions_dir,
            marker_name=raw["marker_name"],
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


def _safe_diagnostic(scope: str, exc: BaseException) -> str:
    value = " ".join(f"{scope}: {type(exc).__name__}: {exc}".splitlines())
    if len(value) <= LISTENER_DIAGNOSTIC_LIMIT:
        return value
    return value[: LISTENER_DIAGNOSTIC_LIMIT - 1] + "…"


def _combine_diagnostics(*values: str | None) -> str | None:
    combined = "; ".join(value for value in values if value)
    if not combined:
        return None
    if len(combined) <= LISTENER_DIAGNOSTIC_LIMIT:
        return combined
    return combined[: LISTENER_DIAGNOSTIC_LIMIT - 1] + "…"


def _secure_listener_state(paths: ListenerPaths) -> None:
    ensure_private_directory(paths.state_dir)
    ensure_private_files(
        (
            paths.config_file,
            paths.database_file,
            paths.heartbeat_file,
            paths.lock_file,
            paths.log_file,
            *paths.state_dir.glob(f"{paths.database_file.name}-*"),
            *paths.state_dir.glob(f"{paths.log_file.name}.*"),
        )
    )


def _require_matching_state(config: ListenerConfig, paths: ListenerPaths) -> None:
    if config.state_dir != paths.state_dir:
        raise ListenerError("installed Gogurt listener state directory does not match its config")


def _validate_global_configuration(config: ListenerConfig, paths: ListenerPaths) -> None:
    _require_matching_state(config, paths)
    _listener_interval(config.interval_seconds)
    validate_gogurt_marker_name(config.marker_name)
    if not config.executable.is_file() or (
        sys.platform != "win32" and not os.access(config.executable, os.X_OK)
    ):
        raise ListenerError("installed Gogurt executable is absent or not executable")
    if config.actions_dir is not None and not config.actions_dir.is_dir():
        raise ListenerError("installed Gogurt actions directory is absent")
    validate_gogurt_action_executables(
        config.routes_file,
        actions_dir=config.actions_dir,
    )


class ListenerStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _connect(self, *, timeout_seconds: float = 30) -> sqlite3.Connection:
        ensure_private_directory(self.path.parent)
        ensure_private_file(self.path)
        connection = sqlite3.connect(self.path, timeout=timeout_seconds)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        ensure_private_files(self.path.parent.glob(f"{self.path.name}-*"))
        return connection

    def create(self) -> None:
        with closing(self._connect()) as connection:
            journal_mode = connection.execute("PRAGMA journal_mode").fetchone()
            if journal_mode is None or str(journal_mode[0]).casefold() != "wal":
                configured = connection.execute("PRAGMA journal_mode = WAL").fetchone()
                if configured is None or str(configured[0]).casefold() != "wal":
                    raise ListenerError("Gogurt listener state could not enable WAL journaling")
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
                plan_status = plan.get("status")
                if plan_status in {"unmarked", "attention"}:
                    continue
                if plan_status != "ready":
                    raise ListenerError("Gogurt returned an invalid listener plan status")
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

    def mark_running_uncertain(self, dispatch_id: str, *, error: str, now: float) -> None:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE dispatches
                SET state = 'uncertain', completed_at = ?, next_retry_at = NULL,
                    exit_code = NULL, error = ?
                WHERE dispatch_id = ? AND state = 'running'
                """,
                (now, error, dispatch_id),
            )
            connection.commit()

    def summary(self, *, timeout_seconds: float = 30) -> dict[str, object]:
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return {"counts": {}, "attention": []}
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise OSError(f"Gogurt listener state is not a regular database: {self.path}")
        try:
            with closing(self._connect(timeout_seconds=timeout_seconds)) as connection:
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
        except sqlite3.OperationalError as exc:
            if "no such table: dispatches" not in str(exc):
                raise
            # The native service may have created the database file but not yet
            # committed its schema while an install/status health probe runs.
            return {"counts": {}, "attention": []}
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
        ensure_private_directory(self.path.parent)
        ensure_private_file(self.path)
        flags = os.O_RDWR | os.O_APPEND | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.path, flags)
        self._stream = os.fdopen(descriptor, "a+b")
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
        # A process-control signal may invoke request_stop while the main
        # thread is sampling active custody for a heartbeat.
        self._active_lock = threading.RLock()
        self._active_dispatch: str | None = None
        self._active_process: subprocess.Popen[bytes] | None = None
        self._health_lock = threading.Lock()
        self._configuration_diagnostic: str | None = None
        self._runtime_diagnostic: str | None = None
        self._worker_failure: Exception | None = None
        self._mount_attention: list[dict[str, str]] = []
        self.started_at = self.clock()

    def request_stop(self) -> None:
        self.stop_event.set()
        with self._active_lock:
            process = self._active_process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError as exc:
                self.logger.warning("action terminate=%s", _safe_diagnostic("failed", exc))

    def _settle_worker(self, worker: threading.Thread) -> None:
        worker.join(timeout=LISTENER_ACTION_TERMINATE_SECONDS)
        kill_diagnostic: str | None = None
        with self._active_lock:
            process = self._active_process
        if process is not None and process.poll() is None:
            try:
                process.kill()
                process.wait(timeout=LISTENER_ACTION_KILL_SECONDS)
            except (OSError, subprocess.TimeoutExpired) as exc:
                kill_diagnostic = _safe_diagnostic("force action stop", exc)
                self.logger.error("action=%s", kill_diagnostic)
        if worker.is_alive():
            worker.join(timeout=LISTENER_ACTION_KILL_SECONDS)
        with self._active_lock:
            dispatch_id = self._active_dispatch
            process = self._active_process
        process_live = process is not None and process.poll() is None
        if not worker.is_alive() and not process_live:
            return
        process_state = "live child process" if process_live else "unsettled dispatch worker"
        diagnostic = _combine_diagnostics(
            f"listener shutdown: {process_state}",
            kill_diagnostic,
        )
        assert diagnostic is not None
        if dispatch_id is not None:
            try:
                self.store.mark_running_uncertain(
                    dispatch_id,
                    error="listener shutdown could not prove action custody settlement",
                    now=self.clock(),
                )
            except Exception as exc:
                diagnostic = _combine_diagnostics(
                    diagnostic,
                    _safe_diagnostic("mark action custody uncertain", exc),
                )
                assert diagnostic is not None
        with self._health_lock:
            self._runtime_diagnostic = diagnostic
        raise ListenerError(diagnostic)

    def _planner(self, mount_point: Path) -> Mapping[str, object]:
        try:
            return plan_gogurt_action(
                self.config.routes_file,
                mount_point,
                actions_dir=self.config.actions_dir,
                marker_name=self.config.marker_name,
            )
        except (ConfigError, ListenerError, OSError, UnicodeError, ValueError) as exc:
            diagnostic = _safe_diagnostic("mount input", exc)
            with self._health_lock:
                if len(self._mount_attention) < LISTENER_MOUNT_ATTENTION_LIMIT:
                    self._mount_attention.append(
                        {
                            "mount_point": str(mount_point),
                            "diagnostic": diagnostic,
                        }
                    )
            self.logger.warning("mount=%s attention=%s", mount_point, diagnostic)
            return {"status": "attention"}

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
        with self._health_lock:
            configuration_diagnostic = self._configuration_diagnostic
            runtime_diagnostic = self._runtime_diagnostic
            mount_attention = list(self._mount_attention)
        payload = {
            "schema": LISTENER_HEARTBEAT_SCHEMA,
            "version": _package_version(),
            "pid": os.getpid(),
            "started_at": _now_text(self.started_at),
            "heartbeat_at": _now_text(self.clock()),
            "queue_depth": self.dispatch_queue.qsize(),
            "active_dispatch": active,
            "dispatches": summary,
            "configuration": {
                "status": "failed" if configuration_diagnostic is not None else "valid",
                "diagnostic": configuration_diagnostic,
            },
            "runtime": {
                "status": "failed" if runtime_diagnostic is not None else "running",
                "diagnostic": runtime_diagnostic,
            },
            "mount_attention": mount_attention,
        }
        atomic_write(
            self.paths.heartbeat_file,
            (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            mode=PRIVATE_FILE_MODE,
        )

    def _worker(self) -> None:
        while not self.stop_event.is_set():
            try:
                dispatch_id = self.dispatch_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            with self._queue_lock:
                self._queued.discard(dispatch_id)
            while not self.stop_event.is_set():
                with self._health_lock:
                    configuration_valid = self._configuration_diagnostic is None
                if configuration_valid:
                    break
                self.stop_event.wait(0.2)
            if self.stop_event.is_set():
                self.dispatch_queue.task_done()
                continue
            plan = self.store.start_dispatch(dispatch_id, now=self.clock())
            if plan is None:
                self.dispatch_queue.task_done()
                continue
            with self._active_lock:
                self._active_dispatch = dispatch_id
            self._heartbeat()
            return_code: int | None = None
            error: str | None = None
            process_started = False
            process: subprocess.Popen[bytes] | None = None
            try:
                if self.stop_event.is_set():
                    error = "listener stopped before the action process acquired custody"
                else:
                    command = revalidate_gogurt_action(plan)
                    process = subprocess.Popen(
                        command,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    process_started = True
                    with self._active_lock:
                        self._active_process = process
                        stop_requested = self.stop_event.is_set()
                    if stop_requested and process.poll() is None:
                        process.terminate()
                    return_code = process.wait()
            except (ConfigError, OSError, ValueError) as exc:
                error = str(exc)
            state = self.store.finish_dispatch(
                dispatch_id,
                return_code=return_code,
                error=error,
                uncertain=(
                    process_started
                    and self.stop_event.is_set()
                    and (return_code != 0 or error is not None)
                ),
                now=self.clock(),
            )
            with self._active_lock:
                if process is None or process.poll() is not None:
                    self._active_process = None
                self._active_dispatch = None
            self.logger.info(
                "dispatch=%s state=%s exit=%s error=%s",
                dispatch_id,
                state,
                return_code,
                error,
            )
            self.dispatch_queue.task_done()
            self._heartbeat()

    def _supervised_worker(self) -> None:
        try:
            self._worker()
        except Exception as exc:
            diagnostic = _safe_diagnostic("dispatch worker", exc)
            with self._health_lock:
                self._worker_failure = exc
                self._runtime_diagnostic = diagnostic
            self.logger.error("runtime=%s", diagnostic)
            self.stop_event.set()
            try:
                self._heartbeat()
            except Exception as heartbeat_exc:
                self.logger.error(
                    "runtime heartbeat=%s",
                    _safe_diagnostic("dispatch worker heartbeat", heartbeat_exc),
                )

    def run_once(self) -> None:
        now = self.clock()
        with self._health_lock:
            self._mount_attention = []
        try:
            _validate_global_configuration(self.config, self.paths)
        except (ConfigError, ListenerError, OSError, UnicodeError, ValueError) as exc:
            diagnostic = _safe_diagnostic("global configuration", exc)
            with self._health_lock:
                self._configuration_diagnostic = diagnostic
            self.logger.error("configuration=%s", diagnostic)
            self._heartbeat()
            return
        with self._health_lock:
            self._configuration_diagnostic = None
        try:
            mount_points = self.discover()
        except (OSError, ValueError) as exc:
            diagnostic = _safe_diagnostic("mount discovery", exc)
            with self._health_lock:
                self._mount_attention.append({"mount_point": "", "diagnostic": diagnostic})
            self.logger.warning("mount discovery attention=%s", diagnostic)
            self._enqueue(self.store.runnable(now=now))
            self._heartbeat()
            return
        queued = self.store.observe(mount_points, self._planner, now=now)
        self._enqueue(queued)
        self._enqueue(self.store.runnable(now=now))
        self._heartbeat()

    def run(self) -> None:
        self.store.create()
        worker = threading.Thread(
            target=self._supervised_worker,
            name="gogurt-dispatch",
            daemon=True,
        )
        worker.start()
        try:
            while not self.stop_event.is_set():
                self.run_once()
                self.stop_event.wait(self.config.interval_seconds)
        finally:
            self.request_stop()
            self._settle_worker(worker)
            try:
                self._heartbeat()
            except Exception as heartbeat_exc:
                if self._worker_failure is None:
                    raise
                self.logger.error(
                    "final heartbeat=%s",
                    _safe_diagnostic("listener final heartbeat", heartbeat_exc),
                )
        if self._worker_failure is not None:
            raise ListenerError(str(self._runtime_diagnostic)) from self._worker_failure


class _PrivateRotatingFileHandler(logging.handlers.RotatingFileHandler):
    def _open(self) -> Any:
        return open_private_text_append(
            Path(self.baseFilename),
            encoding=self.encoding or "utf-8",
            errors=self.errors,
        )


def _logger(paths: ListenerPaths) -> logging.Logger:
    _secure_listener_state(paths)
    ensure_private_file(paths.log_file)
    logger = logging.getLogger("gogurt.listener")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    handler = _PrivateRotatingFileHandler(
        paths.log_file,
        maxBytes=LISTENER_LOG_BYTES,
        backupCount=LISTENER_LOG_BACKUPS,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger


def run_listener(config_file: Path) -> None:
    state_dir = config_file.parent
    bootstrap_paths = ListenerPaths(
        state_dir=state_dir,
        config_file=config_file,
        database_file=state_dir / "listener.sqlite3",
        heartbeat_file=state_dir / "heartbeat.json",
        lock_file=state_dir / "listener.lock",
        log_file=state_dir / "listener.log",
        registration_file=None,
    )
    _secure_listener_state(bootstrap_paths)
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
    _require_matching_state(config, paths)
    _secure_listener_state(paths)
    runtime = ListenerRuntime(config, paths, logger=_logger(paths))

    def stop(_signum: int, _frame: object) -> None:
        runtime.request_stop()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with ListenerLock(paths.lock_file):
        runtime.logger.info("listener started pid=%s version=%s", os.getpid(), _package_version())
        try:
            runtime.run()
        except BaseException as exc:
            runtime.logger.error(
                "listener failed pid=%s diagnostic=%s",
                os.getpid(),
                _safe_diagnostic("runtime", exc),
            )
            raise
        else:
            runtime.logger.info("listener stopped pid=%s", os.getpid())


def _read_heartbeat_result(path: Path) -> tuple[dict[str, object] | None, str | None]:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, _safe_diagnostic("listener heartbeat", exc)
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        return None, "listener heartbeat: path is not a regular file"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError) as exc:
        return None, _safe_diagnostic("listener heartbeat", exc)
    if not isinstance(value, dict):
        return None, "listener heartbeat: payload is not an object"
    if value.get("schema") != LISTENER_HEARTBEAT_SCHEMA:
        return None, "listener heartbeat: schema is invalid"
    if value.get("version") != _package_version():
        return None, "listener heartbeat: version differs from the installed listener"
    configuration = value.get("configuration")
    if not isinstance(configuration, dict) or configuration.get("status") not in {
        "valid",
        "failed",
    }:
        return None, "listener heartbeat: configuration status is invalid"
    runtime = value.get("runtime")
    if not isinstance(runtime, dict) or runtime.get("status") not in {
        "running",
        "failed",
    }:
        return None, "listener heartbeat: runtime status is invalid"
    mount_attention = value.get("mount_attention")
    if not isinstance(mount_attention, list):
        return None, "listener heartbeat: mount attention is invalid"
    return value, None


def _read_heartbeat(path: Path) -> dict[str, object] | None:
    heartbeat, _diagnostic = _read_heartbeat_result(path)
    return heartbeat


def listener_status(
    *,
    paths: ListenerPaths | None = None,
    adapter: ListenerAdapter | None = None,
    now: float | None = None,
) -> dict[str, object]:
    resolved_paths = paths or default_listener_paths()
    native_adapter = adapter or listener_adapter()
    native = native_adapter.status(resolved_paths)
    heartbeat, heartbeat_file_diagnostic = _read_heartbeat_result(resolved_paths.heartbeat_file)
    config: ListenerConfig | None = None
    config_error: str | None = None
    if resolved_paths.config_file.is_file():
        try:
            config = ListenerConfig.read(resolved_paths.config_file)
            _validate_global_configuration(config, resolved_paths)
        except (ConfigError, ListenerError, OSError, UnicodeError, ValueError) as exc:
            config_error = _safe_diagnostic("global configuration", exc)
    elif native.installed:
        config_error = (
            f"global configuration: listener config is absent: {resolved_paths.config_file}"
        )
    current = time.time() if now is None else now
    heartbeat_age: float | None = None
    if heartbeat is not None:
        try:
            heartbeat_time = datetime.fromisoformat(
                str(heartbeat["heartbeat_at"]).replace("Z", "+00:00")
            ).timestamp()
            heartbeat_age = max(0.0, current - heartbeat_time)
        except (KeyError, TypeError, ValueError) as exc:
            heartbeat = None
            heartbeat_file_diagnostic = _safe_diagnostic("listener heartbeat time", exc)
    heartbeat_diagnostic: str | None = None
    runtime_diagnostic: str | None = None
    mount_attention: list[object] = []
    if heartbeat is not None:
        configuration = cast(dict[str, object], heartbeat["configuration"])
        if configuration.get("status") == "failed":
            raw_diagnostic = configuration.get("diagnostic")
            heartbeat_diagnostic = (
                str(raw_diagnostic)
                if raw_diagnostic
                else "global configuration: listener reported failure"
            )
        runtime = cast(dict[str, object], heartbeat["runtime"])
        if runtime.get("status") == "failed":
            raw_runtime_diagnostic = runtime.get("diagnostic")
            runtime_diagnostic = (
                str(raw_runtime_diagnostic)
                if raw_runtime_diagnostic
                else "listener runtime: dispatch worker reported failure"
            )
        mount_attention = cast(list[object], heartbeat["mount_attention"])
    heartbeat_process_live = False
    if heartbeat is not None:
        heartbeat_pid = heartbeat.get("pid")
        heartbeat_process_live = (
            isinstance(heartbeat_pid, int)
            and heartbeat_pid > 0
            and _process_is_running(heartbeat_pid)
        )
        if not heartbeat_process_live:
            heartbeat = None
            heartbeat_age = None
    state_error: str | None = None
    try:
        state = ListenerStore(resolved_paths.database_file).summary(
            timeout_seconds=LISTENER_STATUS_DB_TIMEOUT_SECONDS
        )
    except (OSError, sqlite3.Error, UnicodeError, ValueError) as exc:
        state = {"counts": {}, "attention": []}
        state_error = _safe_diagnostic("listener state", exc)
    diagnostic = _combine_diagnostics(
        config_error,
        runtime_diagnostic,
        heartbeat_diagnostic,
        heartbeat_file_diagnostic,
        state_error,
    )
    if not native.installed:
        health = "absent"
    elif not native.running:
        health = "stopped"
    elif diagnostic is not None:
        health = "failed"
    elif heartbeat is None:
        health = "starting"
    else:
        interval = config.interval_seconds if config is not None else 2.0
        healthy = heartbeat_age is not None and heartbeat_age <= max(10, interval * 3)
        health = "healthy" if healthy else "stale"
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
        "mount_attention": mount_attention,
        "diagnostic": diagnostic,
    }


def _wait_for_health(
    expected: frozenset[str],
    *,
    paths: ListenerPaths,
    adapter: ListenerAdapter,
    previous_pid: int | None = None,
    terminated_pid: int | None = None,
    timeout_seconds: float = 20,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    status = listener_status(paths=paths, adapter=adapter)
    while time.monotonic() < deadline:
        heartbeat = status.get("heartbeat")
        current_pid = heartbeat.get("pid") if isinstance(heartbeat, dict) else None
        runtime_released = True
        if status["health"] in {"absent", "stopped"} and paths.lock_file.is_file():
            try:
                with ListenerLock(paths.lock_file):
                    pass
            except ListenerError:
                runtime_released = False
        if (
            status["health"] in expected
            and (previous_pid is None or current_pid != previous_pid)
            and runtime_released
            and (terminated_pid is None or not _process_is_running(terminated_pid))
        ):
            return status
        time.sleep(0.2)
        status = listener_status(paths=paths, adapter=adapter)
    raise ListenerError(f"Gogurt listener did not reach {sorted(expected)}: {status['health']}")


def _wait_for_native_absence(
    *,
    paths: ListenerPaths,
    adapter: ListenerAdapter,
    terminated_pid: int | None,
    timeout_seconds: float = 20,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    native = adapter.status(paths)
    runtime_released = False
    while True:
        runtime_released = True
        if paths.lock_file.is_file():
            try:
                with ListenerLock(paths.lock_file):
                    pass
            except ListenerError:
                runtime_released = False
        process_released = terminated_pid is None or not _process_is_running(terminated_pid)
        if not native.installed and runtime_released and process_released:
            return
        if time.monotonic() >= deadline:
            break
        time.sleep(0.2)
        native = adapter.status(paths)
    raise ListenerError(
        "Gogurt listener native uninstall did not settle: "
        f"installed={native.installed} runtime_released={runtime_released} "
        f"process_released={process_released}"
    )


def _heartbeat_pid(paths: ListenerPaths) -> int | None:
    heartbeat = _read_heartbeat(paths.heartbeat_file)
    if heartbeat is None:
        return None
    value = heartbeat.get("pid")
    return int(value) if isinstance(value, int) else None


def _process_is_running(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes

        kernel32 = cast(Any, ctypes).windll.kernel32
        handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return kernel32.GetLastError() == 5  # ERROR_ACCESS_DENIED
        try:
            return kernel32.WaitForSingleObject(handle, 0) == 0x00000102  # WAIT_TIMEOUT
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


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
    validate_gogurt_marker_name(marker_name)
    actions = actions_dir.expanduser().resolve() if actions_dir is not None else None
    if actions is not None and not actions.is_dir():
        raise NotADirectoryError(actions)
    interval = _listener_interval(interval_seconds)
    validate_gogurt_action_executables(routes, actions_dir=actions)
    resolved_paths = paths or default_listener_paths()
    native_adapter = adapter or listener_adapter()
    resolved_executable = resolve_listener_executable(
        str(executable.expanduser()) if executable is not None else None
    )
    config = ListenerConfig(
        executable=resolved_executable,
        routes_file=routes,
        actions_dir=actions,
        marker_name=marker_name,
        interval_seconds=interval,
        state_dir=resolved_paths.state_dir,
    )
    _secure_listener_state(resolved_paths)
    native_status = native_adapter.status(resolved_paths)
    previous: ListenerConfig | None = None
    previous_content: bytes | None = None
    if native_status.installed:
        if not resolved_paths.config_file.is_file():
            raise ListenerError("installed Gogurt listener config is absent")
        previous = ListenerConfig.read(resolved_paths.config_file)
        previous_content = resolved_paths.config_file.read_bytes()
    previous_pid = _heartbeat_pid(resolved_paths)
    staged_config = stage_bytes(
        resolved_paths.config_file,
        config.content(),
        mode=PRIVATE_FILE_MODE,
    )
    try:
        try:
            if native_status.installed:
                native_adapter.stop(resolved_paths)
                _wait_for_health(
                    frozenset({"absent", "stopped"}),
                    paths=resolved_paths,
                    adapter=native_adapter,
                    terminated_pid=previous_pid,
                )
            promote_staged(
                staged_config,
                resolved_paths.config_file,
                mode=PRIVATE_FILE_MODE,
            )
            # Establish schema and WAL before the native process and status
            # probes can open concurrent database connections.
            ListenerStore(resolved_paths.database_file).create()
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
        except BaseException as exc:
            rollback_errors: list[str] = []
            try:
                failed_pid = _heartbeat_pid(resolved_paths)
            except BaseException as rollback_exc:
                failed_pid = None
                rollback_errors.append(_safe_diagnostic("read failed listener pid", rollback_exc))
            try:
                native_adapter.unregister(resolved_paths)
                _wait_for_health(
                    frozenset({"absent"}),
                    paths=resolved_paths,
                    adapter=native_adapter,
                    terminated_pid=failed_pid,
                )
            except BaseException as rollback_exc:
                rollback_errors.append(_safe_diagnostic("remove failed registration", rollback_exc))
            if previous is None:
                try:
                    resolved_paths.config_file.unlink(missing_ok=True)
                except BaseException as rollback_exc:
                    rollback_errors.append(_safe_diagnostic("remove config", rollback_exc))
            else:
                assert previous_content is not None
                try:
                    atomic_write(
                        resolved_paths.config_file,
                        previous_content,
                        mode=PRIVATE_FILE_MODE,
                    )
                    native_adapter.register(
                        resolved_paths,
                        _service_command(previous, resolved_paths.config_file),
                    )
                    _wait_for_health(
                        frozenset({"healthy"}),
                        paths=resolved_paths,
                        adapter=native_adapter,
                        previous_pid=failed_pid,
                    )
                except BaseException as rollback_exc:
                    rollback_errors.append(
                        _safe_diagnostic("restore prior healthy listener", rollback_exc)
                    )
            if rollback_errors:
                detail = "; ".join(rollback_errors)
                raise ListenerError(
                    f"{_safe_diagnostic('Gogurt listener installation failed', exc)}; "
                    f"rollback failed ({detail})"
                ) from exc
            raise
    finally:
        staged_config.unlink(missing_ok=True)


def start_listener(
    *, paths: ListenerPaths | None = None, adapter: ListenerAdapter | None = None
) -> dict[str, object]:
    resolved_paths = paths or default_listener_paths()
    native_adapter = adapter or listener_adapter()
    _secure_listener_state(resolved_paths)
    config = ListenerConfig.read(resolved_paths.config_file)
    _validate_global_configuration(config, resolved_paths)
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
    previous_pid = _heartbeat_pid(resolved_paths)
    native_adapter.stop(resolved_paths)
    return _wait_for_health(
        frozenset({"absent", "stopped"}),
        paths=resolved_paths,
        adapter=native_adapter,
        terminated_pid=previous_pid,
    )


def restart_listener(
    *, paths: ListenerPaths | None = None, adapter: ListenerAdapter | None = None
) -> dict[str, object]:
    resolved_paths = paths or default_listener_paths()
    native_adapter = adapter or listener_adapter()
    _secure_listener_state(resolved_paths)
    config = ListenerConfig.read(resolved_paths.config_file)
    _validate_global_configuration(config, resolved_paths)
    previous_pid = _heartbeat_pid(resolved_paths)
    native_adapter.stop(resolved_paths)
    _wait_for_health(
        frozenset({"absent", "stopped"}),
        paths=resolved_paths,
        adapter=native_adapter,
        terminated_pid=previous_pid,
    )
    native_adapter.start(resolved_paths)
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
    previous_pid = _heartbeat_pid(resolved_paths)
    cleanup_errors: list[str] = []
    try:
        native_adapter.unregister(resolved_paths)
    except Exception as exc:
        cleanup_errors.append(_safe_diagnostic("remove native registration", exc))
    settled = False
    try:
        _wait_for_native_absence(
            paths=resolved_paths,
            adapter=native_adapter,
            terminated_pid=previous_pid,
        )
        settled = True
    except Exception as exc:
        cleanup_errors.append(_safe_diagnostic("settle native listener", exc))
    if settled and resolved_paths.state_dir.is_dir():
        try:
            shutil.rmtree(resolved_paths.state_dir)
        except Exception as exc:
            cleanup_errors.append(_safe_diagnostic("remove listener state", exc))
    if cleanup_errors:
        detail = _combine_diagnostics(*cleanup_errors)
        assert detail is not None
        raise ListenerError(f"Gogurt listener uninstall failed: {detail}")
    return listener_status(paths=resolved_paths, adapter=native_adapter)
