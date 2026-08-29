from __future__ import annotations

import faulthandler
import json
import logging
import logging.handlers
import math
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
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

from config_validation import ConfigError
from gogurt_core.core import (
    plan_gogurt_action,
    revalidate_gogurt_action,
    validate_gogurt_action_executables,
)
from gogurt_core.mounts import MountedVolumeProvider, validate_gogurt_interval
from gogurt_core.providers import GogurtProviderReference

from gogurt_listener_runtime.filesystem import (
    PRIVATE_FILE_MODE,
    atomic_write,
    ensure_private_directory,
    ensure_private_file,
    ensure_private_files,
    open_private_text_append,
    promote_staged,
    stage_bytes,
)
from gogurt_listener_runtime.platform import (
    ListenerAdapter,
    ListenerRuntimePaths,
)

LISTENER_CONFIG_SCHEMA = "gogurt-listener-config/v1"
LISTENER_HEARTBEAT_SCHEMA = "gogurt-listener-heartbeat/v1"
LISTENER_STATUS_SCHEMA = "gogurt-listener-status/v1"
LISTENER_STATE_SCHEMA = 1
LISTENER_MAX_ATTEMPTS = 3
LISTENER_RETRY_SECONDS = (5.0, 30.0)
LISTENER_LOG_BYTES = 1_048_576
LISTENER_LOG_BACKUPS = 3
LISTENER_FATAL_LOG_BYTES = 262_144
LISTENER_MOUNT_ATTENTION_LIMIT = 20
LISTENER_DIAGNOSTIC_LIMIT = 512
LISTENER_STATUS_DB_TIMEOUT_SECONDS = 0.25
LISTENER_ACTION_TERMINATE_SECONDS = 2.0
LISTENER_ACTION_KILL_SECONDS = 2.0
LISTENER_CONTROL_POLL_SECONDS = 0.2
LISTENER_HEARTBEAT_FUTURE_SECONDS = 10.0
LISTENER_MAX_JSON_INTEGER = (1 << 63) - 1
LISTENER_MAX_PID = (1 << 32) - 1
LISTENER_OPERATIONS = ("install", "status", "start", "stop", "restart", "uninstall")
_LISTENER_STATE_DDL = """
CREATE TABLE IF NOT EXISTS listener_meta (
    key TEXT NOT NULL PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS observed_mounts (
    mount_point TEXT NOT NULL PRIMARY KEY,
    present INTEGER NOT NULL CHECK (present IN (0, 1)),
    generation INTEGER NOT NULL CHECK (generation >= 1),
    marker_identity TEXT
);
CREATE TABLE IF NOT EXISTS dispatches (
    dispatch_id TEXT NOT NULL PRIMARY KEY,
    mount_point TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK (generation >= 1),
    marker_identity TEXT NOT NULL,
    route TEXT NOT NULL,
    plan_json TEXT NOT NULL CHECK (json_valid(plan_json)),
    state TEXT NOT NULL CHECK (
        state IN ('queued', 'running', 'retry', 'uncertain', 'completed', 'failed')
    ),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    observed_at REAL NOT NULL,
    started_at REAL,
    completed_at REAL,
    next_retry_at REAL,
    exit_code INTEGER,
    error TEXT
);
CREATE INDEX IF NOT EXISTS dispatches_state_idx
    ON dispatches(state, next_retry_at, observed_at, dispatch_id);
"""


class ListenerError(RuntimeError):
    """The Gogurt listener contract could not be satisfied."""


def _listener_interval(value: object) -> float:
    try:
        return validate_gogurt_interval(value)
    except (OverflowError, ValueError) as exc:
        raise ListenerError(str(exc)) from exc


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise ListenerError(f"Gogurt persisted JSON contains duplicate field: {key}")
        payload[key] = value
    return payload


def _reject_json_constant(value: str) -> object:
    raise ListenerError(f"Gogurt persisted JSON contains nonfinite number: {value}")


def _strict_json_integer(value: str) -> int:
    parsed = int(value)
    if abs(parsed) > LISTENER_MAX_JSON_INTEGER:
        raise ListenerError("Gogurt persisted JSON integer is outside the supported range")
    return parsed


