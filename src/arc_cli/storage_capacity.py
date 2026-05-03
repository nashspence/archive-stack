from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from contracts.operator import copy as operator_copy

_SUMMARY_FLAG = "ARC_LOCAL_STORAGE_SUMMARY"
_AVAILABLE_BYTES_ENV = "ARC_LOCAL_STORAGE_AVAILABLE_BYTES"
_BUDGET_BYTES_ENV = "ARC_LOCAL_STORAGE_BUDGET_BYTES"
_REQUIRED_BYTES_ENV = "ARC_LOCAL_STORAGE_REQUIRED_BYTES"
_WORKFLOW_ENV = "ARC_LOCAL_STORAGE_WORKFLOW"
_PATH_ENV = "ARC_LOCAL_STORAGE_PATH"


@dataclass(frozen=True, slots=True)
class LocalStorageCapacity:
    available_bytes: int | None
    budget_bytes: int | None
    required_bytes: int | None
    workflow: str


class LocalStorageCapacityBlocked(RuntimeError):
    def __init__(self, capacity: LocalStorageCapacity) -> None:
        super().__init__(storage_capacity_blocked_copy(capacity))
        self.capacity = capacity
        self.copy_text = storage_capacity_blocked_copy(capacity)


def _optional_int_env(name: str) -> int | None:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return None
    return int(value)


def _storage_path() -> Path:
    configured = os.getenv(_PATH_ENV)
    return Path(configured) if configured else Path.cwd()


def _measured_available_bytes() -> int | None:
    try:
        return shutil.disk_usage(_storage_path()).free
    except OSError:
        return None


def local_storage_capacity() -> LocalStorageCapacity:
    available_bytes = _optional_int_env(_AVAILABLE_BYTES_ENV)
    if available_bytes is None:
        available_bytes = _measured_available_bytes()
    return LocalStorageCapacity(
        available_bytes=available_bytes,
        budget_bytes=_optional_int_env(_BUDGET_BYTES_ENV),
        required_bytes=_optional_int_env(_REQUIRED_BYTES_ENV),
        workflow=os.getenv(_WORKFLOW_ENV, "Local work"),
    )


def storage_capacity_summary_requested() -> bool:
    return os.getenv(_SUMMARY_FLAG, "").casefold() in {"1", "true", "yes"}


def storage_capacity_summary_copy(capacity: LocalStorageCapacity) -> str:
    return operator_copy.storage_capacity_summary(
        available_bytes=capacity.available_bytes,
        budget_bytes=capacity.budget_bytes,
    )


def storage_capacity_blocked_copy(capacity: LocalStorageCapacity) -> str:
    return operator_copy.storage_capacity_blocked(
        workflow=capacity.workflow,
        required_bytes=capacity.required_bytes,
        available_bytes=capacity.available_bytes,
    )


def check_local_storage_capacity() -> None:
    capacity = local_storage_capacity()
    if capacity.required_bytes is None or capacity.available_bytes is None:
        return
    if capacity.available_bytes < capacity.required_bytes:
        raise LocalStorageCapacityBlocked(capacity)
