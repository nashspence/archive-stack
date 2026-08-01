from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any, Protocol

cleanup_stop = threading.Event()


cleanup_thread: threading.Thread | None = None


handoff_stop = threading.Event()


handoff_thread: threading.Thread | None = None


class TaskScheduler(Protocol):
    def add_task(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> None: ...


state_lock = threading.RLock()


job_state_lock = threading.RLock()


active_jobs: set[str] = set()


scheduled_jobs: set[str] = set()


shared_input_tree_locks: dict[str, threading.Lock] = {}


shared_input_tree_locks_guard = threading.Lock()


input_file_upload_setup_locks: dict[tuple[str, str], threading.Lock] = {}


input_file_upload_setup_locks_guard = threading.Lock()


input_upload_state_locks: dict[str, threading.RLock] = {}


input_upload_state_locks_guard = threading.Lock()


def shared_input_tree_lock(upload_id: str) -> threading.Lock:
    with shared_input_tree_locks_guard:
        lock = shared_input_tree_locks.get(upload_id)
        if lock is None:
            lock = threading.Lock()
            shared_input_tree_locks[upload_id] = lock
        return lock


def input_file_upload_setup_lock(upload_id: str, rel_path: str) -> threading.Lock:
    key = (upload_id, rel_path)
    with input_file_upload_setup_locks_guard:
        lock = input_file_upload_setup_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            input_file_upload_setup_locks[key] = lock
        return lock


def input_upload_state_lock(upload_id: str) -> threading.RLock:
    with input_upload_state_locks_guard:
        lock = input_upload_state_locks.get(upload_id)
        if lock is None:
            lock = threading.RLock()
            input_upload_state_locks[upload_id] = lock
        return lock