def _now_text(now: float | None = None) -> str:
    value = time.time() if now is None else now
    return datetime.fromtimestamp(value, tz=UTC).isoformat().replace("+00:00", "Z")


def _validated_product_version(value: str) -> str:
    if not value or value != value.strip() or len(value) > 128:
        raise ListenerError("Gogurt product version must be a bounded nonempty string")
    return value


@dataclass(frozen=True, slots=True)
class ListenerConfig:
    executable: Path
    routes_file: Path
    actions_dir: Path | None
    interval_seconds: float
    state_dir: Path
    mounted_volume_provider: GogurtProviderReference
    listener_host_provider: GogurtProviderReference
    autorun: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "interval_seconds", _listener_interval(self.interval_seconds))
        if self.mounted_volume_provider.kind != "mounted-volume":
            raise ListenerError("Gogurt listener config requires a mounted-volume provider")
        if self.listener_host_provider.kind != "listener-host":
            raise ListenerError("Gogurt listener config requires a listener-host provider")

    def payload(self) -> dict[str, object]:
        return {
            "schema": LISTENER_CONFIG_SCHEMA,
            "executable": str(self.executable),
            "routes_file": str(self.routes_file),
            "actions_dir": str(self.actions_dir) if self.actions_dir is not None else None,
            "interval_seconds": self.interval_seconds,
            "state_dir": str(self.state_dir),
            "mounted_volume_provider": self.mounted_volume_provider.as_dict(),
            "listener_host_provider": self.listener_host_provider.as_dict(),
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
                parse_int=_strict_json_integer,
            )
        except ListenerError:
            raise
        except (FileNotFoundError, json.JSONDecodeError, UnicodeError) as exc:
            raise ListenerError(f"invalid Gogurt listener config: {path}") from exc
        expected = {
            "schema",
            "executable",
            "routes_file",
            "actions_dir",
            "interval_seconds",
            "state_dir",
            "mounted_volume_provider",
            "listener_host_provider",
            "autorun",
        }
        if not isinstance(raw, dict) or set(raw) != expected:
            raise ListenerError(f"invalid Gogurt listener config fields: {path}")
        if raw["schema"] != LISTENER_CONFIG_SCHEMA:
            raise ListenerError(f"Gogurt listener config schema is invalid: {path}")
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
        try:
            mounted_volume_provider = GogurtProviderReference.from_mapping(
                raw["mounted_volume_provider"]
            )
            listener_host_provider = GogurtProviderReference.from_mapping(
                raw["listener_host_provider"]
            )
            return cls(
                executable=executable,
                routes_file=routes_file,
                actions_dir=actions_dir,
                interval_seconds=interval,
                state_dir=state_dir,
                mounted_volume_provider=mounted_volume_provider,
                listener_host_provider=listener_host_provider,
            )
        except (TypeError, ValueError) as exc:
            raise ListenerError("installed Gogurt provider identity is invalid") from exc


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


def _secure_sqlite_database_metadata(path: Path) -> bool:
    """Validate and normalize the database without opening a non-SQLite descriptor."""

    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise OSError(f"Gogurt listener state path is not a regular file: {path}")
    if os.name != "nt":
        os.chmod(path, PRIVATE_FILE_MODE, follow_symlinks=False)
    return True


def _secure_listener_state(paths: ListenerRuntimePaths) -> None:
    ensure_private_directory(paths.state_dir)
    _secure_sqlite_database_metadata(paths.database_file)
    ensure_private_files(
        (
            paths.config_file,
            paths.heartbeat_file,
            paths.lock_file,
            paths.log_file,
            paths.stop_file,
            *paths.state_dir.glob(f"{paths.log_file.name}.*"),
            *paths.state_dir.glob("listener.fatal.log*"),
        )
    )


def _listener_schema_signature(connection: sqlite3.Connection) -> tuple[tuple[str, ...], ...]:
    return tuple(
        (
            str(row[0]),
            str(row[1]),
            str(row[2]),
            " ".join(str(row[3]).casefold().split()),
        )
        for row in connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_schema "
            "WHERE type IN ('table', 'index') AND sql IS NOT NULL "
            "AND name NOT LIKE 'sqlite_%' ORDER BY type, name"
        )
    )


def _verify_listener_schema(connection: sqlite3.Connection) -> None:
    with closing(sqlite3.connect(":memory:")) as expected:
        expected.executescript(_LISTENER_STATE_DDL)
        expected_signature = _listener_schema_signature(expected)
    actual_signature = _listener_schema_signature(connection)
    if actual_signature != expected_signature:
        raise ListenerError("Gogurt listener state does not match its exact current schema")
    if tuple(str(row[0]) for row in connection.execute("PRAGMA quick_check")) != ("ok",):
        raise ListenerError("Gogurt listener state failed SQLite quick_check")
    if list(connection.execute("PRAGMA foreign_key_check")):
        raise ListenerError("Gogurt listener state failed SQLite foreign_key_check")


def _require_matching_state(config: ListenerConfig, paths: ListenerRuntimePaths) -> None:
    if config.state_dir != paths.state_dir:
        raise ListenerError("installed Gogurt listener state directory does not match its config")


def _validate_global_configuration(config: ListenerConfig, paths: ListenerRuntimePaths) -> None:
    _require_matching_state(config, paths)
    _listener_interval(config.interval_seconds)
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
        if not _secure_sqlite_database_metadata(self.path):
            raise FileNotFoundError(self.path)
        connection = sqlite3.connect(self.path, timeout=timeout_seconds)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def create(self) -> None:
        ensure_private_directory(self.path.parent)
        if not _secure_sqlite_database_metadata(self.path):
            ensure_private_file(self.path)
        with closing(self._connect()) as connection:
            journal_mode = connection.execute("PRAGMA journal_mode").fetchone()
            if journal_mode is None or str(journal_mode[0]).casefold() != "wal":
                configured = connection.execute("PRAGMA journal_mode = WAL").fetchone()
                if configured is None or str(configured[0]).casefold() != "wal":
                    raise ListenerError("Gogurt listener state could not enable WAL journaling")
            connection.executescript(_LISTENER_STATE_DDL)
            _verify_listener_schema(connection)
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
        current = tuple(sorted({str(path.resolve()) for path in mount_points}, key=str.casefold))
        queued: list[str] = []
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TEMP TABLE current_mounts (mount_point TEXT PRIMARY KEY) WITHOUT ROWID"
            )
            connection.executemany(
                "INSERT INTO current_mounts(mount_point) VALUES (?)",
                ((path_value,) for path_value in current),
            )
            connection.execute(
                """
                UPDATE observed_mounts
                SET present = 0
                WHERE present = 1
                  AND NOT EXISTS (
                    SELECT 1 FROM current_mounts
                    WHERE current_mounts.mount_point = observed_mounts.mount_point
                  )
                """
            )
            known_rows = connection.execute(
                """
                SELECT observed_mounts.*
                FROM current_mounts
                CROSS JOIN observed_mounts USING (mount_point)
                """
            ).fetchall()
            known = {str(row["mount_point"]): row for row in known_rows}
            for path_value in current:
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

    def runnable(self, *, now: float, limit: int) -> list[str]:
        if limit < 1:
            return []
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT dispatch_id FROM dispatches
                WHERE state = 'queued'
                   OR (state = 'retry' AND next_retry_at <= ?)
                ORDER BY observed_at, dispatch_id
                LIMIT ?
                """,
                (now, limit),
            ).fetchall()
        return [str(row["dispatch_id"]) for row in rows]

    def start_dispatch(self, dispatch_id: str, *, now: float) -> dict[str, object] | None:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT plan_json FROM dispatches
                WHERE dispatch_id = ?
                  AND (
                    state = 'queued'
                    OR (state = 'retry' AND next_retry_at <= ?)
                  )
                """,
                (dispatch_id, now),
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


class _FileLock:
    def __init__(self, path: Path, *, timeout_seconds: float, diagnostic: str) -> None:
        self.path = path
        self.timeout_seconds = timeout_seconds
        self.diagnostic = diagnostic
        self._stream: Any = None

    def __enter__(self) -> _FileLock:
        ensure_private_directory(self.path.parent)
        ensure_private_file(self.path)
        flags = os.O_RDWR | os.O_APPEND | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.path, flags)
        self._stream = os.fdopen(descriptor, "a+b")
        self._stream.seek(0)
        self._stream.write(b"0")
        self._stream.flush()
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    self._stream.seek(0)
                    windows_locking = cast(Any, msvcrt)
                    windows_locking.locking(self._stream.fileno(), windows_locking.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    self._stream.close()
                    self._stream = None
                    raise ListenerError(self.diagnostic) from exc
                time.sleep(0.01)
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


class ListenerLock(_FileLock):
    def __init__(self, path: Path) -> None:
        super().__init__(
            path,
            timeout_seconds=0,
            diagnostic="another Gogurt listener already owns this state",
        )


class ListenerRuntime:
    def __init__(
        self,
        config: ListenerConfig,
        paths: ListenerRuntimePaths,
        *,
        mounted_volume_provider: MountedVolumeProvider,
        product_version: str,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        logger: logging.Logger | None = None,
    ) -> None:
        self.config = config
        self.product_version = _validated_product_version(product_version)
        self.paths = paths
        if mounted_volume_provider.reference != config.mounted_volume_provider:
            raise ListenerError(
                "Gogurt mounted-volume provider differs from persisted listener identity"
            )
        self.mounted_volume_provider = mounted_volume_provider
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
                provider=self.mounted_volume_provider,
                actions_dir=self.config.actions_dir,
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

    def _enqueue_runnable(self, *, now: float) -> None:
        with self._active_lock:
            active = self._active_dispatch is not None
        with self._queue_lock:
            available = 1 - len(self._queued) - int(active)
        if available > 0:
            self._enqueue(self.store.runnable(now=now, limit=available))

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
            "runtime_version": self.product_version,
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
        # Readers may observe either complete snapshot. They must not hold a
        # publication lock: atomic_write stages the replacement, and its
        # Windows promotion path settles transient open-reader conflicts.
        atomic_write(
            self.paths.heartbeat_file,
            (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            mode=PRIVATE_FILE_MODE,
        )

    def _stop_requested(self) -> bool:
        try:
            info = self.paths.stop_file.lstat()
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ListenerError("Gogurt listener stop request is not a regular file")
        return True

    def _wait_for_next_poll(self) -> None:
        deadline = time.monotonic() + self.config.interval_seconds
        while not self.stop_event.is_set():
            if self._stop_requested():
                self.logger.info("listener cooperative stop requested")
                self.request_stop()
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self.stop_event.wait(min(LISTENER_CONTROL_POLL_SECONDS, remaining))

    def _worker(self) -> None:
        while not self.stop_event.is_set():
            try:
                dispatch_id = self.dispatch_queue.get(timeout=0.2)
            except queue.Empty:
                continue
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
            with self._queue_lock:
                self._queued.discard(dispatch_id)
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
                    command = revalidate_gogurt_action(
                        plan,
                        provider=self.mounted_volume_provider,
                    )
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
            mount_points = self.mounted_volume_provider.discover()
        except (OSError, ValueError) as exc:
            diagnostic = _safe_diagnostic("mount discovery", exc)
            with self._health_lock:
                self._mount_attention.append({"mount_point": "", "diagnostic": diagnostic})
            self.logger.warning("mount discovery attention=%s", diagnostic)
            self._enqueue_runnable(now=now)
            self._heartbeat()
            return
        self.store.observe(mount_points, self._planner, now=now)
        self._enqueue_runnable(now=now)
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
                self._wait_for_next_poll()
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


def _logger(paths: ListenerRuntimePaths) -> logging.Logger:
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


@contextmanager
def _fatal_signal_log(paths: ListenerRuntimePaths) -> Iterator[None]:
    """Retain bounded interpreter fatal-signal evidence outside the rotating log FD."""

    path = paths.state_dir / "listener.fatal.log"
    backup = paths.state_dir / "listener.fatal.log.1"
    ensure_private_file(path)
    if path.stat().st_size >= LISTENER_FATAL_LOG_BYTES:
        if backup.exists() or backup.is_symlink():
            ensure_private_file(backup)
        os.replace(path, backup)
        ensure_private_file(path)
    stream = open_private_text_append(path, encoding="utf-8", errors="backslashreplace")
    try:
        faulthandler.enable(file=stream, all_threads=True)
        yield
    finally:
        faulthandler.disable()
        stream.close()


def run_listener(
    config_file: Path,
    *,
    mounted_volume_provider: MountedVolumeProvider,
    product_version: str,
) -> None:
    state_dir = config_file.parent
    bootstrap_paths = ListenerRuntimePaths(
        state_dir=state_dir,
        config_file=config_file,
        database_file=state_dir / "listener.sqlite3",
        heartbeat_file=state_dir / "heartbeat.json",
        lock_file=state_dir / "listener.lock",
        log_file=state_dir / "listener.log",
        stop_file=state_dir / "stop.request",
    )
    _secure_listener_state(bootstrap_paths)
    current_product_version = _validated_product_version(product_version)
    config = ListenerConfig.read(config_file)
    paths = ListenerRuntimePaths(
        state_dir=config.state_dir,
        config_file=config_file,
        database_file=config.state_dir / "listener.sqlite3",
        heartbeat_file=config.state_dir / "heartbeat.json",
        lock_file=config.state_dir / "listener.lock",
        log_file=config.state_dir / "listener.log",
        stop_file=config.state_dir / "stop.request",
    )
    _require_matching_state(config, paths)
    _secure_listener_state(paths)
    runtime = ListenerRuntime(
        config,
        paths,
        mounted_volume_provider=mounted_volume_provider,
        product_version=current_product_version,
        logger=_logger(paths),
    )

    def stop(_signum: int, _frame: object) -> None:
        runtime.request_stop()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with ListenerLock(paths.lock_file), _fatal_signal_log(paths):
        runtime.logger.info(
            "listener started pid=%s version=%s",
            os.getpid(),
            current_product_version,
        )
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


def _heartbeat_timestamp(value: object, *, field: str) -> float:
    if not isinstance(value, str) or not value:
        raise ListenerError(f"Gogurt listener heartbeat {field} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timezone is absent")
        timestamp = parsed.timestamp()
    except (OSError, OverflowError, ValueError) as exc:
        raise ListenerError(f"Gogurt listener heartbeat {field} is invalid") from exc
    if not math.isfinite(timestamp):
        raise ListenerError(f"Gogurt listener heartbeat {field} is invalid")
    return timestamp


def _validate_heartbeat(value: object) -> dict[str, object]:
    expected = {
        "schema",
        "runtime_version",
        "pid",
        "started_at",
        "heartbeat_at",
        "queue_depth",
        "active_dispatch",
        "dispatches",
        "configuration",
        "runtime",
        "mount_attention",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise ListenerError("Gogurt listener heartbeat fields are invalid")
    if value["schema"] != LISTENER_HEARTBEAT_SCHEMA:
        raise ListenerError("Gogurt listener heartbeat schema is invalid")
    if not isinstance(value["runtime_version"], str):
        raise ListenerError("Gogurt listener heartbeat runtime version is invalid")
    _validated_product_version(value["runtime_version"])
    pid = value["pid"]
    if isinstance(pid, bool) or not isinstance(pid, int) or not 0 < pid <= LISTENER_MAX_PID:
        raise ListenerError("Gogurt listener heartbeat PID is invalid")
    started_at = _heartbeat_timestamp(value["started_at"], field="start time")
    heartbeat_at = _heartbeat_timestamp(value["heartbeat_at"], field="time")
    if heartbeat_at + LISTENER_HEARTBEAT_FUTURE_SECONDS < started_at:
        raise ListenerError("Gogurt listener heartbeat precedes its start time")
    queue_depth = value["queue_depth"]
    if isinstance(queue_depth, bool) or not isinstance(queue_depth, int) or queue_depth < 0:
        raise ListenerError("Gogurt listener heartbeat queue depth is invalid")
    active_dispatch = value["active_dispatch"]
    if active_dispatch is not None and not isinstance(active_dispatch, str):
        raise ListenerError("Gogurt listener heartbeat active dispatch is invalid")
    if not isinstance(value["dispatches"], dict):
        raise ListenerError("Gogurt listener heartbeat dispatch summary is invalid")
    for field in ("configuration", "runtime"):
        section = value[field]
        if not isinstance(section, dict) or set(section) != {"status", "diagnostic"}:
            raise ListenerError(f"Gogurt listener heartbeat {field} is invalid")
        allowed = {"valid", "failed"} if field == "configuration" else {"running", "failed"}
        if section["status"] not in allowed:
            raise ListenerError(f"Gogurt listener heartbeat {field} status is invalid")
        diagnostic = section["diagnostic"]
        if diagnostic is not None and not isinstance(diagnostic, str):
            raise ListenerError(f"Gogurt listener heartbeat {field} diagnostic is invalid")
    attention = value["mount_attention"]
    if not isinstance(attention, list) or len(attention) > LISTENER_MOUNT_ATTENTION_LIMIT:
        raise ListenerError("Gogurt listener heartbeat mount attention is invalid")
    if any(
        not isinstance(item, dict)
        or set(item) != {"mount_point", "diagnostic"}
        or not all(isinstance(item[key], str) for key in ("mount_point", "diagnostic"))
        for item in attention
    ):
        raise ListenerError("Gogurt listener heartbeat mount attention is invalid")
    return value


def _read_heartbeat_result(
    path: Path,
) -> tuple[dict[str, object] | None, str | None]:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, _safe_diagnostic("listener heartbeat", exc)
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        return None, "listener heartbeat: path is not a regular file"
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_strict_json_object,
            parse_constant=_reject_json_constant,
            parse_int=_strict_json_integer,
        )
        return _validate_heartbeat(value), None
    except (ListenerError, OSError, json.JSONDecodeError, UnicodeError) as exc:
        return None, _safe_diagnostic("listener heartbeat", exc)


def _read_heartbeat(path: Path) -> dict[str, object] | None:
    heartbeat, _diagnostic = _read_heartbeat_result(path)
    return heartbeat


def listener_status(
    *,
    paths: ListenerRuntimePaths,
    adapter: ListenerAdapter,
    product_version: str,
    now: float | None = None,
) -> dict[str, object]:
    resolved_paths = paths
    native_adapter = adapter
    native = native_adapter.status(resolved_paths)
    expected_product_version = _validated_product_version(product_version)
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
            heartbeat_time = _heartbeat_timestamp(heartbeat["heartbeat_at"], field="time")
            raw_age = current - heartbeat_time
            if raw_age < -LISTENER_HEARTBEAT_FUTURE_SECONDS:
                raise ListenerError("Gogurt listener heartbeat time is in the future")
            heartbeat_age = max(0.0, raw_age)
        except (KeyError, ListenerError, OSError, OverflowError, TypeError, ValueError) as exc:
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
            and not isinstance(heartbeat_pid, bool)
            and heartbeat_pid > 0
            and native_adapter.process_is_running(heartbeat_pid)
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
        "manager_version": expected_product_version,
        "platform": sys.platform,
        "installed": native.installed,
        "enabled": native.enabled,
        "running": native.running,
        "health": health,
        "config_file": str(resolved_paths.config_file),
        "state_dir": str(resolved_paths.state_dir),
        "executable": str(config.executable) if config is not None else None,
        "mounted_volume_provider": config.mounted_volume_provider.as_dict()
        if config is not None
        else None,
        "listener_host_provider": (
            config.listener_host_provider.as_dict() if config is not None else None
        ),
        "heartbeat_age_seconds": heartbeat_age,
        "heartbeat": heartbeat,
        "dispatches": state,
        "mount_attention": mount_attention,
        "diagnostic": diagnostic,
    }


def _wait_for_health(
    expected: frozenset[str],
    *,
    paths: ListenerRuntimePaths,
    adapter: ListenerAdapter,
    product_version: str,
    previous_pid: int | None = None,
    terminated_pid: int | None = None,
    timeout_seconds: float = 20,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    status = listener_status(
        paths=paths,
        adapter=adapter,
        product_version=product_version,
    )
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
        dispatches = status.get("dispatches")
        counts = dispatches.get("counts") if isinstance(dispatches, dict) else None
        custody_released = not isinstance(counts, dict) or counts.get("running", 0) == 0
        if (
            status["health"] in expected
            and (previous_pid is None or current_pid != previous_pid)
            and runtime_released
            and custody_released
            and (terminated_pid is None or not adapter.process_is_running(terminated_pid))
        ):
            return status
        time.sleep(0.2)
        status = listener_status(
            paths=paths,
            adapter=adapter,
            product_version=product_version,
        )
    raise ListenerError(f"Gogurt listener did not reach {sorted(expected)}: {status['health']}")


def _wait_for_native_absence(
    *,
    paths: ListenerRuntimePaths,
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
        process_released = terminated_pid is None or not adapter.process_is_running(terminated_pid)
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


def _heartbeat_pid(paths: ListenerRuntimePaths) -> int | None:
    heartbeat = _read_heartbeat(paths.heartbeat_file)
    if heartbeat is None:
        return None
    value = heartbeat.get("pid")
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and 0 < value <= LISTENER_MAX_PID
        else None
    )


def _clear_stop_request(paths: ListenerRuntimePaths) -> None:
    try:
        info = paths.stop_file.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ListenerError("Gogurt listener stop request is not a regular file")
    paths.stop_file.unlink()


def _remove_state_root(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    is_junction = bool(getattr(path, "is_junction", lambda: False)())
    if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode) and not is_junction:
        shutil.rmtree(path)
    else:
        path.unlink()
    if path.exists() or path.is_symlink():
        raise OSError(f"Gogurt listener state remains after cleanup: {path}")


def install_listener(
    routes_file: Path,
    *,
    actions_dir: Path | None,
    interval_seconds: float = 2.0,
    executable: Path,
    paths: ListenerRuntimePaths,
    adapter: ListenerAdapter,
    product_version: str,
    mounted_volume_provider: GogurtProviderReference,
    listener_host_provider: GogurtProviderReference,
    wait_for_health: bool = True,
) -> dict[str, object]:
    expected_product_version = _validated_product_version(product_version)
    routes = routes_file.expanduser().resolve()
    actions = actions_dir.expanduser().resolve() if actions_dir is not None else None
    if actions is not None and not actions.is_dir():
        raise NotADirectoryError(actions)
    interval = _listener_interval(interval_seconds)
    validate_gogurt_action_executables(routes, actions_dir=actions)
    resolved_paths = paths
    native_adapter = adapter
    resolved_executable = executable.expanduser().resolve()
    config = ListenerConfig(
        executable=resolved_executable,
        routes_file=routes,
        actions_dir=actions,
        interval_seconds=interval,
        state_dir=resolved_paths.state_dir,
        mounted_volume_provider=mounted_volume_provider,
        listener_host_provider=listener_host_provider,
    )
    _secure_listener_state(resolved_paths)
    native_status = native_adapter.status(resolved_paths)
    previous: ListenerConfig | None = None
    previous_content: bytes | None = None
    requested_content = config.content()
    if native_status.installed:
        if not resolved_paths.config_file.is_file():
            raise ListenerError("installed Gogurt listener config is absent")
        previous = ListenerConfig.read(resolved_paths.config_file)
        previous_content = resolved_paths.config_file.read_bytes()
        if (
            previous_content == requested_content
            and native_status.enabled
            and native_status.running
        ):
            current = listener_status(
                paths=resolved_paths,
                adapter=native_adapter,
                product_version=expected_product_version,
            )
            if current.get("health") == "healthy":
                return current
    previous_pid = _heartbeat_pid(resolved_paths)
    staged_config = stage_bytes(
        resolved_paths.config_file,
        requested_content,
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
                    product_version=expected_product_version,
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
            _clear_stop_request(resolved_paths)
            native_adapter.register(
                resolved_paths,
                _service_command(config, resolved_paths.config_file),
            )
            if wait_for_health:
                return _wait_for_health(
                    frozenset({"healthy"}),
                    paths=resolved_paths,
                    adapter=native_adapter,
                    product_version=expected_product_version,
                    previous_pid=previous_pid,
                )
            return listener_status(
                paths=resolved_paths,
                adapter=native_adapter,
                product_version=expected_product_version,
            )
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
                    product_version=expected_product_version,
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
                    _clear_stop_request(resolved_paths)
                    native_adapter.register(
                        resolved_paths,
                        _service_command(previous, resolved_paths.config_file),
                    )
                    _wait_for_health(
                        frozenset({"healthy"}),
                        paths=resolved_paths,
                        adapter=native_adapter,
                        product_version=expected_product_version,
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
    *,
    paths: ListenerRuntimePaths,
    adapter: ListenerAdapter,
    product_version: str,
) -> dict[str, object]:
    resolved_paths = paths
    native_adapter = adapter
    _secure_listener_state(resolved_paths)
    config = ListenerConfig.read(resolved_paths.config_file)
    _validate_global_configuration(config, resolved_paths)
    previous_pid = _heartbeat_pid(resolved_paths)
    _clear_stop_request(resolved_paths)
    native_adapter.start(resolved_paths)
    return _wait_for_health(
        frozenset({"healthy"}),
        paths=resolved_paths,
        adapter=native_adapter,
        product_version=product_version,
        previous_pid=previous_pid,
    )


def stop_listener(
    *,
    paths: ListenerRuntimePaths,
    adapter: ListenerAdapter,
    product_version: str,
) -> dict[str, object]:
    resolved_paths = paths
    native_adapter = adapter
    previous_pid = _heartbeat_pid(resolved_paths)
    native_adapter.stop(resolved_paths)
    return _wait_for_health(
        frozenset({"absent", "stopped"}),
        paths=resolved_paths,
        adapter=native_adapter,
        product_version=product_version,
        terminated_pid=previous_pid,
    )


def restart_listener(
    *,
    paths: ListenerRuntimePaths,
    adapter: ListenerAdapter,
    product_version: str,
) -> dict[str, object]:
    resolved_paths = paths
    native_adapter = adapter
    _secure_listener_state(resolved_paths)
    config = ListenerConfig.read(resolved_paths.config_file)
    _validate_global_configuration(config, resolved_paths)
    previous_pid = _heartbeat_pid(resolved_paths)
    native_adapter.stop(resolved_paths)
    _wait_for_health(
        frozenset({"absent", "stopped"}),
        paths=resolved_paths,
        adapter=native_adapter,
        product_version=product_version,
        terminated_pid=previous_pid,
    )
    _clear_stop_request(resolved_paths)
    native_adapter.start(resolved_paths)
    return _wait_for_health(
        frozenset({"healthy"}),
        paths=resolved_paths,
        adapter=native_adapter,
        product_version=product_version,
        previous_pid=previous_pid,
    )


def uninstall_listener(
    *,
    paths: ListenerRuntimePaths,
    adapter: ListenerAdapter,
    product_version: str,
) -> dict[str, object]:
    resolved_paths = paths
    native_adapter = adapter
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
    if settled:
        try:
            _remove_state_root(resolved_paths.state_dir)
        except Exception as exc:
            cleanup_errors.append(_safe_diagnostic("remove listener state", exc))
    if cleanup_errors:
        detail = _combine_diagnostics(*cleanup_errors)
        assert detail is not None
        raise ListenerError(f"Gogurt listener uninstall failed: {detail}")
    return listener_status(
        paths=resolved_paths,
        adapter=native_adapter,
        product_version=product_version,
    )
