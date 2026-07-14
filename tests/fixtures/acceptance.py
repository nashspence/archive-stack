from __future__ import annotations

import base64
import hashlib
import inspect
import json
import math
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections import defaultdict
from collections.abc import AsyncIterator, Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote, unquote, urlsplit

import httpx
import pytest
import uvicorn
import yaml
from fastapi import Request, Response
from fastapi.responses import JSONResponse

from riverhog_api.app import create_app
from riverhog_api.deps import ServiceContainer
from riverhog_core.domain.enums import (
    ArchiveRestoreState,
    ArchiveState,
    CoverageState,
    DiscState,
    FetchState,
    VerificationState,
)
from riverhog_core.domain.errors import BadRequest, Conflict, HashMismatch, InvalidState, NotFound
from riverhog_core.domain.models import (
    ArchiveCollectionContribution,
    ArchiveRestoreCollection,
    ArchiveRestoreImage,
    ArchiveRestoreListPage,
    ArchiveRestoreNotificationStatus,
    ArchiveRestoreProgress,
    ArchiveRestoreSummary,
    ArchiveStatus,
    ArchiveUsageCollection,
    ArchiveUsageImage,
    ArchiveUsageReport,
    ArchiveUsageSnapshot,
    ArchiveUsageTotals,
    CollectionCoverageImage,
    CollectionListPage,
    CollectionManifestStatus,
    CollectionSummary,
    Coverage,
    DiscHistoryEntry,
    DiscSummary,
    FetchDiscHint,
    FetchListPage,
    FetchSummary,
)
from riverhog_core.domain.selectors import parse_target
from riverhog_core.domain.types import (
    CollectionId,
    DiscId,
    EntryId,
    FetchId,
    ImageId,
    Sha256Hex,
    TargetStr,
)
from riverhog_core.durability import (
    coverage_state,
    disc_counts_as_verified,
    disc_counts_toward_redundancy,
    disc_redundancy_state,
    normalize_disc_state,
    normalize_required_disc_count,
    registered_disc_shortfall,
)
from riverhog_core.fs_paths import (
    find_collection_id_conflict,
    normalize_collection_id,
    normalize_relpath,
    normalize_upload_slug,
    normalize_upload_timestamp,
)
from riverhog_core.iso.streaming import IsoStream, build_iso_cmd_from_root
from riverhog_core.planner.manifest import MANIFEST_FILENAME
from riverhog_core.webhooks import (
    WebhookConfig,
    build_archive_restore_paused_reminder_payload,
    build_archive_restore_ready_payload,
    build_fetch_queued_payload,
)
from tests.fixtures.data import (
    DEFAULT_DISC_CREATED_AT,
    DOCS_COLLECTION_ID,
    DOCS_FILES,
    IMAGE_FIXTURES,
    IMAGE_ID,
    MIN_FILL_BYTES,
    PHOTOS_2024_FILES,
    PHOTOS_COLLECTION_ID,
    PHOTOS_NESTED_COLLECTION_ID,
    PHOTOS_PARENT_COLLECTION_ID,
    SPLIT_DISC_ONE_ID,
    SPLIT_DISC_ONE_LOCATION,
    SPLIT_DISC_TWO_ID,
    SPLIT_DISC_TWO_LOCATION,
    SPLIT_FILE_RELPATH,
    SPLIT_IMAGE_FIXTURES,
    TARGET_BYTES,
    build_file_disc,
    fixture_decrypt_bytes,
    fixture_encrypt_bytes,
    split_fixture_plaintext,
    write_tree,
)
from tests.fixtures.disc_contracts import InspectedIso, inspect_fixture_image_root
from tests.timing_profile import time_block

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
FIXTURE_UPLOAD_EXPIRES_AT = "2099-12-31T23:59:59Z"
_UPLOAD_EXPIRY_SWEEP_INTERVAL_SECONDS = 0.05
_ARCHIVE_UPLOAD_SWEEP_INTERVAL_SECONDS = 1.0
_ARCHIVE_RESTORE_SWEEP_INTERVAL_SECONDS = 0.05
_ARCHIVE_RESTORE_LATENCY_SECONDS = 0.2
_ARCHIVE_RESTORE_READY_TTL_SECONDS = 4.0
_OPERATOR_WEBHOOK_RETRY_DELAY_SECONDS = 1.0
_OPERATOR_WEBHOOK_REMINDER_INTERVAL_SECONDS = 2.0
_CLI_SUBPROCESS_TIMEOUT_SECONDS = 30.0
_CLI_TIMEOUT_OUTPUT_CHARS = 4_000
_DISC_SORT_FIELDS = {"disc_id", "image_id", "state", "verification_state", "location"}


def _generated_disc_id(image_id: str, ordinal: int) -> str:
    return f"{image_id}-{ordinal}"


def _fixture_archive_prefix(collection_id: str) -> str:
    digest = hashlib.sha256(collection_id.encode("utf-8")).hexdigest()[:32]
    return f"archive/archives/{digest}"


def _disc_counts_toward_slot_pool(state: DiscState) -> bool:
    return state in {DiscState.NEEDED, DiscState.BURNING} or disc_counts_toward_redundancy(
        state.value
    )


def _recovery_coverage_state(
    *,
    covered_bytes: int,
    total_bytes: int,
) -> CoverageState:
    if total_bytes <= 0 or covered_bytes <= 0:
        return CoverageState.NONE
    if covered_bytes >= total_bytes:
        return CoverageState.FULL
    return CoverageState.PARTIAL


def _history_event_name(
    *,
    location_changed: bool,
    state_changed: bool,
    verification_changed: bool,
) -> str:
    changed = sum(int(flag) for flag in (location_changed, state_changed, verification_changed))
    if changed > 1:
        return "updated"
    if location_changed:
        return "location_updated"
    if state_changed:
        return "state_updated"
    if verification_changed:
        return "verification_updated"
    return "updated"


def _disc_history(
    *,
    at: str,
    event: str,
    state: DiscState,
    verification_state,
    location: str | None,
) -> tuple[DiscHistoryEntry, ...]:
    return (
        DiscHistoryEntry(
            at=at,
            event=event,
            state=state,
            verification_state=verification_state,
            location=location,
        ),
    )


@dataclass(frozen=True, slots=True)
class FileDisc:
    disc_id: DiscId
    image_id: str
    location: str
    disc_path: str
    enc: dict[str, object]
    part_index: int | None = None
    part_count: int | None = None
    part_bytes: int | None = None
    part_sha256: str | None = None

    @property
    def hint(self) -> FetchDiscHint:
        return FetchDiscHint(
            disc_id=self.disc_id,
            image_id=self.image_id,
            location=self.location,
        )


@dataclass(slots=True)
class StoredFile:
    collection_id: CollectionId
    path: str
    content: bytes
    hot: bool
    hot_backing_missing: bool = False
    discs: list[FileDisc] = field(default_factory=list)

    @property
    def bytes(self) -> int:
        return len(self.content)

    @property
    def sha256(self) -> Sha256Hex:
        return cast(Sha256Hex, hashlib.sha256(self.content).hexdigest())

    @property
    def disc_coverage(self) -> bool:
        return bool(self.discs)

    @property
    def projected_target(self) -> str:
        return f"{self.collection_id}/{self.path}"


@dataclass(frozen=True, slots=True)
class CandidateRecord:
    candidate_id: ImageId
    finalized_id: str
    filename: str
    image_root: Path
    bytes: int
    iso_ready: bool
    covered_paths: tuple[tuple[CollectionId, str], ...]

    @property
    def files(self) -> int:
        return len(self.covered_paths)

    @property
    def collections(self) -> list[str]:
        return sorted({str(collection_id) for collection_id, _ in self.covered_paths})

    @property
    def projected_paths(self) -> list[str]:
        return sorted(f"{collection_id}/{path}" for collection_id, path in self.covered_paths)

    @property
    def fill(self) -> float:
        return self.bytes / TARGET_BYTES

    @property
    def finalized_at(self) -> str:
        return (
            f"{self.finalized_id[0:4]}-{self.finalized_id[4:6]}-{self.finalized_id[6:8]}"
            f"T{self.finalized_id[9:11]}:{self.finalized_id[11:13]}:{self.finalized_id[13:15]}Z"
        )

    def plan_payload(self) -> dict[str, object]:
        return {
            "candidate_id": str(self.candidate_id),
            "bytes": self.bytes,
            "target_bytes": TARGET_BYTES,
            "fill": self.fill,
            "files": self.files,
            "collections": len(self.collections),
            "collection_ids": self.collections,
            "iso_ready": self.iso_ready,
        }

    def finalized_image_payload(
        self,
        *,
        discs_registered: int = 0,
        discs_verified: int = 0,
    ) -> dict[str, object]:
        required_disc_count = normalize_required_disc_count(None)
        redundancy_state = disc_redundancy_state(
            required_disc_count=required_disc_count,
            registered_disc_count=discs_registered,
        )
        return {
            "id": self.finalized_id,
            "filename": self.filename,
            "finalized_at": self.finalized_at,
            "bytes": self.bytes,
            "target_bytes": TARGET_BYTES,
            "fill": self.fill,
            "files": self.files,
            "collections": len(self.collections),
            "collection_ids": self.collections,
            "iso_ready": True,
            "disc_redundancy_state": redundancy_state.value,
            "discs_required": required_disc_count,
            "discs_registered": discs_registered,
            "discs_verified": discs_verified,
            "discs_missing": registered_disc_shortfall(
                required_disc_count=required_disc_count,
                registered_disc_count=discs_registered,
            ),
        }


@dataclass(frozen=True, slots=True)
class _RecoveryParts:
    part_count: int
    present_parts: frozenset[int]


@dataclass(slots=True)
class FetchEntryRecord:
    id: EntryId
    collection_id: CollectionId
    path: str
    bytes: int
    sha256: Sha256Hex
    content: bytes
    discs: list[FileDisc]
    uploaded_bytes: int = 0
    uploaded_content: bytes | None = None
    upload_expires_at: str | None = None
    upload_url: str | None = None


@dataclass(slots=True)
class FetchRecord:
    summary: FetchSummary
    entries: dict[EntryId, FetchEntryRecord]
    notification_sent_at: str | None = None
    notification_next_attempt_at: str | None = None
    notification_count: int = 0
    notification_failure: str | None = None


@dataclass(slots=True)
class CollectionUploadFileRecord:
    path: str
    bytes: int
    sha256: Sha256Hex
    uploaded_bytes: int = 0
    uploaded_content: bytes | None = None
    upload_expires_at: str | None = None
    upload_url: str | None = None


@dataclass(slots=True)
class CollectionUploadRecord:
    collection_id: CollectionId
    ingest_source: str | None
    files: dict[str, CollectionUploadFileRecord]
    notify: dict[str, object] | None = None
    state: str = "uploading"
    archive_attempt_count: int = 0
    latest_failure: str | None = None


@dataclass(slots=True)
class AcceptanceArchiveRestoreRecord:
    restore_id: str
    image_id: ImageId
    state: ArchiveRestoreState
    created_at: str
    type: str = "disc_rebuild"
    image_ids: tuple[ImageId, ...] = ()
    collection_ids: tuple[CollectionId, ...] = ()
    paths: tuple[str, ...] | None = None
    requested_at: str | None = None
    ready_at: str | None = None
    next_poll_at: str | None = None
    expires_at: str | None = None
    completed_at: str | None = None
    canceled_at: str | None = None
    paused_at: str | None = None
    paused_from_state: str | None = None
    latest_message: str | None = None
    reminder_count: int = 0
    next_reminder_at: str | None = None
    last_notified_at: str | None = None
    failure_count: int = 0
    last_failure_at: str | None = None
    last_failure: str | None = None
    archive_verification_state: str = "pending"
    extraction_state: str = "pending"
    materialization_state: str = "pending"


@dataclass(slots=True)
class AcceptanceState:
    local_collection_sources: dict[CollectionId, Path] = field(default_factory=dict)
    files_by_collection: dict[CollectionId, dict[str, StoredFile]] = field(default_factory=dict)
    candidates_by_id: dict[ImageId, CandidateRecord] = field(default_factory=dict)
    finalized_images_by_id: dict[ImageId, CandidateRecord] = field(default_factory=dict)
    disc_summaries: dict[tuple[str, DiscId], DiscSummary] = field(default_factory=dict)
    fetches: dict[FetchId, FetchRecord] = field(default_factory=dict)
    collection_uploads: dict[CollectionId, CollectionUploadRecord] = field(default_factory=dict)
    collection_archive_status_by_collection: dict[CollectionId, ArchiveStatus] = field(
        default_factory=dict
    )
    archive_usage_snapshots: list[ArchiveUsageSnapshot] = field(default_factory=list)
    archive_restores_by_id: dict[str, AcceptanceArchiveRestoreRecord] = field(default_factory=dict)
    archive_upload_failures_by_collection: dict[CollectionId, str] = field(default_factory=dict)
    archive_upload_defer_until_by_collection: dict[CollectionId, float] = field(
        default_factory=dict
    )
    webhook_deliveries: list[dict[str, object]] = field(default_factory=list)
    webhook_attempts: list[dict[str, object]] = field(default_factory=list)
    webhook_behaviors: list[dict[str, object]] = field(default_factory=list)
    lock: Any = field(default_factory=threading.RLock, repr=False)
    public_base_url: str = ""
    real_iso_streams_enabled: bool = False
    next_fetch_number: int = 0

    def clear_webhook_deliveries(self) -> None:
        with self.lock:
            self.webhook_deliveries.clear()
            self.webhook_attempts.clear()
            self.webhook_behaviors.clear()

    def record_webhook_delivery(self, payload: dict[str, object]) -> None:
        normalized = json.loads(json.dumps(payload, sort_keys=True))
        assert isinstance(normalized, dict)
        with self.lock:
            self.webhook_deliveries.append(normalized)

    def record_webhook_attempt(self, payload: dict[str, object]) -> None:
        normalized = json.loads(json.dumps(payload, sort_keys=True))
        assert isinstance(normalized, dict)
        with self.lock:
            self.webhook_attempts.append(normalized)

    def list_webhook_deliveries(self) -> list[dict[str, object]]:
        with self.lock:
            return [
                cast(dict[str, object], json.loads(json.dumps(item)))
                for item in self.webhook_deliveries
            ]

    def list_webhook_attempts(self) -> list[dict[str, object]]:
        with self.lock:
            return [
                cast(dict[str, object], json.loads(json.dumps(item)))
                for item in self.webhook_attempts
            ]

    def add_webhook_behavior(
        self,
        *,
        event: str,
        status_code: int = 503,
        remaining: int = 1,
        delay_seconds: float = 0.0,
        mode: str = "status",
    ) -> None:
        with self.lock:
            self.webhook_behaviors.append(
                {
                    "event": event,
                    "mode": mode,
                    "status_code": status_code,
                    "remaining": max(1, remaining),
                    "delay_seconds": max(0.0, delay_seconds),
                }
            )

    def _consume_webhook_behavior(self, event: str) -> dict[str, object] | None:
        with self.lock:
            for behavior in self.webhook_behaviors:
                if str(behavior.get("event", "")).strip() != event:
                    continue
                remaining = int(behavior.get("remaining", 0))
                if remaining <= 0:
                    continue
                behavior["remaining"] = remaining - 1
                return cast(dict[str, object], json.loads(json.dumps(behavior)))
            return None

    def deliver_webhook_payload(
        self,
        payload: dict[str, object],
        *,
        delivered_at: datetime | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        with self.lock:
            event = str(payload.get("event", "")).strip()
            behavior = self._consume_webhook_behavior(event)
            attempt_payload: dict[str, object] = {
                "event": event,
                "payload": payload,
                "received_at": _acceptance_isoformat(delivered_at or datetime.now(UTC)),
                "result": "delivered",
                "status_code": 204,
            }
            if behavior is not None:
                attempt_payload["behavior"] = behavior
                mode = str(behavior.get("mode", "status")).strip() or "status"
                if mode == "timeout":
                    attempt_payload["result"] = "timeout"
                    attempt_payload["status_code"] = 0
                    self.record_webhook_attempt(attempt_payload)
                    raise httpx.ReadTimeout(f"test webhook sink timed out after {timeout_seconds}s")
                status_code = int(behavior.get("status_code", 503))
                if status_code >= 400:
                    attempt_payload["result"] = "failed"
                    attempt_payload["status_code"] = status_code
                    self.record_webhook_attempt(attempt_payload)
                    raise RuntimeError(f"test webhook sink returned HTTP {status_code}")
            self.record_webhook_attempt(attempt_payload)
            self.record_webhook_delivery(payload)

    def webhook_config(self) -> WebhookConfig:
        return WebhookConfig(
            url="",
            base_url=self.public_base_url,
            retry_seconds=_OPERATOR_WEBHOOK_RETRY_DELAY_SECONDS,
            reminder_interval_seconds=_OPERATOR_WEBHOOK_REMINDER_INTERVAL_SECONDS,
        )

    def register_local_collection_source(self, collection_id: str, root: Path) -> None:
        with self.lock:
            self.local_collection_sources[CollectionId(collection_id)] = root

    def seed_collection(
        self,
        collection_id: str,
        files: Mapping[str, bytes],
        *,
        hot_paths: set[str],
    ) -> None:
        with self.lock:
            normalized_collection_id = normalize_collection_id(collection_id)
            conflict = find_collection_id_conflict(
                (str(current) for current in self.files_by_collection), normalized_collection_id
            )
            if (
                CollectionId(normalized_collection_id) not in self.files_by_collection
                and conflict is not None
            ):
                raise Conflict(f"collection id conflicts with existing collection: {conflict}")
            collection_key = CollectionId(normalized_collection_id)
            records: dict[str, StoredFile] = {}
            for relative_path, content in sorted(files.items()):
                normalized = relative_path.lstrip("/")
                records[normalized] = StoredFile(
                    collection_id=collection_key,
                    path=normalized,
                    content=content,
                    hot=normalized in hot_paths,
                )
            self.files_by_collection[collection_key] = records

    def seed_image(self, image: CandidateRecord) -> None:
        with self.lock:
            self.candidates_by_id[image.candidate_id] = image

    def collection_archive_status(self, collection_id: str) -> ArchiveStatus:
        return self.collection_archive_status_by_collection.get(
            CollectionId(normalize_collection_id(collection_id)),
            ArchiveStatus(state=ArchiveState.PENDING),
        )

    def mark_collection_archive_uploaded(self, collection_id: str) -> None:
        normalized_collection_id = normalize_collection_id(collection_id)
        records = self.collection_files(normalized_collection_id)
        now = DEFAULT_DISC_CREATED_AT
        self.collection_archive_status_by_collection[CollectionId(normalized_collection_id)] = (
            ArchiveStatus(
                state=ArchiveState.UPLOADED,
                object_path=f"{_fixture_archive_prefix(normalized_collection_id)}/archive.tar.age",
                stored_bytes=sum(record.bytes for record in records),
                backend="s3",
                storage_class="DEEP_ARCHIVE",
                last_uploaded_at=now,
                last_verified_at=now,
                failure=None,
            )
        )

    def ensure_archive_restore(self, image_id: str) -> None:
        image = self.finalized_images_by_id.get(ImageId(image_id))
        if image is None:
            return
        required_collection_ids = tuple(
            sorted({collection_id for collection_id, _ in image.covered_paths})
        )
        if not required_collection_ids or any(
            self.collection_archive_status(collection_id).state != ArchiveState.UPLOADED
            for collection_id in required_collection_ids
        ):
            return
        if self._disc_redundancy_count(image_id) > 0:
            return
        if not self._has_recovery_triggering_disc_history(image_id):
            return
        if self.active_archive_restore(image_id) is not None:
            return
        pending_rebuild = self.active_disc_rebuild_restore()
        if pending_rebuild is not None and pending_rebuild.requested_at is None:
            image_key = ImageId(image_id)
            if image_key not in pending_rebuild.image_ids:
                pending_rebuild.image_ids = tuple(sorted((*pending_rebuild.image_ids, image_key)))
            pending_rebuild.collection_ids = tuple(
                sorted({*pending_rebuild.collection_ids, *required_collection_ids})
            )
            return
        restore_id = self._generated_archive_restore_id(image_id)
        image_key = ImageId(image_id)
        self.archive_restores_by_id[restore_id] = AcceptanceArchiveRestoreRecord(
            restore_id=restore_id,
            image_id=image_key,
            state=ArchiveRestoreState.REQUESTED,
            created_at=_acceptance_isoformat(datetime.now(UTC)),
            type="disc_rebuild",
            image_ids=(image_key,),
            collection_ids=required_collection_ids,
            latest_message=(
                "Archive restore queued; Riverhog will restore collection data from the archive."
            ),
        )

    def active_disc_rebuild_restore(self) -> AcceptanceArchiveRestoreRecord | None:
        active_states = {
            ArchiveRestoreState.REQUESTED,
            ArchiveRestoreState.READY,
            ArchiveRestoreState.PAUSED,
        }
        restores = [
            record
            for record in self.archive_restores_by_id.values()
            if record.type == "disc_rebuild" and record.state in active_states
        ]
        if not restores:
            return None
        return sorted(restores, key=lambda current: current.created_at, reverse=True)[0]

    def active_archive_restore(self, image_id: str) -> AcceptanceArchiveRestoreRecord | None:
        active_states = {
            ArchiveRestoreState.REQUESTED,
            ArchiveRestoreState.READY,
            ArchiveRestoreState.PAUSED,
        }
        restores = [
            record
            for record in self.archive_restores_by_id.values()
            if ImageId(image_id) in self._record_image_ids(record) and record.state in active_states
        ]
        if not restores:
            return None
        return sorted(restores, key=lambda current: current.created_at, reverse=True)[0]

    def latest_archive_restore(self, image_id: str) -> AcceptanceArchiveRestoreRecord | None:
        restores = [
            record
            for record in self.archive_restores_by_id.values()
            if ImageId(image_id) in self._record_image_ids(record)
        ]
        if not restores:
            return None
        return sorted(restores, key=lambda current: current.created_at, reverse=True)[0]

    def _generated_archive_restore_id(self, image_id: str) -> str:
        existing_ids = {
            record.restore_id
            for record in self.archive_restores_by_id.values()
            if str(record.image_id) == image_id
        }
        ordinal = 1
        while True:
            candidate = f"ar-{image_id}-rebuild-{ordinal}"
            ordinal += 1
            if candidate not in existing_ids:
                return candidate

    @staticmethod
    def _record_image_ids(record: AcceptanceArchiveRestoreRecord) -> tuple[ImageId, ...]:
        return record.image_ids or ((record.image_id,) if str(record.image_id) else ())

    def _disc_redundancy_count(self, image_id: str) -> int:
        return sum(
            1
            for (scoped_image_id, _disc_id), summary in self.disc_summaries.items()
            if scoped_image_id == image_id and disc_counts_toward_redundancy(summary.state.value)
        )

    def _has_recovery_triggering_disc_history(self, image_id: str) -> bool:
        return any(
            scoped_image_id == image_id
            and normalize_disc_state(summary.state.value)
            not in {DiscState.NEEDED, DiscState.BURNING}
            for (scoped_image_id, _disc_id), summary in self.disc_summaries.items()
        )

    def ensure_required_disc_slots(self, image_id: str) -> None:
        image = self.finalized_images_by_id.get(ImageId(image_id))
        if image is None:
            raise NotFound(f"image not found: {image_id}")
        required_disc_count = normalize_required_disc_count(None)
        discs = [
            summary
            for (scoped_image_id, _disc_id), summary in sorted(self.disc_summaries.items())
            if scoped_image_id == image_id
        ]
        existing_ids = {
            str(disc_id)
            for scoped_image_id, disc_id in self.disc_summaries
            if scoped_image_id == image_id
        }

        if not discs:
            while len(existing_ids) < required_disc_count:
                self._create_generated_disc_slot(image_id, existing_ids=existing_ids)
            return

        active_slot_count = sum(
            1 for summary in discs if _disc_counts_toward_slot_pool(summary.state)
        )
        disc_redundancy_count = sum(
            1 for summary in discs if disc_counts_toward_redundancy(summary.state.value)
        )
        if disc_redundancy_count > 0:
            while active_slot_count < required_disc_count:
                self._create_generated_disc_slot(image_id, existing_ids=existing_ids)
                active_slot_count += 1

    def _create_generated_disc_slot(self, image_id: str, *, existing_ids: set[str]) -> DiscSummary:
        ordinal = 1
        while True:
            disc_id = _generated_disc_id(image_id, ordinal)
            ordinal += 1
            if disc_id in existing_ids:
                continue
            summary = DiscSummary(
                disc_id=DiscId(disc_id),
                image_id=image_id,
                label_text=disc_id,
                location=None,
                created_at=DEFAULT_DISC_CREATED_AT,
                state=DiscState.NEEDED,
                verification_state=VerificationState.PENDING,
                history=_disc_history(
                    at=DEFAULT_DISC_CREATED_AT,
                    event="created",
                    state=DiscState.NEEDED,
                    verification_state=VerificationState.PENDING,
                    location=None,
                ),
            )
            self.disc_summaries[(image_id, DiscId(disc_id))] = summary
            existing_ids.add(disc_id)
            return summary

    def collection_files(self, collection_id: str | CollectionId) -> list[StoredFile]:
        collection_key = CollectionId(str(collection_id))
        records = self.files_by_collection.get(collection_key)
        if records is None:
            raise NotFound(f"collection not found: {collection_key}")
        return list(records.values())

    def file_content(self, collection_id: str | CollectionId, path: str) -> bytes:
        collection_key = CollectionId(str(collection_id))
        records = self.files_by_collection.get(collection_key)
        if records is None:
            raise NotFound(f"collection not found: {collection_key}")
        record = records.get(path)
        if record is None:
            raise NotFound(f"file not found in {collection_key}: {path}")
        if record.hot_backing_missing:
            raise NotFound(f"file not found in hot store: {collection_key}/{path}")
        return record.content

    def collection_summary(self, collection_id: str | CollectionId) -> CollectionSummary:
        records = self.collection_files(collection_id)
        (
            image_coverage,
            covered_paths,
            recovery_parts_by_image_path,
        ) = self.collection_image_coverage(collection_id)
        disc_redundancy_bytes = 0
        image_states = {str(image.id): image.disc_redundancy_state for image in image_coverage}
        for record in records:
            image_ids = covered_paths.get(record.path, set())
            if image_ids and all(
                image_states.get(image_id) == CoverageState.FULL for image_id in image_ids
            ):
                disc_redundancy_bytes += record.bytes
        disc_coverage = self.collection_disc_coverage(
            records,
            image_coverage=image_coverage,
            covered_paths=covered_paths,
            recovery_parts_by_image_path=recovery_parts_by_image_path,
        )
        return CollectionSummary(
            id=CollectionId(str(collection_id)),
            files=len(records),
            bytes=sum(record.bytes for record in records),
            hot_bytes=sum(record.bytes for record in records if record.hot),
            disc_coverage=disc_coverage,
            disc_redundancy=Coverage(
                state=coverage_state(
                    total_bytes=sum(record.bytes for record in records),
                    covered_bytes=disc_redundancy_bytes,
                ),
                bytes=disc_redundancy_bytes,
            ),
            image_coverage=image_coverage,
            archive=self._collection_archive_status(str(collection_id), records),
            collection_manifest=self._collection_manifest_status(str(collection_id)),
            archive_format="tar",
            compression="none",
        )

    def _collection_archive_status(
        self,
        collection_id: str,
        records: list[StoredFile],
    ) -> ArchiveStatus:
        _ = records
        direct = self.collection_archive_status(collection_id)
        return direct

    def _collection_manifest_status(
        self,
        collection_id: str,
    ) -> CollectionManifestStatus | None:
        status = self._collection_archive_status(
            collection_id,
            self.collection_files(collection_id),
        )
        if status.state != ArchiveState.UPLOADED:
            return None
        return CollectionManifestStatus(
            object_path=f"{_fixture_archive_prefix(collection_id)}/manifest.yml.age",
            sha256="0" * 64,
            ots_object_path=f"{_fixture_archive_prefix(collection_id)}/manifest.yml.ots.age",
            ots_state="uploaded",
            ots_sha256="1" * 64,
        )

    def collection_image_coverage(
        self, collection_id: str | CollectionId
    ) -> tuple[
        list[CollectionCoverageImage],
        dict[str, set[str]],
        dict[tuple[str, str], _RecoveryParts],
    ]:
        normalized_collection_id = CollectionId(str(collection_id))
        covered_paths: dict[str, set[str]] = {}
        recovery_parts_by_image_path: dict[tuple[str, str], _RecoveryParts] = {}
        image_coverage: list[CollectionCoverageImage] = []
        for image in sorted(
            self.finalized_images_by_id.values(),
            key=lambda current: current.finalized_id,
        ):
            if normalized_collection_id not in {
                collection_id for collection_id, _ in image.covered_paths
            }:
                continue
            manifest_entries = _acceptance_manifest_entries_for_collection(
                image,
                normalized_collection_id,
            )
            for covered_collection_id, path in image.covered_paths:
                if covered_collection_id != normalized_collection_id:
                    continue
                covered_paths.setdefault(path, set()).add(image.finalized_id)
                recovery_parts = manifest_entries.get(path)
                if recovery_parts is not None:
                    recovery_parts_by_image_path[(image.finalized_id, path)] = recovery_parts
            discs = [
                summary
                for (image_id, _disc_id), summary in sorted(self.disc_summaries.items())
                if image_id == image.finalized_id
            ]
            discs_registered = sum(
                1 for disc in discs if disc_counts_toward_redundancy(disc.state.value)
            )
            discs_verified = sum(
                1
                for disc in discs
                if disc_counts_as_verified(
                    state=disc.state.value,
                    verification_state=disc.verification_state.value,
                )
            )
            discs_required = normalize_required_disc_count(None)
            image_coverage.append(
                CollectionCoverageImage(
                    id=ImageId(image.finalized_id),
                    filename=image.filename,
                    disc_redundancy_state=disc_redundancy_state(
                        required_disc_count=discs_required,
                        registered_disc_count=discs_registered,
                    ),
                    discs_required=discs_required,
                    discs_registered=discs_registered,
                    discs_verified=discs_verified,
                    discs_missing=registered_disc_shortfall(
                        required_disc_count=discs_required,
                        registered_disc_count=discs_registered,
                    ),
                    covered_paths=sorted(
                        path
                        for covered_collection_id, path in image.covered_paths
                        if covered_collection_id == normalized_collection_id
                    ),
                    discs=discs,
                )
            )
        return image_coverage, covered_paths, recovery_parts_by_image_path

    def collection_disc_coverage(
        self,
        records: list[StoredFile],
        *,
        image_coverage: Sequence[CollectionCoverageImage],
        covered_paths: dict[str, set[str]],
        recovery_parts_by_image_path: dict[tuple[str, str], _RecoveryParts],
    ) -> Coverage:
        total_bytes = sum(record.bytes for record in records)
        image_by_id = {str(image.id): image for image in image_coverage}
        verified_disc_available_bytes = 0

        for record in records:
            image_ids = covered_paths.get(record.path, set())
            if _path_is_recoverable(
                record.path,
                image_ids=image_ids,
                recovery_parts_by_image_path=recovery_parts_by_image_path,
                image_available=lambda image: image.discs_registered > 0,
                image_by_id=image_by_id,
            ):
                verified_disc_available_bytes += record.bytes
            else:
                verified_disc_available_bytes += _partial_recoverable_bytes(
                    record.bytes,
                    image_ids=image_ids,
                    recovery_parts_by_image_path=recovery_parts_by_image_path,
                    image_available=lambda image: image.discs_registered > 0,
                    image_by_id=image_by_id,
                )
        state = coverage_state(
            covered_bytes=verified_disc_available_bytes,
            total_bytes=total_bytes,
        )
        return Coverage(state=state, bytes=verified_disc_available_bytes)

    def selected_files(self, raw_target: str, *, missing_ok: bool = False) -> list[StoredFile]:
        target = parse_target(raw_target)
        selected = [
            record
            for collection_files in self.files_by_collection.values()
            for record in collection_files.values()
            if (
                record.projected_target.startswith(target.canonical)
                if target.is_dir
                else record.projected_target == target.canonical
            )
        ]
        if not selected and not missing_ok:
            raise NotFound(f"target not found: {raw_target}")
        return selected

    def selected_bytes(self, raw_target: str) -> int:
        return sum(record.bytes for record in self.selected_files(raw_target))

    def is_hot(self, raw_target: str) -> bool:
        selected = self.selected_files(raw_target, missing_ok=True)
        return bool(selected) and all(record.hot for record in selected)

    def file_is_fully_compliant(self, record: StoredFile) -> bool:
        covering_images = [
            image
            for image in self.finalized_images_by_id.values()
            if (record.collection_id, record.path) in image.covered_paths
        ]
        if not covering_images:
            return False
        required = normalize_required_disc_count(None)
        return all(
            sum(
                1
                for (image_id, _disc_id), summary in self.disc_summaries.items()
                if image_id == image.finalized_id
                and disc_counts_as_verified(
                    state=summary.state.value,
                    verification_state=summary.verification_state.value,
                )
            )
            >= required
            for image in covering_images
        )

    @staticmethod
    def _record_matches_target(record: StoredFile, raw_target: str) -> bool:
        target = parse_target(raw_target)
        return (
            record.projected_target.startswith(target.canonical)
            if target.is_dir
            else record.projected_target == target.canonical
        )

    def reserve_fetch_id(self, fetch_id: str) -> None:
        if fetch_id.startswith("fx-"):
            suffix = fetch_id.removeprefix("fx-")
            if suffix.isdigit():
                self.next_fetch_number = max(self.next_fetch_number, int(suffix))

    @staticmethod
    def _disc_from_dict(item: dict[str, object]) -> FileDisc:
        return FileDisc(
            disc_id=DiscId(str(item["disc_id"])),
            image_id=str(item["image_id"]),
            location=str(item["location"]),
            disc_path=str(item["disc_path"]),
            enc=cast(dict[str, object], item["enc"]),
            part_index=cast(int | None, item.get("part_index")),
            part_count=cast(int | None, item.get("part_count")),
            part_bytes=cast(int | None, item.get("part_bytes")),
            part_sha256=cast(str | None, item.get("part_sha256")),
        )


def _with_state_lock(method: Callable[..., Any]) -> Callable[..., Any]:
    if inspect.iscoroutinefunction(method):

        @wraps(method)
        async def async_wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
            with self.state.lock:
                return await method(self, *args, **kwargs)

        async_wrapper.__acceptance_state_locked__ = True
        return async_wrapper

    @wraps(method)
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        with self.state.lock:
            return method(self, *args, **kwargs)

    wrapper.__acceptance_state_locked__ = True
    return wrapper


class AcceptanceCollectionService:
    def __init__(self, state: AcceptanceState) -> None:
        self.state = state

    @_with_state_lock
    def create_or_resume_upload(
        self,
        *,
        upload_slug: str,
        files: list[dict[str, object]],
        ingest_source: str | None = None,
        upload_timestamp: str | None = None,
        notify: dict[str, object] | None = None,
    ) -> dict[str, object]:
        normalized_slug = normalize_upload_slug(upload_slug)
        normalized_upload_timestamp = (
            normalize_upload_timestamp(upload_timestamp) if upload_timestamp is not None else None
        )
        requested_collection_id = (
            self._collection_id_for_upload_timestamp(
                normalized_slug,
                normalized_upload_timestamp,
            )
            if normalized_upload_timestamp is not None
            else None
        )
        normalized_files = self._normalize_files(files)
        collection = self._matching_collection(normalized_slug, normalized_files)
        if collection is not None:
            self._ensure_requested_collection_id_matches(collection, requested_collection_id)
            return self._finalized_upload_payload(collection)

        upload = self._matching_upload(normalized_slug, normalized_files)
        if upload is not None:
            upload = self._expire_upload(upload)

        if upload is None:
            normalized_collection_id = requested_collection_id or self._mint_collection_id(
                normalized_slug
            )
            collection_key = CollectionId(normalized_collection_id)
            if collection_key in self.state.files_by_collection:
                raise Conflict(f"collection already exists: {normalized_collection_id}")
            if collection_key in self.state.collection_uploads:
                raise Conflict(
                    "collection upload already exists with different manifest: "
                    f"{normalized_collection_id}"
                )
            conflict = find_collection_id_conflict(
                (
                    [
                        *(str(current) for current in self.state.files_by_collection),
                        *(str(current) for current in self.state.collection_uploads),
                    ]
                ),
                normalized_collection_id,
            )
            if conflict is not None:
                raise Conflict(f"collection id conflicts with existing collection: {conflict}")
            upload = CollectionUploadRecord(
                collection_id=collection_key,
                ingest_source=ingest_source,
                files={
                    item["path"]: CollectionUploadFileRecord(
                        path=item["path"],
                        bytes=int(item["bytes"]),
                        sha256=Sha256Hex(str(item["sha256"])),
                    )
                    for item in normalized_files
                },
                notify=notify,
            )
            self.state.collection_uploads[collection_key] = upload
        else:
            self._ensure_requested_collection_id_matches(
                upload.collection_id,
                requested_collection_id,
            )
            upload.ingest_source = ingest_source
            if notify is not None:
                upload.notify = notify
            if upload.state == "failed":
                upload.state = "archiving"
                upload.latest_failure = None

        if self._is_complete(upload):
            if upload.state == "uploading":
                upload.state = "archiving"
            return self._upload_payload(upload, state=upload.state, collection=None)
        return self._upload_payload(upload, state="uploading", collection=None)

    @_with_state_lock
    def create_or_resume_upload_session(
        self,
        *,
        upload_slug: str,
        ingest_source: str | None = None,
        upload_timestamp: str | None = None,
        notify: dict[str, object] | None = None,
    ) -> dict[str, object]:
        normalized_slug = normalize_upload_slug(upload_slug)
        normalized_upload_timestamp = (
            normalize_upload_timestamp(upload_timestamp) if upload_timestamp is not None else None
        )
        requested_collection_id = (
            self._collection_id_for_upload_timestamp(
                normalized_slug,
                normalized_upload_timestamp,
            )
            if normalized_upload_timestamp is not None
            else None
        )
        if requested_collection_id is not None:
            collection_key = CollectionId(requested_collection_id)
            if collection_key in self.state.files_by_collection:
                return self._finalized_upload_payload(collection_key)
            upload = self.state.collection_uploads.get(collection_key)
            if upload is not None:
                if upload.state != "open":
                    raise Conflict(f"collection upload session is {upload.state}: {collection_key}")
                upload.ingest_source = ingest_source
                if notify is not None:
                    upload.notify = notify
                return self._upload_payload(upload, state="open", collection=None)
        else:
            upload = self._open_upload_session_for_slug(normalized_slug)
            if upload is not None:
                upload.ingest_source = ingest_source
                if notify is not None:
                    upload.notify = notify
                return self._upload_payload(upload, state="open", collection=None)

        normalized_collection_id = requested_collection_id or self._mint_collection_id(
            normalized_slug
        )
        collection_key = CollectionId(normalized_collection_id)
        if collection_key in self.state.files_by_collection:
            raise Conflict(f"collection already exists: {normalized_collection_id}")
        if collection_key in self.state.collection_uploads:
            raise Conflict(f"collection upload already exists: {normalized_collection_id}")
        upload = CollectionUploadRecord(
            collection_id=collection_key,
            ingest_source=ingest_source,
            files={},
            notify=notify,
            state="open",
        )
        self.state.collection_uploads[collection_key] = upload
        return self._upload_payload(upload, state="open", collection=None)

    @_with_state_lock
    def register_upload_session_file(
        self,
        collection_id: str,
        file: dict[str, object],
    ) -> dict[str, object]:
        normalized_collection_id = normalize_collection_id(collection_id)
        collection_key = CollectionId(normalized_collection_id)
        upload = self.state.collection_uploads.get(collection_key)
        if upload is None:
            raise NotFound(f"collection upload session not found: {normalized_collection_id}")
        if upload.state != "open":
            raise Conflict(f"collection upload session is not open: {normalized_collection_id}")
        normalized_file = self._normalize_files([file])[0]
        path = str(normalized_file["path"])
        existing = upload.files.get(path)
        if existing is not None:
            if {
                "path": existing.path,
                "bytes": existing.bytes,
                "sha256": str(existing.sha256),
            } != normalized_file:
                raise Conflict(
                    f"collection upload session file already exists with different metadata: {path}"
                )
            return self._upload_payload(upload, state="open", collection=None)
        upload.files[path] = CollectionUploadFileRecord(
            path=path,
            bytes=int(normalized_file["bytes"]),
            sha256=Sha256Hex(str(normalized_file["sha256"])),
        )
        return self._upload_payload(upload, state="open", collection=None)

    @_with_state_lock
    def create_or_resume_registered_file_upload(
        self,
        collection_id: str,
        file: dict[str, object],
    ) -> dict[str, object]:
        normalized_collection_id = normalize_collection_id(collection_id)
        collection_key = CollectionId(normalized_collection_id)
        upload = self.state.collection_uploads.get(collection_key)
        if upload is None:
            raise NotFound(f"collection upload session not found: {normalized_collection_id}")
        if upload.state != "open":
            raise Conflict(f"collection upload session is not open: {normalized_collection_id}")
        normalized_file = self._normalize_files([file])[0]
        path = str(normalized_file["path"])
        file_record = upload.files.get(path)
        if file_record is not None:
            if {
                "path": file_record.path,
                "bytes": file_record.bytes,
                "sha256": str(file_record.sha256),
            } != normalized_file:
                raise Conflict(
                    f"collection upload session file already exists with different metadata: {path}"
                )
        else:
            file_record = CollectionUploadFileRecord(
                path=path,
                bytes=int(normalized_file["bytes"]),
                sha256=Sha256Hex(str(normalized_file["sha256"])),
            )
            upload.files[path] = file_record

        if file_record.upload_url is None:
            file_record.upload_url = (
                f"/files/collections/{quote(normalized_collection_id, safe='')}/"
                f"{quote(path, safe='/')}"
            )
        if self._file_upload_state(file_record) != "uploaded":
            file_record.upload_expires_at = FIXTURE_UPLOAD_EXPIRES_AT

        return {
            "collection_id": str(upload.collection_id),
            "ingest_source": upload.ingest_source,
            "state": upload.state,
            "file": {
                "path": file_record.path,
                "bytes": file_record.bytes,
                "sha256": str(file_record.sha256),
                "upload_state": self._file_upload_state(file_record),
                "uploaded_bytes": file_record.uploaded_bytes,
                "upload_state_expires_at": file_record.upload_expires_at,
            },
            "path": file_record.path,
            "protocol": "tus",
            "upload_url": file_record.upload_url,
            "offset": file_record.uploaded_bytes,
            "length": file_record.bytes,
            "checksum_algorithm": "sha256",
            "expires_at": file_record.upload_expires_at,
        }

    @_with_state_lock
    def sync_finished_upload_target(self, target_path: str) -> dict[str, object] | None:
        normalized = target_path.lstrip("/")
        prefix = ".riverhog/uploads/collections/"
        if not normalized.startswith(prefix):
            return None
        rest = normalized.removeprefix(prefix)
        parts = rest.split("/", 2)
        if len(parts) != 3:
            return None
        normalized_collection_id = normalize_collection_id(f"{parts[0]}/{parts[1]}")
        normalized_path = normalize_relpath(parts[2])
        if normalized != f"{prefix}{normalized_collection_id}/{normalized_path}":
            return None

        upload = self.state.collection_uploads.get(CollectionId(normalized_collection_id))
        if upload is None or upload.state in {"canceled", "expired"}:
            return None
        file_record = upload.files.get(normalized_path)
        if file_record is None:
            return None

        file_record.uploaded_bytes = file_record.bytes
        file_record.upload_expires_at = None

        if upload.state != "open" and upload.state != "archiving" and self._is_complete(upload):
            upload.state = "archiving"

        return {
            "collection_id": str(upload.collection_id),
            "ingest_source": upload.ingest_source,
            "state": upload.state,
            "file": {
                "path": file_record.path,
                "bytes": file_record.bytes,
                "sha256": str(file_record.sha256),
                "upload_state": self._file_upload_state(file_record),
                "uploaded_bytes": file_record.uploaded_bytes,
                "upload_state_expires_at": file_record.upload_expires_at,
            },
        }

    @_with_state_lock
    def complete_upload_session(self, collection_id: str) -> dict[str, object]:
        normalized_collection_id = normalize_collection_id(collection_id)
        collection_key = CollectionId(normalized_collection_id)
        if collection_key in self.state.files_by_collection:
            return self._finalized_upload_payload(collection_key)
        upload = self.state.collection_uploads.get(collection_key)
        if upload is None:
            raise NotFound(f"collection upload session not found: {normalized_collection_id}")
        if upload.state in {"archiving", "failed"}:
            return self._upload_payload(upload, state=upload.state, collection=None)
        if upload.state != "open":
            raise Conflict(f"collection upload session is not open: {normalized_collection_id}")
        if not upload.files:
            raise Conflict("collection upload session cannot complete without files")
        if not self._is_complete(upload):
            raise Conflict("collection upload session still has missing file bytes")
        upload.state = "archiving"
        return self._upload_payload(upload, state="archiving", collection=None)

    @_with_state_lock
    def cancel_upload_session(self, collection_id: str) -> dict[str, object]:
        normalized_collection_id = normalize_collection_id(collection_id)
        collection_key = CollectionId(normalized_collection_id)
        upload = self.state.collection_uploads.get(collection_key)
        if upload is None:
            raise NotFound(f"collection upload session not found: {normalized_collection_id}")
        if upload.state in {"canceled", "expired"}:
            return self._upload_payload(upload, state=upload.state, collection=None)
        if upload.state != "open":
            raise Conflict("collection upload session cannot be canceled after completion handoff")
        upload.files.clear()
        upload.state = "canceled"
        return self._upload_payload(upload, state="canceled", collection=None)

    def _matching_collection(
        self,
        upload_slug: str,
        files: list[dict[str, object]],
    ) -> CollectionId | None:
        for collection_id, records in sorted(self.state.files_by_collection.items()):
            if self._collection_id_upload_slug(str(collection_id)) != upload_slug:
                continue
            if self._stored_collection_manifest(records.values()) == files:
                return collection_id
        return None

    def _open_upload_session_for_slug(self, upload_slug: str) -> CollectionUploadRecord | None:
        for upload in sorted(
            self.state.collection_uploads.values(),
            key=lambda current: str(current.collection_id),
        ):
            if upload.state != "open":
                continue
            if self._collection_id_upload_slug(str(upload.collection_id)) == upload_slug:
                return upload
        return None

    def _matching_upload(
        self,
        upload_slug: str,
        files: list[dict[str, object]],
    ) -> CollectionUploadRecord | None:
        for upload in sorted(
            self.state.collection_uploads.values(),
            key=lambda current: str(current.collection_id),
        ):
            if self._collection_id_upload_slug(str(upload.collection_id)) != upload_slug:
                continue
            if self._upload_manifest(upload) == files:
                return upload
        return None

    @staticmethod
    def _collection_id_for_upload_timestamp(upload_slug: str, upload_timestamp: str) -> str:
        return f"{upload_timestamp[:4]}/{upload_timestamp}__{upload_slug}"

    @staticmethod
    def _ensure_requested_collection_id_matches(
        existing_collection_id: CollectionId,
        requested_collection_id: str | None,
    ) -> None:
        if (
            requested_collection_id is None
            or str(existing_collection_id) == requested_collection_id
        ):
            return
        raise Conflict(
            "collection upload already exists for this slug and manifest with a different "
            f"timestamp: {existing_collection_id}"
        )

    def _mint_collection_id(self, upload_slug: str) -> str:
        current = datetime.now(UTC).replace(microsecond=0)
        while True:
            stamp = current.strftime("%Y%m%dT%H%M%SZ")
            collection_id = f"{current:%Y}/{stamp}__{upload_slug}"
            key = CollectionId(collection_id)
            if (
                key not in self.state.files_by_collection
                and key not in self.state.collection_uploads
            ):
                return collection_id
            current += timedelta(seconds=1)

    @staticmethod
    def _collection_id_upload_slug(collection_id: str) -> str | None:
        leaf = collection_id.rsplit("/", 1)[-1]
        if "__" not in leaf:
            return None
        return leaf.split("__", 1)[1]

    @staticmethod
    def _upload_manifest(upload: CollectionUploadRecord) -> list[dict[str, object]]:
        return [
            {
                "path": file_record.path,
                "bytes": file_record.bytes,
                "sha256": str(file_record.sha256),
            }
            for file_record in sorted(upload.files.values(), key=lambda current: current.path)
        ]

    @staticmethod
    def _stored_collection_manifest(
        records: Iterable[StoredFile],
    ) -> list[dict[str, object]]:
        return [
            {
                "path": record.path,
                "bytes": record.bytes,
                "sha256": str(record.sha256),
            }
            for record in sorted(records, key=lambda current: current.path)
        ]

    def _finalized_upload_payload(self, collection_id: CollectionId) -> dict[str, object]:
        files = {
            record.path: CollectionUploadFileRecord(
                path=record.path,
                bytes=record.bytes,
                sha256=record.sha256,
                uploaded_bytes=record.bytes,
                uploaded_content=record.content,
            )
            for record in self.state.files_by_collection[collection_id].values()
        }
        upload = CollectionUploadRecord(
            collection_id=collection_id,
            ingest_source=None,
            files=files,
            state="finalized",
        )
        return self._upload_payload(
            upload,
            state="finalized",
            collection=self.state.collection_summary(str(collection_id)),
        )

    @_with_state_lock
    def get_upload(self, collection_id: str) -> dict[str, object]:
        normalized_collection_id = normalize_collection_id(collection_id)
        upload = self.state.collection_uploads.get(CollectionId(normalized_collection_id))
        if upload is None:
            raise NotFound(f"collection upload not found: {normalized_collection_id}")
        upload = self._expire_upload(upload)
        if upload is None:
            raise NotFound(f"collection upload not found: {normalized_collection_id}")
        if upload.state in {"open", "canceled", "expired"}:
            return self._upload_payload(upload, state=upload.state, collection=None)
        if self._is_complete(upload):
            if upload.state == "uploading":
                upload.state = "archiving"
            return self._upload_payload(upload, state=upload.state, collection=None)
        return self._upload_payload(upload, state="uploading", collection=None)

    @_with_state_lock
    def create_or_resume_file_upload(self, collection_id: str, path: str) -> dict[str, object]:
        normalized_collection_id = normalize_collection_id(collection_id)
        normalized_path = normalize_relpath(path)
        upload = self.state.collection_uploads.get(CollectionId(normalized_collection_id))
        if upload is None:
            raise NotFound(f"collection upload not found: {normalized_collection_id}")
        upload = self._expire_upload(upload)
        if upload is None:
            raise NotFound(f"collection upload not found: {normalized_collection_id}")
        if upload.state not in {"open", "uploading"}:
            raise Conflict(
                f"collection upload is not accepting file bytes: {normalized_collection_id}"
            )
        try:
            file_record = upload.files[normalized_path]
        except KeyError as exc:
            raise NotFound(f"collection upload file not found: {normalized_path}") from exc
        if file_record.upload_url is None:
            file_record.upload_url = (
                f"/files/collections/{quote(normalized_collection_id, safe='')}/"
                f"{quote(normalized_path, safe='/')}"
            )
        if self._file_upload_state(file_record) != "uploaded":
            file_record.upload_expires_at = FIXTURE_UPLOAD_EXPIRES_AT
        return {
            "path": file_record.path,
            "protocol": "tus",
            "upload_url": file_record.upload_url,
            "offset": file_record.uploaded_bytes,
            "length": file_record.bytes,
            "checksum_algorithm": "sha256",
            "expires_at": file_record.upload_expires_at,
        }

    @_with_state_lock
    def append_upload_chunk(
        self,
        collection_id: str,
        path: str,
        *,
        offset: int,
        checksum: str,
        content: bytes,
    ) -> dict[str, object]:
        normalized_collection_id = normalize_collection_id(collection_id)
        normalized_path = normalize_relpath(path)
        upload = self.state.collection_uploads.get(CollectionId(normalized_collection_id))
        if upload is None:
            raise NotFound(f"collection upload not found: {normalized_collection_id}")
        upload = self._expire_upload(upload)
        if upload is None:
            raise NotFound(f"collection upload not found: {normalized_collection_id}")
        if upload.state not in {"open", "uploading"}:
            raise Conflict(
                f"collection upload is not accepting file bytes: {normalized_collection_id}"
            )
        file_record = upload.files[normalized_path]
        if offset != file_record.uploaded_bytes:
            raise Conflict("upload offset did not match current collection file offset")
        algorithm, separator, digest = checksum.partition(" ")
        if separator != " " or algorithm != "sha256":
            raise Conflict("upload checksum must use sha256")
        actual_digest = base64.b64encode(hashlib.sha256(content).digest()).decode("ascii")
        if digest != actual_digest:
            raise HashMismatch("upload checksum did not match the provided chunk")
        next_offset = offset + len(content)
        if next_offset > file_record.bytes:
            raise Conflict("upload chunk exceeded the expected collection file length")

        current_content = file_record.uploaded_content or b""
        file_record.uploaded_content = current_content + content
        file_record.uploaded_bytes = next_offset
        if next_offset < file_record.bytes:
            file_record.upload_expires_at = FIXTURE_UPLOAD_EXPIRES_AT
        else:
            actual_sha = hashlib.sha256(file_record.uploaded_content).hexdigest()
            if actual_sha != file_record.sha256:
                raise HashMismatch("sha256 did not match expected file hash")
            file_record.upload_expires_at = None

        if self._is_complete(upload) and upload.state == "uploading":
            upload.state = "archiving"

        return {
            "offset": file_record.uploaded_bytes,
            "length": file_record.bytes,
            "expires_at": file_record.upload_expires_at,
        }

    @_with_state_lock
    def get_file_upload(self, collection_id: str, path: str) -> dict[str, object]:
        normalized_collection_id = normalize_collection_id(collection_id)
        normalized_path = normalize_relpath(path)
        upload = self.state.collection_uploads.get(CollectionId(normalized_collection_id))
        if upload is None:
            raise NotFound(f"collection upload not found: {normalized_collection_id}")
        upload = self._expire_upload(upload)
        if upload is None:
            raise NotFound(f"collection upload not found: {normalized_collection_id}")
        try:
            file_record = upload.files[normalized_path]
        except KeyError as exc:
            raise NotFound(f"collection upload file not found: {normalized_path}") from exc
        if file_record.upload_url is None:
            raise NotFound(f"collection upload file is not resumable: {normalized_path}")
        return {
            "path": file_record.path,
            "protocol": "tus",
            "upload_url": file_record.upload_url,
            "offset": file_record.uploaded_bytes,
            "length": file_record.bytes,
            "checksum_algorithm": "sha256",
            "expires_at": file_record.upload_expires_at,
        }

    @_with_state_lock
    def cancel_file_upload(self, collection_id: str, path: str) -> None:
        normalized_collection_id = normalize_collection_id(collection_id)
        normalized_path = normalize_relpath(path)
        upload = self.state.collection_uploads.get(CollectionId(normalized_collection_id))
        if upload is None:
            raise NotFound(f"collection upload not found: {normalized_collection_id}")
        upload = self._expire_upload(upload)
        if upload is None:
            raise NotFound(f"collection upload not found: {normalized_collection_id}")
        try:
            file_record = upload.files[normalized_path]
        except KeyError as exc:
            raise NotFound(f"collection upload file not found: {normalized_path}") from exc
        if file_record.upload_url is None:
            raise NotFound(f"collection upload file is not resumable: {normalized_path}")
        file_record.upload_url = None
        file_record.uploaded_bytes = 0
        file_record.uploaded_content = None
        file_record.upload_expires_at = None

    @_with_state_lock
    def expire_stale_uploads(self) -> None:
        for collection_id in list(self.state.collection_uploads):
            upload = self.state.collection_uploads.get(collection_id)
            if upload is None:
                continue
            self._expire_upload(upload)

    @_with_state_lock
    def get(
        self,
        collection_id: str,
        *,
        coverage_path_limit: int = 100,
    ) -> CollectionSummary:
        return self.state.collection_summary(collection_id)

    @_with_state_lock
    def list(
        self,
        *,
        page: int,
        per_page: int,
        q: str | None,
        disc_redundancy_state: str | None,
        sort: str = "id",
        order: str = "asc",
    ) -> CollectionListPage:
        needle = q.casefold() if q else None
        summaries = [
            self.state.collection_summary(str(collection_id))
            for collection_id in sorted(self.state.files_by_collection)
        ]
        if needle is not None:
            summaries = [summary for summary in summaries if needle in str(summary.id).casefold()]
        if disc_redundancy_state is not None:
            summaries = [
                summary
                for summary in summaries
                if summary.disc_redundancy.state.value == disc_redundancy_state
            ]
        if sort not in {
            "id",
            "bytes",
            "files",
            "hot_bytes",
            "disc_coverage",
            "disc_redundancy",
        }:
            sort = "id"
        reverse = order == "desc"
        sort_values = {
            "disc_coverage": lambda summary: summary.disc_coverage.bytes,
            "disc_redundancy": lambda summary: summary.disc_redundancy.bytes,
        }
        summaries.sort(
            key=lambda summary: (
                str(summary.id).casefold()
                if sort == "id"
                else sort_values[sort](summary)
                if sort in sort_values
                else int(getattr(summary, sort))
            ),
            reverse=reverse,
        )
        total = len(summaries)
        pages = math.ceil(total / per_page) if total else 0
        start = (page - 1) * per_page
        stop = start + per_page
        return CollectionListPage(
            page=page,
            per_page=per_page,
            total=total,
            pages=pages,
            collections=summaries[start:stop],
        )

    @staticmethod
    def _normalize_files(files: list[dict[str, object]]) -> list[dict[str, object]]:
        if not files:
            raise Conflict("collection upload must include at least one file")
        out: list[dict[str, object]] = []
        seen: set[str] = set()
        for item in files:
            path = normalize_relpath(str(item["path"]))
            if path in seen:
                raise Conflict(f"collection upload listed the same file more than once: {path}")
            seen.add(path)
            out.append(
                {
                    "path": path,
                    "bytes": int(item["bytes"]),
                    "sha256": str(item["sha256"]),
                }
            )
        return sorted(out, key=lambda current: str(current["path"]))

    @staticmethod
    def _file_upload_state(file_record: CollectionUploadFileRecord) -> str:
        if file_record.bytes > 0 and file_record.uploaded_bytes >= file_record.bytes:
            return "uploaded"
        if file_record.uploaded_bytes > 0:
            return "partial"
        return "pending"

    def _expire_upload(self, upload: CollectionUploadRecord) -> CollectionUploadRecord | None:
        now = datetime.now(UTC)
        expired_any = False
        for file_record in upload.files.values():
            if file_record.upload_expires_at is None:
                continue
            expires_at = datetime.fromisoformat(
                file_record.upload_expires_at.replace("Z", "+00:00")
            )
            if expires_at > now:
                continue
            expired_any = True
            file_record.upload_url = None
            file_record.uploaded_bytes = 0
            file_record.uploaded_content = None
            file_record.upload_expires_at = None
        if expired_any and self._has_no_live_file_state(upload):
            del self.state.collection_uploads[upload.collection_id]
            return None
        return upload

    def _has_no_live_file_state(self, upload: CollectionUploadRecord) -> bool:
        return all(
            self._file_upload_state(file_record) == "pending"
            and file_record.upload_url is None
            and file_record.upload_expires_at is None
            for file_record in upload.files.values()
        )

    def _is_complete(self, upload: CollectionUploadRecord) -> bool:
        return bool(upload.files) and all(
            self._file_upload_state(file_record) == "uploaded"
            for file_record in upload.files.values()
        )

    def _finalize_upload(self, upload: CollectionUploadRecord) -> CollectionSummary:
        files = {
            path: file_record.uploaded_content or b""
            for path, file_record in sorted(upload.files.items())
        }
        hot_paths = set(files)
        self.state.seed_collection(
            str(upload.collection_id),
            files,
            hot_paths=hot_paths,
        )
        summary = self.state.collection_summary(str(upload.collection_id))
        self.state.mark_collection_archive_uploaded(str(upload.collection_id))
        del self.state.collection_uploads[upload.collection_id]
        return summary

    def _upload_payload(
        self,
        upload: CollectionUploadRecord,
        *,
        state: str,
        collection: CollectionSummary | None,
    ) -> dict[str, object]:
        files = [upload.files[path] for path in sorted(upload.files)]
        upload_expiries = [
            file_record.upload_expires_at
            for file_record in files
            if file_record.upload_expires_at is not None
        ]
        return {
            "collection_id": str(upload.collection_id),
            "ingest_source": upload.ingest_source,
            "state": state,
            "files_total": len(files),
            "files_pending": sum(
                1 for file_record in files if self._file_upload_state(file_record) == "pending"
            ),
            "files_partial": sum(
                1 for file_record in files if self._file_upload_state(file_record) == "partial"
            ),
            "files_uploaded": sum(
                1 for file_record in files if self._file_upload_state(file_record) == "uploaded"
            ),
            "bytes_total": sum(file_record.bytes for file_record in files),
            "uploaded_bytes": sum(file_record.uploaded_bytes for file_record in files),
            "missing_bytes": max(
                sum(file_record.bytes for file_record in files)
                - sum(file_record.uploaded_bytes for file_record in files),
                0,
            ),
            "upload_state_expires_at": max(upload_expiries) if upload_expiries else None,
            "latest_failure": upload.latest_failure,
            "notify": upload.notify,
            "files": [
                {
                    "path": file_record.path,
                    "bytes": file_record.bytes,
                    "sha256": str(file_record.sha256),
                    "upload_state": self._file_upload_state(file_record),
                    "uploaded_bytes": file_record.uploaded_bytes,
                    "upload_state_expires_at": file_record.upload_expires_at,
                }
                for file_record in files
            ],
            "collection": (
                {
                    "id": str(collection.id),
                    "files": collection.files,
                    "bytes": collection.bytes,
                    "hot_bytes": collection.hot_bytes,
                    "disc_coverage": {
                        "state": collection.disc_coverage.state.value,
                        "bytes": collection.disc_coverage.bytes,
                    },
                    "disc_redundancy": {
                        "state": collection.disc_redundancy.state.value,
                        "bytes": collection.disc_redundancy.bytes,
                    },
                }
                if collection is not None
                else None
            ),
        }


@dataclass(slots=True)
class _ContainerSlot:
    container: ServiceContainer


def _clear_workspace(workspace: Path) -> None:
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True, exist_ok=True)


class AcceptanceSearchService:
    def __init__(self, state: AcceptanceState) -> None:
        self.state = state

    @_with_state_lock
    def search(
        self,
        *,
        q: str | None,
        page: int,
        per_page: int,
        sort: str,
        order: str,
        collection: str | None = None,
        hot: bool | None = None,
        disc_coverage: bool | None = None,
    ) -> dict[str, object]:
        normalized_collection = normalize_collection_id(collection) if collection else None
        needle = q.casefold() if q else None
        records: list[dict[str, object]] = []
        for collection_id in sorted(self.state.files_by_collection):
            collection_name = str(collection_id)
            if normalized_collection is not None and collection_name != normalized_collection:
                continue
            for record in sorted(
                self.state.collection_files(collection_id), key=lambda item: item.path
            ):
                full_path = record.projected_target
                if needle is not None and needle not in full_path.casefold():
                    continue
                if hot is not None and record.hot is not hot:
                    continue
                if disc_coverage is not None and record.disc_coverage is not disc_coverage:
                    continue
                records.append(
                    {
                        "target": record.projected_target,
                        "collection": collection_name,
                        "path": record.path,
                        "bytes": record.bytes,
                        "sha256": record.sha256,
                        "hot": record.hot,
                        "disc_coverage": record.disc_coverage,
                    }
                )

        reverse = order == "desc"
        if sort == "target":
            records.sort(key=lambda item: str(item["target"]).casefold(), reverse=reverse)
        elif sort == "collection":
            records.sort(
                key=lambda item: (str(item["collection"]).casefold(), str(item["path"]).casefold()),
                reverse=reverse,
            )
        elif sort == "path":
            records.sort(
                key=lambda item: (str(item["path"]).casefold(), str(item["collection"]).casefold()),
                reverse=reverse,
            )
        else:
            records.sort(
                key=lambda item: (int(item.get(sort, 0) or 0), str(item["target"]).casefold()),
                reverse=reverse,
            )
        total = len(records)
        pages = math.ceil(total / per_page) if total else 0
        start = (page - 1) * per_page
        return {
            "query": q,
            "collection": normalized_collection,
            "hot": hot,
            "disc_coverage": disc_coverage,
            "page": page,
            "per_page": per_page,
            "total": total,
            "pages": pages,
            "sort": sort,
            "order": order,
            "files": records[start : start + per_page],
        }


class AcceptancePlanningService:
    def __init__(self, state: AcceptanceState) -> None:
        self.state = state

    @_with_state_lock
    def process_due_refresh(self, *, limit: int = 1) -> int:
        _ = limit
        return 0

    @_with_state_lock
    def get_plan(
        self,
        *,
        page: int = 1,
        per_page: int = 25,
        sort: str = "fill",
        order: str = "desc",
        q: str | None = None,
        collection: str | None = None,
        iso_ready: bool | None = None,
    ) -> dict[str, object]:
        candidates = [
            image
            for image in self.state.candidates_by_id.values()
            if ImageId(image.finalized_id) not in self.state.finalized_images_by_id
            and all(
                CollectionId(collection_id) in self.state.files_by_collection
                and self.state.collection_archive_status(collection_id).state
                == ArchiveState.UPLOADED
                for collection_id in image.collections
            )
        ]
        covered = {
            (collection_id, path)
            for image in self.state.candidates_by_id.values()
            for collection_id, path in image.covered_paths
        }
        unplanned_bytes = sum(
            record.bytes
            for collection_files in self.state.files_by_collection.values()
            for record in collection_files.values()
            if (record.collection_id, record.path) not in covered
        )
        if q:
            needle = q.casefold()
            candidates = [
                candidate
                for candidate in candidates
                if needle in str(candidate.candidate_id).casefold()
                or any(
                    needle in collection_id.casefold() for collection_id in candidate.collections
                )
                or any(
                    needle in projected_path.casefold()
                    for projected_path in candidate.projected_paths
                )
            ]
        if collection:
            candidates = [
                candidate for candidate in candidates if collection in candidate.collections
            ]
        if iso_ready is not None:
            candidates = [candidate for candidate in candidates if candidate.iso_ready is iso_ready]

        reverse = order == "desc"
        sort_key = {
            "fill": lambda candidate: (
                candidate.fill,
                candidate.bytes,
                str(candidate.candidate_id),
            ),
            "bytes": lambda candidate: (
                candidate.bytes,
                candidate.fill,
                str(candidate.candidate_id),
            ),
            "files": lambda candidate: (
                candidate.files,
                candidate.bytes,
                str(candidate.candidate_id),
            ),
            "collections": lambda candidate: (
                len(candidate.collections),
                candidate.bytes,
                str(candidate.candidate_id),
            ),
            "candidate_id": lambda candidate: (str(candidate.candidate_id),),
        }[sort]
        candidates = sorted(candidates, key=sort_key, reverse=reverse)

        total = len(candidates)
        pages = math.ceil(total / per_page) if total else 0
        start = (page - 1) * per_page
        stop = start + per_page
        page_candidates = candidates[start:stop]
        return {
            "page": page,
            "per_page": per_page,
            "total": total,
            "pages": pages,
            "sort": sort,
            "order": order,
            "ready": bool(
                [
                    image
                    for image in self.state.candidates_by_id.values()
                    if ImageId(image.finalized_id) not in self.state.finalized_images_by_id
                ]
            ),
            "target_bytes": TARGET_BYTES,
            "min_fill_bytes": MIN_FILL_BYTES,
            "candidates": [candidate.plan_payload() for candidate in page_candidates],
            "unplanned_bytes": unplanned_bytes,
        }

    @_with_state_lock
    def list_images(
        self,
        *,
        page: int,
        per_page: int,
        sort: str,
        order: str,
        q: str | None,
        collection: str | None,
        has_discs: bool | None,
    ) -> dict[str, object]:
        images = list(self.state.finalized_images_by_id.values())
        if q:
            needle = q.casefold()
            images = [
                image
                for image in images
                if needle in image.finalized_id.casefold()
                or needle in image.filename.casefold()
                or any(needle in collection_id.casefold() for collection_id in image.collections)
            ]
        if collection:
            images = [image for image in images if collection in image.collections]
        if has_discs is not None:
            images = [image for image in images if (self._discs_registered(image) > 0) is has_discs]

        reverse = order == "desc"
        sort_key = {
            "finalized_at": lambda image: (image.finalized_id, image.filename),
            "bytes": lambda image: (image.bytes, image.finalized_id),
            "discs_registered": lambda image: (
                self._discs_registered(image),
                image.finalized_id,
            ),
        }[sort]
        images = sorted(images, key=sort_key, reverse=reverse)

        total = len(images)
        pages = math.ceil(total / per_page) if total else 0
        start = (page - 1) * per_page
        stop = start + per_page
        page_images = images[start:stop]
        return {
            "page": page,
            "per_page": per_page,
            "total": total,
            "pages": pages,
            "sort": sort,
            "order": order,
            "images": [
                image.finalized_image_payload(
                    discs_registered=self._discs_registered(image),
                    discs_verified=self._discs_verified(image),
                )
                for image in page_images
            ],
        }

    @_with_state_lock
    def get_image(self, image_id: str) -> dict[str, object]:
        image = self._finalized_image_record(image_id)
        return image.finalized_image_payload(
            discs_registered=self._discs_registered(image),
            discs_verified=self._discs_verified(image),
        )

    @_with_state_lock
    def finalize_image(self, candidate_id: str) -> dict[str, object]:
        candidate = self._candidate_record(candidate_id)
        if not candidate.iso_ready:
            raise InvalidState("image must be ISO-ready before finalization")
        finalized_key = ImageId(candidate.finalized_id)
        self.state.finalized_images_by_id.setdefault(finalized_key, candidate)
        self.state.ensure_required_disc_slots(candidate.finalized_id)
        image = self.state.finalized_images_by_id[finalized_key]
        return image.finalized_image_payload(
            discs_registered=self._discs_registered(image),
            discs_verified=self._discs_verified(image),
        )

    @_with_state_lock
    async def get_iso_stream(self, image_id: str) -> IsoStream:
        image = self._finalized_image_record(image_id)
        payload = self._fixture_iso_bytes(image)

        async def body() -> AsyncIterator[bytes]:
            yield payload

        return IsoStream(
            body=body(),
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="{image.filename}"',
                "Cache-Control": "no-store",
                "Content-Length": str(len(payload)),
                "X-Accel-Buffering": "no",
            },
        )

    def _candidate_record(self, candidate_id: str) -> CandidateRecord:
        image = self.state.candidates_by_id.get(ImageId(candidate_id))
        if image is None:
            raise NotFound(f"candidate not found: {candidate_id}")
        return image

    def _finalized_image_record(self, image_id: str) -> CandidateRecord:
        image = self.state.finalized_images_by_id.get(ImageId(image_id))
        if image is None:
            raise NotFound(f"image not found: {image_id}")
        return image

    def _discs_registered(self, image: CandidateRecord) -> int:
        return sum(
            1
            for (image_id, _disc_id), summary in self.state.disc_summaries.items()
            if image_id == image.finalized_id and disc_counts_toward_redundancy(summary.state.value)
        )

    def _discs_verified(self, image: CandidateRecord) -> int:
        return sum(
            1
            for (image_id, _disc_id), summary in self.state.disc_summaries.items()
            if image_id == image.finalized_id
            and disc_counts_as_verified(
                state=summary.state.value,
                verification_state=summary.verification_state.value,
            )
        )

    def _fixture_iso_bytes(self, image: CandidateRecord) -> bytes:
        if self.state.real_iso_streams_enabled:
            return _fixture_real_iso_bytes(
                image_root=image.image_root,
                image_id=str(image.finalized_id),
            )
        payload = {
            "fixture": "spec-iso",
            "image_id": image.finalized_id,
            "filename": image.filename,
            "files": sorted(
                path.relative_to(image.image_root).as_posix()
                for path in image.image_root.rglob("*")
                if path.is_file()
            ),
        }
        return json.dumps(payload, sort_keys=True).encode("utf-8")


class AcceptanceArchiveUploadService:
    def __init__(self, state: AcceptanceState) -> None:
        self.state = state

    @_with_state_lock
    def requeue_failed_uploads_for_startup(self, *, limit: int = 100) -> int:
        if limit < 1:
            return 0
        requeued = 0
        collections = AcceptanceCollectionService(self.state)
        for _collection_id, upload in sorted(self.state.collection_uploads.items()):
            if requeued >= limit:
                return requeued
            if upload.state != "failed" or not collections._is_complete(upload):
                continue
            upload.state = "archiving"
            upload.latest_failure = None
            requeued += 1
        return requeued

    @_with_state_lock
    def publish_restore_catalog(self) -> int:
        return sum(
            1
            for archive in self.state.collection_archive_status_by_collection.values()
            if archive.state == ArchiveState.UPLOADED
        )

    @_with_state_lock
    def process_due_uploads(self, *, limit: int = 1) -> int:
        attempted = 0
        for collection_id, upload in sorted(self.state.collection_uploads.items()):
            if attempted >= limit:
                return attempted
            if upload.state != "archiving" or not AcceptanceCollectionService(
                self.state
            )._is_complete(upload):
                continue
            defer_until = self.state.archive_upload_defer_until_by_collection.get(collection_id)
            if defer_until is not None:
                if time.monotonic() < defer_until:
                    continue
                self.state.archive_upload_defer_until_by_collection.pop(collection_id, None)
            failure = self.state.archive_upload_failures_by_collection.get(collection_id)
            upload.archive_attempt_count += 1
            if failure is not None:
                upload.latest_failure = failure
                upload.state = "archiving"
                self.state.collection_archive_status_by_collection[collection_id] = ArchiveStatus(
                    state=ArchiveState.RETRYING,
                    failure=failure,
                )
                self.state.deliver_webhook_payload(
                    {
                        "event": "collections.archive_retrying",
                        "collection_id": str(collection_id),
                        "error": failure,
                        "attempts": upload.archive_attempt_count,
                        "failed_at": _acceptance_isoformat(datetime.now(UTC)),
                        "next_retry_at": _acceptance_isoformat(
                            datetime.now(UTC)
                            + timedelta(seconds=_ARCHIVE_UPLOAD_SWEEP_INTERVAL_SECONDS)
                        ),
                        "retry_delay_seconds": _ARCHIVE_UPLOAD_SWEEP_INTERVAL_SECONDS,
                    },
                    delivered_at=datetime.now(UTC),
                    timeout_seconds=self.state.webhook_config().timeout_seconds,
                )
                attempted += 1
                continue
            AcceptanceCollectionService(self.state)._finalize_upload(upload)
            self.state.archive_usage_snapshots.append(_acceptance_archive_snapshot(self.state))
            attempted += 1
        return attempted


class AcceptanceArchiveRestoreService:
    def __init__(self, state: AcceptanceState) -> None:
        self.state = state

    @_with_state_lock
    def repair_missing_fetch_hot_files(
        self,
        *,
        limit: int = 100,
    ) -> int:
        _ = limit
        return 0

    @_with_state_lock
    def list(
        self,
        *,
        page: int,
        per_page: int,
        sort: str,
        order: str,
        terminal: str = "all",
        restore_type: str | None = None,
        state: str | None = None,
        collection: str | None = None,
        image: str | None = None,
    ) -> ArchiveRestoreListPage:
        if page < 1:
            raise BadRequest("page must be greater than or equal to 1")
        if per_page < 1 or per_page > 100:
            raise BadRequest("per_page must be between 1 and 100")
        if order not in {"asc", "desc"}:
            raise BadRequest("order must be asc or desc")
        if terminal not in {"active", "terminal", "all"}:
            raise BadRequest("terminal must be active, terminal, or all")
        active_states = {
            ArchiveRestoreState.REQUESTED,
            ArchiveRestoreState.READY,
            ArchiveRestoreState.PAUSED,
        }
        normalized_collection = (
            CollectionId(normalize_collection_id(collection)) if collection else None
        )
        image_id = ImageId(image) if image else None
        records = [
            record
            for record in self.state.archive_restores_by_id.values()
            if (restore_type is None or record.type == restore_type)
            and (
                terminal == "all"
                or (terminal == "active" and record.state in active_states)
                or (terminal == "terminal" and record.state not in active_states)
            )
            and (state is None or record.state.value == state)
            and (normalized_collection is None or normalized_collection in record.collection_ids)
            and (image_id is None or image_id in self.state._record_image_ids(record))
        ]

        def sort_value(record: AcceptanceArchiveRestoreRecord) -> object:
            if sort == "id":
                return record.restore_id
            if sort == "type":
                return record.type
            if sort == "state":
                return record.state.value
            if sort == "ready_at":
                return record.ready_at or ""
            if sort == "expires_at":
                return record.expires_at or ""
            if sort == "created_at":
                return record.created_at
            raise BadRequest(
                "sort must be one of created_at, id, expires_at, ready_at, state, type"
            )

        records.sort(key=lambda record: (sort_value(record), record.restore_id))
        if order == "desc":
            records.reverse()
        total = len(records)
        pages = (total + per_page - 1) // per_page if total else 0
        start = (page - 1) * per_page
        return ArchiveRestoreListPage(
            page=page,
            per_page=per_page,
            total=total,
            pages=pages,
            sort=sort,
            order=order,
            terminal=terminal,
            type=restore_type,
            state=state,
            collection=str(normalized_collection) if normalized_collection else None,
            image=str(image_id) if image_id else None,
            restores=[self._summary(record) for record in records[start : start + per_page]],
        )

    @_with_state_lock
    def get(self, restore_id: str) -> ArchiveRestoreSummary:
        with self.state.lock:
            record = self.state.archive_restores_by_id.get(restore_id)
            if record is None:
                raise NotFound(f"archive restore not found: {restore_id}")
            return self._summary(record)

    @_with_state_lock
    def get_for_collection(self, collection_id: str) -> ArchiveRestoreSummary:
        normalized_collection_id = CollectionId(normalize_collection_id(collection_id))
        restores = [
            record
            for record in self.state.archive_restores_by_id.values()
            if record.type == "fetch_materialization"
            and normalized_collection_id in record.collection_ids
        ]
        if not restores:
            raise NotFound(f"archive restore not found for collection: {collection_id}")
        return self._summary(sorted(restores, key=lambda record: record.created_at)[-1])

    @_with_state_lock
    def create_or_resume_for_collection(
        self,
        collection_id: str,
        *,
        paths: Sequence[str] | None = None,
    ) -> ArchiveRestoreSummary:
        normalized_collection_id = CollectionId(normalize_collection_id(collection_id))
        if (
            self.state.collection_archive_status(str(normalized_collection_id)).state
            != ArchiveState.UPLOADED
        ):
            raise InvalidState(
                "collection archive is not uploaded and cannot be restored yet: "
                f"{normalized_collection_id}"
            )
        normalized_paths = self._normalize_fetch_materialization_paths(
            normalized_collection_id,
            paths,
        )
        active_states = {
            ArchiveRestoreState.REQUESTED,
            ArchiveRestoreState.READY,
        }
        for record in self.state.archive_restores_by_id.values():
            if (
                record.type == "fetch_materialization"
                and normalized_collection_id in record.collection_ids
                and record.state in active_states
            ):
                if normalized_paths is None:
                    record.paths = None
                elif record.paths is not None:
                    record.paths = tuple(sorted({*record.paths, *normalized_paths}))
                return self._summary(record)
        current = datetime.now(UTC)
        restore_id = self._generated_fetch_materialization_restore_id(str(normalized_collection_id))
        record = AcceptanceArchiveRestoreRecord(
            restore_id=restore_id,
            image_id=ImageId(""),
            state=ArchiveRestoreState.REQUESTED,
            created_at=_acceptance_isoformat(current),
            type="fetch_materialization",
            image_ids=(),
            collection_ids=(normalized_collection_id,),
            paths=normalized_paths,
            latest_message="Archive restore queued; Riverhog will materialize requested files.",
        )
        self._request_restore(record, current)
        self.state.archive_restores_by_id[restore_id] = record
        return self._summary(record)

    @_with_state_lock
    def list_for_fetch(
        self,
        fetch_id: str,
        *,
        page: int,
        per_page: int,
        sort: str,
        order: str,
        state: str | None = None,
    ) -> ArchiveRestoreListPage:
        if page < 1:
            raise BadRequest("page must be greater than or equal to 1")
        if per_page < 1 or per_page > 100:
            raise BadRequest("per_page must be between 1 and 100")
        if order not in {"asc", "desc"}:
            raise BadRequest("order must be asc or desc")
        record = self._fetch_record(fetch_id)
        records = self._fetch_materialization_records_for_fetch(record, state=state)

        def sort_value(item: AcceptanceArchiveRestoreRecord) -> object:
            if sort == "id":
                return item.restore_id
            if sort == "type":
                return item.type
            if sort == "state":
                return item.state.value
            if sort == "ready_at":
                return item.ready_at or ""
            if sort == "expires_at":
                return item.expires_at or ""
            if sort == "created_at":
                return item.created_at
            raise BadRequest(
                "sort must be one of created_at, id, expires_at, ready_at, state, type"
            )

        records.sort(key=lambda item: (sort_value(item), item.restore_id))
        if order == "desc":
            records.reverse()
        total = len(records)
        pages = (total + per_page - 1) // per_page if total else 0
        start = (page - 1) * per_page
        return ArchiveRestoreListPage(
            page=page,
            per_page=per_page,
            total=total,
            pages=pages,
            sort=sort,
            order=order,
            terminal="all",
            type="fetch_materialization",
            state=state,
            collection=None,
            image=None,
            restores=[self._summary(item) for item in records[start : start + per_page]],
        )

    @_with_state_lock
    def create_or_resume_for_fetch(self, fetch_id: str) -> ArchiveRestoreListPage:
        fetch = self._fetch_record(fetch_id)
        if fetch.summary.state == FetchState.QUEUED_ARCHIVE:
            fetch.summary = AcceptanceFetchService(self.state)._replace_summary(
                fetch,
                state=FetchState.RESTORING_ARCHIVE,
            )
        paths_by_collection = self._fetch_paths(fetch, missing_only=True)
        for collection_id, paths in sorted(paths_by_collection.items()):
            self.create_or_resume_for_collection(str(collection_id), paths=sorted(paths))
        return self.list_for_fetch(
            fetch_id,
            page=1,
            per_page=100,
            sort="created_at",
            order="desc",
        )

    @_with_state_lock
    def cancel_for_fetch(self, fetch_id: str) -> ArchiveRestoreListPage:
        fetch = self._fetch_record(fetch_id)
        if fetch.summary.state in {
            FetchState.QUEUED_ARCHIVE,
            FetchState.RESTORING_ARCHIVE,
        }:
            fetch.summary = AcceptanceFetchService(self.state)._replace_summary(
                fetch,
                state=FetchState.DRAFT,
            )
        active_states = {
            ArchiveRestoreState.REQUESTED,
            ArchiveRestoreState.READY,
            ArchiveRestoreState.PAUSED,
        }
        restore_ids = [
            record.restore_id
            for record in self._fetch_materialization_records_for_fetch(fetch, state=None)
            if record.state in active_states
        ]
        for restore_id in restore_ids:
            self.cancel(restore_id)
        return self.list_for_fetch(
            fetch_id,
            page=1,
            per_page=100,
            sort="created_at",
            order="desc",
        )

    def _fetch_record(self, fetch_id: str) -> FetchRecord:
        try:
            return self.state.fetches[FetchId(fetch_id)]
        except KeyError as exc:
            raise NotFound(f"fetch not found: {fetch_id}") from exc

    def _fetch_paths(
        self,
        fetch: FetchRecord,
        *,
        missing_only: bool,
    ) -> dict[CollectionId, set[str]]:
        paths_by_collection: dict[CollectionId, set[str]] = {}
        for entry in fetch.entries.values():
            stored = self.state.files_by_collection[entry.collection_id][entry.path]
            if missing_only and stored.hot:
                continue
            paths_by_collection.setdefault(entry.collection_id, set()).add(entry.path)
        return paths_by_collection

    def _fetch_materialization_records_for_fetch(
        self,
        fetch: FetchRecord,
        *,
        state: str | None,
    ) -> list[AcceptanceArchiveRestoreRecord]:
        paths_by_collection = self._fetch_paths(fetch, missing_only=False)
        records: list[AcceptanceArchiveRestoreRecord] = []
        for record in self.state.archive_restores_by_id.values():
            if record.type != "fetch_materialization":
                continue
            if state is not None and record.state.value != state:
                continue
            if self._record_intersects_fetch(record, paths_by_collection):
                records.append(record)
        return records

    @staticmethod
    def _record_intersects_fetch(
        record: AcceptanceArchiveRestoreRecord,
        paths_by_collection: dict[CollectionId, set[str]],
    ) -> bool:
        for collection_id in record.collection_ids:
            fetch_paths = paths_by_collection.get(collection_id)
            if not fetch_paths:
                continue
            if record.paths is None or set(record.paths) & fetch_paths:
                return True
        return False

    def _normalize_fetch_materialization_paths(
        self,
        collection_id: CollectionId,
        paths: Sequence[str] | None,
    ) -> tuple[str, ...] | None:
        if paths is None:
            return None
        normalized_paths = tuple(dict.fromkeys(normalize_relpath(path) for path in paths))
        if not normalized_paths:
            return None
        collection_files = self.state.files_by_collection.get(collection_id)
        if collection_files is None:
            raise NotFound(f"collection not found: {collection_id}")
        missing_paths = sorted(set(normalized_paths) - set(collection_files))
        if missing_paths:
            raise NotFound(f"collection file not found: {missing_paths[0]}")
        return tuple(sorted(normalized_paths))

    def _request_restore(
        self,
        record: AcceptanceArchiveRestoreRecord,
        current: datetime,
    ) -> None:
        if record.requested_at is not None:
            return
        current_text = _acceptance_isoformat(current)
        estimated_ready_at = _acceptance_isoformat(
            current + timedelta(seconds=_ARCHIVE_RESTORE_LATENCY_SECONDS)
        )
        record.state = ArchiveRestoreState.REQUESTED
        record.requested_at = current_text
        record.ready_at = estimated_ready_at
        record.expires_at = None
        record.next_poll_at = _acceptance_isoformat(
            current + timedelta(seconds=_ARCHIVE_RESTORE_SWEEP_INTERVAL_SECONDS)
        )
        if record.type == "fetch_materialization":
            record.latest_message = (
                "Archive restore requested; Riverhog will materialize the requested files "
                "when the archive is ready."
            )
            return
        record.latest_message = (
            "Archive restore requested; wait until the restore is ready before burning "
            "replacement media."
        )

    @_with_state_lock
    def get_for_image(self, image_id: str) -> ArchiveRestoreSummary:
        with self.state.lock:
            record = self.state.latest_archive_restore(image_id)
            if record is None:
                raise NotFound(f"archive restore not found for image: {image_id}")
            return self._summary(record)

    @_with_state_lock
    def complete(self, restore_id: str) -> ArchiveRestoreSummary:
        with self.state.lock:
            record = self.state.archive_restores_by_id.get(restore_id)
            if record is None:
                raise NotFound(f"archive restore not found: {restore_id}")
            if record.state not in {
                ArchiveRestoreState.READY,
                ArchiveRestoreState.EXPIRED,
            }:
                raise InvalidState("archive restore is not ready to complete")
            current_text = _acceptance_isoformat(datetime.now(UTC))
            record.state = ArchiveRestoreState.COMPLETED
            record.completed_at = current_text
            record.expires_at = current_text
            record.next_poll_at = None
            record.next_reminder_at = None
            record.latest_message = (
                "Archive restore completed and restored ISO cleanup was recorded."
            )
            return self._summary(record)

    @_with_state_lock
    def cancel(self, restore_id: str) -> ArchiveRestoreSummary:
        with self.state.lock:
            record = self.state.archive_restores_by_id.get(restore_id)
            if record is None:
                raise NotFound(f"archive restore not found: {restore_id}")
            if record.type != "fetch_materialization":
                raise InvalidState(
                    "disc rebuild archive restores can be paused and resumed, not canceled"
                )
            if record.state not in {
                ArchiveRestoreState.REQUESTED,
                ArchiveRestoreState.READY,
            }:
                raise InvalidState("archive restore is not active and cannot be canceled")
            current_text = _acceptance_isoformat(datetime.now(UTC))
            record.state = ArchiveRestoreState.CANCELED
            record.canceled_at = current_text
            record.next_poll_at = None
            record.next_reminder_at = None
            record.latest_message = (
                "Fetch materialization was canceled by the operator; Riverhog will not "
                "materialize files for this restore."
            )
            return self._summary(record)

    @_with_state_lock
    def pause(self, restore_id: str) -> ArchiveRestoreSummary:
        with self.state.lock:
            record = self.state.archive_restores_by_id.get(restore_id)
            if record is None:
                raise NotFound(f"archive restore not found: {restore_id}")
            if record.type != "disc_rebuild":
                raise InvalidState(
                    "fetch materialization archive restores can be canceled, not paused"
                )
            if record.state == ArchiveRestoreState.PAUSED:
                return self._summary(record)
            if record.state not in {
                ArchiveRestoreState.REQUESTED,
                ArchiveRestoreState.READY,
            }:
                raise InvalidState(
                    "disc rebuild archive restore is not active and cannot be paused"
                )
            current = datetime.now(UTC)
            record.paused_from_state = record.state.value
            record.state = ArchiveRestoreState.PAUSED
            record.paused_at = _acceptance_isoformat(current)
            record.next_poll_at = None
            record.next_reminder_at = _acceptance_isoformat(
                current + timedelta(seconds=_OPERATOR_WEBHOOK_REMINDER_INTERVAL_SECONDS)
            )
            record.latest_message = (
                "Disc rebuild is paused by the operator; resume it when replacement "
                "media should continue."
            )
            return self._summary(record)

    @_with_state_lock
    def resume(self, restore_id: str) -> ArchiveRestoreSummary:
        with self.state.lock:
            record = self.state.archive_restores_by_id.get(restore_id)
            if record is None:
                raise NotFound(f"archive restore not found: {restore_id}")
            if record.type != "disc_rebuild":
                raise InvalidState(
                    "fetch materialization archive restores cannot be resumed from pause"
                )
            if record.state != ArchiveRestoreState.PAUSED:
                raise InvalidState("disc rebuild archive restore is not paused")
            current_text = _acceptance_isoformat(datetime.now(UTC))
            resume_state = record.paused_from_state or ArchiveRestoreState.REQUESTED.value
            if resume_state not in {
                ArchiveRestoreState.REQUESTED.value,
                ArchiveRestoreState.READY.value,
            }:
                resume_state = ArchiveRestoreState.REQUESTED.value
            record.state = ArchiveRestoreState(resume_state)
            record.paused_at = None
            record.paused_from_state = None
            record.next_poll_at = current_text
            record.next_reminder_at = None
            record.latest_message = "Disc rebuild resumed by the operator."
            return self._summary(record)

    @_with_state_lock
    def iter_restored_iso(self, restore_id: str, image_id: str) -> Iterator[bytes]:
        with self.state.lock:
            record = self.state.archive_restores_by_id.get(restore_id)
            if record is None:
                raise NotFound(f"archive restore not found: {restore_id}")
            if record.state != ArchiveRestoreState.READY:
                raise InvalidState("archive restore is not ready for ISO download")
            if ImageId(image_id) not in self.state._record_image_ids(record):
                raise NotFound(f"image not found in archive restore: {image_id}")
            image = self.state.finalized_images_by_id[ImageId(image_id)]
            if self.state.real_iso_streams_enabled:
                image_root = image.image_root
                image_id = str(image.finalized_id)
            else:
                image_root = None
                image_id = ""
        if image_root is not None:
            yield _fixture_real_iso_bytes(image_root=image_root, image_id=image_id)
            return
        with self.state.lock:
            payload = {
                "fixture": "spec-restored-iso",
                "image_id": image.finalized_id,
                "filename": image.filename,
                "files": sorted(
                    path.relative_to(image.image_root).as_posix()
                    for path in image.image_root.rglob("*")
                    if path.is_file()
                ),
            }
            yield json.dumps(payload, sort_keys=True).encode("utf-8")

    @_with_state_lock
    def process_due_restores(self, *, limit: int = 100) -> int:
        with self.state.lock:
            if limit < 1:
                return 0
            current = datetime.now(UTC)
            current_text = _acceptance_isoformat(current)
            processed = 0
            for record in sorted(
                self.state.archive_restores_by_id.values(),
                key=lambda current_record: (current_record.created_at, current_record.restore_id),
            ):
                if processed >= limit:
                    break
                if record.state == ArchiveRestoreState.REQUESTED and record.requested_at is None:
                    self._request_restore(record, current)
                    processed += 1
                    continue
                if (
                    record.state == ArchiveRestoreState.REQUESTED
                    and record.ready_at is not None
                    and record.ready_at <= current_text
                ):
                    self._mark_ready(record, current)
                    processed += 1
                    continue
                if (
                    record.state == ArchiveRestoreState.READY
                    and record.next_reminder_at is not None
                    and record.next_reminder_at <= current_text
                ):
                    image = self.state.finalized_images_by_id[record.image_id]
                    initial_notification_succeeded = record.last_notified_at is not None
                    try:
                        self.state.deliver_webhook_payload(
                            build_archive_restore_ready_payload(
                                config=self.state.webhook_config(),
                                restore_id=record.restore_id,
                                expires_at=record.expires_at,
                                images=[
                                    {
                                        "image_id": str(record.image_id),
                                        "filename": image.filename,
                                    }
                                ],
                                delivered_at=current,
                                reminder_count=record.reminder_count,
                                reminder=initial_notification_succeeded,
                            ),
                            delivered_at=current,
                            timeout_seconds=self.state.webhook_config().timeout_seconds,
                        )
                    except Exception as exc:
                        record.latest_message = (
                            "Ready notification failed and will retry: "
                            f"{str(exc).strip() or exc.__class__.__name__}"
                        )
                        record.next_reminder_at = _acceptance_isoformat(
                            current + timedelta(seconds=_OPERATOR_WEBHOOK_RETRY_DELAY_SECONDS)
                        )
                        processed += 1
                        continue
                    record.last_notified_at = current_text
                    record.next_reminder_at = _acceptance_isoformat(
                        current + timedelta(seconds=_OPERATOR_WEBHOOK_REMINDER_INTERVAL_SECONDS)
                    )
                    if initial_notification_succeeded:
                        record.reminder_count += 1
                    processed += 1
                    continue
                if (
                    record.state == ArchiveRestoreState.READY
                    and record.expires_at is not None
                    and record.expires_at <= current_text
                ):
                    record.state = ArchiveRestoreState.EXPIRED
                    record.next_reminder_at = None
                    record.next_poll_at = None
                    record.latest_message = (
                        "Restored ISO data expired and cleanup was recorded; "
                        "re-initiate recovery to request a new restore."
                    )
                    processed += 1
                if (
                    record.state == ArchiveRestoreState.PAUSED
                    and record.next_reminder_at is not None
                    and record.next_reminder_at <= current_text
                ):
                    images = [
                        self.state.finalized_images_by_id[image_id]
                        for image_id in self.state._record_image_ids(record)
                    ]
                    try:
                        self.state.deliver_webhook_payload(
                            build_archive_restore_paused_reminder_payload(
                                config=self.state.webhook_config(),
                                restore_id=record.restore_id,
                                images=[
                                    {
                                        "image_id": str(image.finalized_id),
                                        "filename": image.filename,
                                    }
                                    for image in images
                                ],
                                collections=[
                                    {"collection_id": str(collection_id)}
                                    for collection_id in record.collection_ids
                                ],
                                delivered_at=current,
                                reminder_count=record.reminder_count,
                                reminder_interval_seconds=(
                                    _OPERATOR_WEBHOOK_REMINDER_INTERVAL_SECONDS
                                ),
                            ),
                            delivered_at=current,
                            timeout_seconds=self.state.webhook_config().timeout_seconds,
                        )
                    except Exception as exc:
                        record.latest_message = (
                            "Paused rebuild reminder failed and will retry: "
                            f"{str(exc).strip() or exc.__class__.__name__}"
                        )
                        record.next_reminder_at = _acceptance_isoformat(
                            current + timedelta(seconds=_OPERATOR_WEBHOOK_RETRY_DELAY_SECONDS)
                        )
                        processed += 1
                        continue
                    record.last_notified_at = current_text
                    record.reminder_count += 1
                    record.next_reminder_at = _acceptance_isoformat(
                        current + timedelta(seconds=_OPERATOR_WEBHOOK_REMINDER_INTERVAL_SECONDS)
                    )
                    processed += 1
            return processed

    def _mark_ready(
        self,
        record: AcceptanceArchiveRestoreRecord,
        current: datetime,
    ) -> None:
        current_text = _acceptance_isoformat(current)
        record.state = ArchiveRestoreState.READY
        record.ready_at = current_text
        record.expires_at = _acceptance_isoformat(
            current + timedelta(seconds=_ARCHIVE_RESTORE_READY_TTL_SECONDS)
        )
        record.next_poll_at = None
        record.latest_message = (
            "Restored ISO data is ready; reopen the restore to complete download, "
            "verify the ISO, and burn replacement media before cleanup."
        )
        if record.type == "fetch_materialization":
            self._complete_fetch_materialization(record, current)
            return
        image = self.state.finalized_images_by_id[record.image_id]
        try:
            self.state.deliver_webhook_payload(
                build_archive_restore_ready_payload(
                    config=self.state.webhook_config(),
                    restore_id=record.restore_id,
                    expires_at=record.expires_at,
                    images=[
                        {
                            "image_id": str(record.image_id),
                            "filename": image.filename,
                        }
                    ],
                    delivered_at=current,
                    reminder_count=record.reminder_count,
                    reminder=False,
                ),
                delivered_at=current,
                timeout_seconds=self.state.webhook_config().timeout_seconds,
            )
        except Exception as exc:
            record.latest_message = (
                "Ready notification failed and will retry: "
                f"{str(exc).strip() or exc.__class__.__name__}"
            )
            record.next_reminder_at = _acceptance_isoformat(
                current + timedelta(seconds=_OPERATOR_WEBHOOK_RETRY_DELAY_SECONDS)
            )
            return
        record.last_notified_at = current_text
        record.next_reminder_at = _acceptance_isoformat(
            current + timedelta(seconds=_OPERATOR_WEBHOOK_REMINDER_INTERVAL_SECONDS)
        )

    def _complete_fetch_materialization(
        self,
        record: AcceptanceArchiveRestoreRecord,
        current: datetime,
    ) -> None:
        for collection_id in record.collection_ids:
            collection_files = self.state.files_by_collection.get(collection_id)
            if collection_files is None:
                raise NotFound(f"collection not found: {collection_id}")
            selected_paths = record.paths or tuple(sorted(collection_files))
            for path in selected_paths:
                if path not in collection_files:
                    raise NotFound(f"collection file not found: {path}")
                collection_files[path].hot = True
        current_text = _acceptance_isoformat(current)
        record.archive_verification_state = "completed"
        record.extraction_state = "completed"
        record.materialization_state = "completed"
        record.state = ArchiveRestoreState.COMPLETED
        record.completed_at = current_text
        record.expires_at = current_text
        record.next_poll_at = None
        record.next_reminder_at = None
        record.latest_message = (
            "Fetch materialization completed; requested files are hot and temporary "
            "archive restore cleanup was recorded."
        )

    def _generated_fetch_materialization_restore_id(self, collection_id: str) -> str:
        existing_ids = set(self.state.archive_restores_by_id)
        safe_collection_id = collection_id.replace("/", "-")
        ordinal = 1
        while True:
            restore_id = f"ar-{safe_collection_id}-restore-{ordinal}"
            ordinal += 1
            if restore_id not in existing_ids:
                return restore_id

    def _summary(self, record: AcceptanceArchiveRestoreRecord) -> ArchiveRestoreSummary:
        images = tuple(
            self.state.finalized_images_by_id[image_id]
            for image_id in self.state._record_image_ids(record)
        )
        collection_ids = record.collection_ids or tuple(
            sorted(
                {collection_id for image in images for collection_id, _path in image.covered_paths}
            )
        )
        warnings = (
            "Archive restore requests take time; the configured restore latency "
            "estimate is short in test fixtures.",
            "Riverhog will notify and remind the operator through the configured "
            "operator webhook in test fixtures.",
            "Restored ISO data will be cleaned up after the configured ready "
            "window if recovery is not completed sooner.",
        )
        return ArchiveRestoreSummary(
            id=record.restore_id,
            type=record.type,
            state=record.state,
            created_at=record.created_at,
            requested_at=record.requested_at,
            ready_at=record.ready_at,
            expires_at=record.expires_at,
            completed_at=record.completed_at,
            canceled_at=record.canceled_at,
            paused_at=record.paused_at,
            paused_from_state=record.paused_from_state,
            paths=record.paths if record.type == "fetch_materialization" else None,
            latest_message=record.latest_message,
            warnings=warnings,
            notification=ArchiveRestoreNotificationStatus(
                webhook_configured=True,
                reminder_count=record.reminder_count,
                next_reminder_at=record.next_reminder_at,
                last_notified_at=record.last_notified_at,
                failure_count=record.failure_count,
                last_failure_at=record.last_failure_at,
                last_failure=record.last_failure,
            ),
            progress=ArchiveRestoreProgress(
                archive_verification=record.archive_verification_state,
                extraction=record.extraction_state,
                materialization=record.materialization_state,
            ),
            collections=tuple(
                ArchiveRestoreCollection(
                    id=collection_id,
                    archive=self.state.collection_archive_status(str(collection_id)),
                    collection_manifest=CollectionManifestStatus(
                        object_path=(
                            f"{_fixture_archive_prefix(str(collection_id))}/manifest.yml.age"
                        ),
                        sha256="0" * 64,
                        ots_object_path=(
                            f"{_fixture_archive_prefix(str(collection_id))}/manifest.yml.ots.age"
                        ),
                        ots_state="uploaded",
                        ots_sha256="1" * 64,
                    ),
                    stored_bytes=int(
                        self.state.collection_archive_status(str(collection_id)).stored_bytes or 0
                    ),
                )
                for collection_id in collection_ids
            ),
            images=(
                tuple(
                    ArchiveRestoreImage(
                        id=image.finalized_id,
                        filename=image.filename,
                        collection_ids=tuple(
                            CollectionId(collection_id) for collection_id in image.collections
                        ),
                        rebuild_state=_acceptance_rebuild_state(record),
                    )
                    for image in images
                )
            ),
        )


class AcceptanceArchiveReportingService:
    def __init__(self, state: AcceptanceState) -> None:
        self.state = state

    @_with_state_lock
    def get_report(
        self,
        *,
        image_id: str | None = None,
        collection: str | None = None,
    ) -> ArchiveUsageReport:
        images = [
            image
            for image in self.state.finalized_images_by_id.values()
            if (image_id is None or image.finalized_id == image_id)
            and (
                collection is None
                or any(
                    current_collection == collection
                    for current_collection, _ in image.covered_paths
                )
            )
        ]
        images.sort(key=lambda current: current.finalized_id, reverse=True)
        image_reports = tuple(_acceptance_archive_image(image) for image in images)
        collection_reports = tuple(
            _acceptance_archive_collections(
                images=images,
                state=self.state,
                collection_filter=collection,
            )
        )
        totals = ArchiveUsageTotals(
            collections=len(collection_reports),
            uploaded_collections=sum(
                1 for report in collection_reports if report.measured_storage_bytes > 0
            ),
            measured_storage_bytes=sum(
                report.measured_storage_bytes for report in collection_reports
            ),
        )
        history = (
            tuple(self.state.archive_usage_snapshots)
            if image_id is None and collection is None
            else ()
        )
        return ArchiveUsageReport(
            scope=_acceptance_archive_scope(image_id=image_id, collection=collection),
            measured_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            totals=totals,
            images=image_reports,
            collections=collection_reports,
            history=history,
        )


def _acceptance_isoformat(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _acceptance_rebuild_state(record: AcceptanceArchiveRestoreRecord) -> str:
    if record.state == ArchiveRestoreState.REQUESTED:
        return "restoring_collections"
    if record.state in {ArchiveRestoreState.READY, ArchiveRestoreState.COMPLETED}:
        return "ready"
    if record.state == ArchiveRestoreState.PAUSED:
        return "paused"
    if record.state == ArchiveRestoreState.CANCELED:
        return "canceled"
    if record.state in {ArchiveRestoreState.EXPIRED, ArchiveRestoreState.FAILED}:
        return "failed"
    return "pending"


def _acceptance_archive_scope(*, image_id: str | None, collection: str | None) -> str:
    if image_id is not None and collection is not None:
        return "filtered"
    if image_id is not None:
        return "image"
    if collection is not None:
        return "collection"
    return "all"


def _acceptance_archive_image(image: CandidateRecord) -> ArchiveUsageImage:
    return ArchiveUsageImage(
        id=image.finalized_id,
        filename=image.filename,
        collection_ids=image.collections,
    )


def _acceptance_archive_collections(
    *,
    images: list[CandidateRecord],
    state: AcceptanceState,
    collection_filter: str | None,
) -> list[ArchiveUsageCollection]:
    contributions_by_collection: dict[str, list[ArchiveCollectionContribution]] = defaultdict(list)
    for image in images:
        represented_by_collection = _acceptance_represented_bytes_by_collection(state, image)
        if collection_filter is not None:
            represented_by_collection = {
                collection_id: represented
                for collection_id, represented in represented_by_collection.items()
                if collection_id == collection_filter
            }
        for collection_id, represented_bytes in represented_by_collection.items():
            contributions_by_collection[collection_id].append(
                ArchiveCollectionContribution(
                    image_id=image.finalized_id,
                    filename=image.filename,
                    represented_bytes=represented_bytes,
                )
            )

    reports: list[ArchiveUsageCollection] = []
    for collection_id, records in sorted(state.files_by_collection.items()):
        normalized_collection_id = str(collection_id)
        if collection_filter is not None and normalized_collection_id != collection_filter:
            continue
        contributions = sorted(
            contributions_by_collection.get(normalized_collection_id, []),
            key=lambda current: str(current.image_id),
            reverse=True,
        )
        archive = state.collection_archive_status(normalized_collection_id)
        measured_storage_bytes = (
            int(archive.stored_bytes or 0) if archive.state == ArchiveState.UPLOADED else 0
        )
        reports.append(
            ArchiveUsageCollection(
                id=normalized_collection_id,
                bytes=sum(record.bytes for record in records.values()),
                measured_storage_bytes=measured_storage_bytes,
                images=tuple(contributions),
                archive=archive,
                collection_manifest=(
                    CollectionManifestStatus(
                        object_path=(
                            f"{_fixture_archive_prefix(normalized_collection_id)}/manifest.yml.age"
                        ),
                        sha256="0" * 64,
                        ots_object_path=(
                            f"{_fixture_archive_prefix(normalized_collection_id)}/manifest.yml.ots.age"
                        ),
                        ots_state="uploaded",
                        ots_sha256="1" * 64,
                    )
                    if archive.state == ArchiveState.UPLOADED
                    else None
                ),
                archive_format="tar" if archive.state == ArchiveState.UPLOADED else None,
                compression="none" if archive.state == ArchiveState.UPLOADED else None,
            )
        )
    return reports


def _acceptance_represented_bytes_by_collection(
    state: AcceptanceState,
    image: CandidateRecord,
) -> dict[str, int]:
    manifest_path = image.image_root / MANIFEST_FILENAME
    disc_manifest = yaml.safe_load(fixture_decrypt_bytes(manifest_path.read_bytes()))
    represented_by_collection: dict[str, int] = defaultdict(int)
    for collection in disc_manifest.get("collections", []):
        collection_id = str(collection["id"])
        for file_entry in collection.get("files", []):
            path = str(file_entry["path"]).lstrip("/")
            record = state.files_by_collection[CollectionId(collection_id)][path]
            parts_block = file_entry.get("parts")
            if parts_block is None:
                represented_by_collection[collection_id] += record.bytes
                continue
            part_count = int(parts_block["count"])
            for present in parts_block.get("present", []):
                represented_by_collection[collection_id] += _split_part_length(
                    record.bytes,
                    part_count=part_count,
                    part_index=int(present["index"]) - 1,
                )
    return dict(represented_by_collection)


def _acceptance_manifest_entries_for_collection(
    image: CandidateRecord,
    collection_id: CollectionId,
) -> dict[str, _RecoveryParts]:
    manifest_path = image.image_root / MANIFEST_FILENAME
    disc_manifest = yaml.safe_load(fixture_decrypt_bytes(manifest_path.read_bytes()))
    for collection in disc_manifest.get("collections", []):
        if CollectionId(str(collection["id"])) != collection_id:
            continue
        entries: dict[str, _RecoveryParts] = {}
        for file_entry in collection.get("files", []):
            path = str(file_entry["path"]).lstrip("/")
            parts_block = file_entry.get("parts")
            if parts_block is None:
                entries[path] = _RecoveryParts(part_count=1, present_parts=frozenset({0}))
                continue
            entries[path] = _RecoveryParts(
                part_count=int(parts_block["count"]),
                present_parts=frozenset(
                    int(present["index"]) - 1 for present in parts_block.get("present", [])
                ),
            )
        return entries
    return {}


def _path_is_recoverable(
    path: str,
    *,
    image_ids: set[str],
    recovery_parts_by_image_path: dict[tuple[str, str], _RecoveryParts],
    image_available: Callable[[CollectionCoverageImage], bool],
    image_by_id: dict[str, CollectionCoverageImage],
) -> bool:
    if not image_ids:
        return False

    expected_part_count: int | None = None
    present_parts: set[int] = set()
    for image_id in image_ids:
        image = image_by_id.get(image_id)
        if image is None or not image_available(image):
            continue
        recovery_parts = recovery_parts_by_image_path.get((image_id, path))
        if recovery_parts is None:
            continue
        if recovery_parts.part_count == 1 and recovery_parts.present_parts == frozenset({0}):
            return True
        if expected_part_count is None:
            expected_part_count = recovery_parts.part_count
        elif expected_part_count != recovery_parts.part_count:
            return False
        present_parts.update(recovery_parts.present_parts)
    return expected_part_count is not None and len(present_parts) == expected_part_count


def _partial_recoverable_bytes(
    total_bytes: int,
    *,
    image_ids: set[str],
    recovery_parts_by_image_path: dict[tuple[str, str], _RecoveryParts],
    image_available: Callable[[CollectionCoverageImage], bool],
    image_by_id: dict[str, CollectionCoverageImage],
) -> int:
    expected_part_count: int | None = None
    present_parts: set[int] = set()
    for image_id in image_ids:
        image = image_by_id.get(image_id)
        if image is None or not image_available(image):
            continue
        recovery_parts = next(
            (
                parts
                for (current_image_id, _path), parts in recovery_parts_by_image_path.items()
                if current_image_id == image_id
            ),
            None,
        )
        if recovery_parts is None or recovery_parts.part_count <= 1:
            continue
        expected_part_count = recovery_parts.part_count
        present_parts.update(recovery_parts.present_parts)
    if expected_part_count is None or not present_parts:
        return 0
    return sum(
        _split_part_length(
            total_bytes,
            part_count=expected_part_count,
            part_index=part_index,
        )
        for part_index in present_parts
    )


def _acceptance_archive_snapshot(state: AcceptanceState) -> ArchiveUsageSnapshot:
    collection_reports = tuple(
        _acceptance_archive_collections(
            images=list(state.finalized_images_by_id.values()),
            state=state,
            collection_filter=None,
        )
    )
    return ArchiveUsageSnapshot(
        captured_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        uploaded_collections=sum(
            1 for collection in collection_reports if collection.measured_storage_bytes > 0
        ),
        measured_storage_bytes=sum(
            collection.measured_storage_bytes for collection in collection_reports
        ),
    )


def _split_part_length(total_bytes: int, *, part_count: int, part_index: int) -> int:
    base, remainder = divmod(total_bytes, part_count)
    return base + int(part_index < remainder)


class AcceptanceDiscService:
    def __init__(self, state: AcceptanceState) -> None:
        self.state = state

    @_with_state_lock
    def _read_disc_part_info(
        self, image: CandidateRecord
    ) -> dict[tuple[str, str], tuple[int, int] | tuple[None, None]]:
        manifest_path = image.image_root / MANIFEST_FILENAME
        raw_bytes = manifest_path.read_bytes()
        disc_manifest = yaml.safe_load(fixture_decrypt_bytes(raw_bytes))
        result: dict[tuple[str, str], tuple[int, int] | tuple[None, None]] = {}
        for collection in disc_manifest.get("collections", []):
            coll_id = str(collection["id"])
            for file_entry in collection.get("files", []):
                path = str(file_entry["path"]).lstrip("/")
                if "parts" in file_entry:
                    count = int(file_entry["parts"]["count"])
                    index_1based = int(file_entry["parts"]["present"][0]["index"])
                    result[(coll_id, path)] = (index_1based - 1, count)
                else:
                    result[(coll_id, path)] = (None, None)
        return result

    @_with_state_lock
    def register(
        self,
        image_id: str,
        location: str,
        *,
        disc_id: str | None = None,
    ) -> DiscSummary:
        image = self.state.finalized_images_by_id.get(ImageId(image_id))
        if image is None:
            raise NotFound(f"image not found: {image_id}")
        self.state.ensure_required_disc_slots(image.finalized_id)
        target = self._registration_target(image.finalized_id, disc_id)
        summary = DiscSummary(
            disc_id=target.disc_id,
            image_id=image.finalized_id,
            label_text=target.label_text,
            location=location,
            created_at=target.created_at,
            state=DiscState.REGISTERED,
            verification_state=target.verification_state,
            history=(
                *target.history,
                DiscHistoryEntry(
                    at=DEFAULT_DISC_CREATED_AT,
                    event="registered",
                    state=DiscState.REGISTERED,
                    verification_state=target.verification_state,
                    location=location,
                ),
            ),
        )
        scoped_key = (image.finalized_id, target.disc_id)
        self.state.disc_summaries[scoped_key] = summary
        disc_info = self._read_disc_part_info(image)
        for collection_id, path in image.covered_paths:
            record = self.state.files_by_collection[collection_id][path]
            if all(
                (existing.disc_id, existing.image_id) != (target.disc_id, image.finalized_id)
                for existing in record.discs
            ):
                part_index, part_count = disc_info.get((str(collection_id), path), (None, None))
                kwargs: dict[str, object] = {}
                if part_index is not None and part_count is not None:
                    file_parts = split_fixture_plaintext(record.content, part_count)
                    kwargs = {
                        "part_index": part_index,
                        "part_count": part_count,
                        "part_bytes": len(file_parts[part_index]),
                        "part_sha256": hashlib.sha256(file_parts[part_index]).hexdigest(),
                    }
                record.discs.append(
                    AcceptanceState._disc_from_dict(
                        build_file_disc(
                            disc_id=str(target.disc_id),
                            image_id=image.finalized_id,
                            location=location,
                            collection_id=str(collection_id),
                            path=path,
                            **kwargs,
                        )
                    )
                )
        return summary

    @_with_state_lock
    def list_for_image(self, image_id: str) -> list[DiscSummary]:
        self.state.ensure_required_disc_slots(image_id)
        return [
            summary
            for (scoped_image_id, _disc_id), summary in sorted(self.state.disc_summaries.items())
            if scoped_image_id == image_id
        ]

    @_with_state_lock
    def list_discs(
        self,
        *,
        page: int,
        per_page: int,
        sort: str,
        order: str,
        q: str | None,
        image_id: str | None,
    ) -> dict[str, object]:
        if image_id is not None:
            self.state.ensure_required_disc_slots(image_id)
        normalized_order = order.casefold()
        if normalized_order not in {"asc", "desc"}:
            raise BadRequest("order must be asc or desc")
        if sort not in _DISC_SORT_FIELDS:
            raise BadRequest(f"sort must be one of {', '.join(sorted(_DISC_SORT_FIELDS))}")

        discs = [
            self._disc_payload(summary)
            for (scoped_image_id, _disc_id), summary in sorted(self.state.disc_summaries.items())
            if image_id is None or scoped_image_id == image_id
        ]
        if q:
            needle = q.casefold()
            discs = [
                disc
                for disc in discs
                if needle
                in " ".join(
                    str(disc.get(field, ""))
                    for field in (
                        "disc_id",
                        "image_id",
                        "state",
                        "verification_state",
                        "location",
                    )
                ).casefold()
            ]
        discs.sort(
            key=lambda disc: str(disc.get(sort) or "").casefold(),
            reverse=normalized_order == "desc",
        )
        total = len(discs)
        pages = math.ceil(total / per_page) if total else 0
        start = (page - 1) * per_page
        payload: dict[str, object] = {
            "page": page,
            "per_page": per_page,
            "total": total,
            "pages": pages,
            "sort": sort,
            "order": normalized_order,
            "query": q,
            "discs": discs[start : start + per_page],
        }
        if image_id is not None:
            payload["image_id"] = image_id
        return payload

    @_with_state_lock
    def get_disc(self, disc_id: str) -> dict[str, object]:
        matches = [
            summary
            for (_image_id, scoped_disc_id), summary in sorted(self.state.disc_summaries.items())
            if str(scoped_disc_id) == disc_id
        ]
        if not matches:
            raise NotFound(f"disc not found: {disc_id}")
        if len(matches) > 1:
            raise BadRequest(f"disc id is ambiguous: {disc_id}")
        return self._disc_payload(matches[0])

    def _disc_payload(self, summary: DiscSummary) -> dict[str, object]:
        image = self.state.finalized_images_by_id.get(ImageId(summary.image_id))
        return {
            "disc_id": str(summary.disc_id),
            "image_id": summary.image_id,
            "filename": image.filename if image is not None else None,
            "label_text": summary.label_text,
            "location": summary.location,
            "created_at": summary.created_at,
            "state": summary.state.value,
            "verification_state": summary.verification_state.value,
            "history": [
                {
                    "at": entry.at,
                    "event": entry.event,
                    "state": entry.state.value,
                    "verification_state": entry.verification_state.value,
                    "location": entry.location,
                }
                for entry in summary.history
            ],
        }

    @_with_state_lock
    def notify_label_needed(self, image_id: str, disc_id: str) -> DiscSummary:
        self.state.ensure_required_disc_slots(image_id)
        scoped_key = (image_id, DiscId(disc_id))
        summary = self.state.disc_summaries.get(scoped_key)
        if summary is None:
            raise NotFound(f"disc not found for image: {disc_id}")
        return summary

    @_with_state_lock
    def update(
        self,
        image_id: str,
        disc_id: str,
        *,
        location: str | None = None,
        state: str | None = None,
        verification_state: str | None = None,
    ) -> DiscSummary:
        self.state.ensure_required_disc_slots(image_id)
        scoped_key = (image_id, DiscId(disc_id))
        summary = self.state.disc_summaries.get(scoped_key)
        if summary is None:
            raise NotFound(f"disc not found for image: {disc_id}")
        previous_state = summary.state
        next_state = DiscState(state) if state is not None else summary.state
        next_verification_state = (
            VerificationState(verification_state)
            if verification_state is not None
            else summary.verification_state
        )
        next_location = location if location is not None else summary.location
        location_changed = location is not None and location != summary.location
        state_changed = next_state != summary.state
        verification_changed = next_verification_state != summary.verification_state
        updated = DiscSummary(
            disc_id=summary.disc_id,
            image_id=summary.image_id,
            label_text=summary.label_text,
            location=next_location,
            created_at=summary.created_at,
            state=next_state,
            verification_state=next_verification_state,
            history=(
                *summary.history,
                DiscHistoryEntry(
                    at=DEFAULT_DISC_CREATED_AT,
                    event=_history_event_name(
                        location_changed=location_changed,
                        state_changed=state_changed,
                        verification_changed=verification_changed,
                    ),
                    state=next_state,
                    verification_state=next_verification_state,
                    location=next_location,
                ),
            ),
        )
        self.state.disc_summaries[scoped_key] = updated
        self._sync_file_disc_visibility(updated)
        if disc_counts_toward_redundancy(previous_state.value) and next_state in {
            DiscState.LOST,
            DiscState.DAMAGED,
        }:
            self.state.ensure_required_disc_slots(image_id)
            self.state.ensure_archive_restore(image_id)
        return updated

    def _registration_target(self, image_id: str, disc_id: str | None) -> DiscSummary:
        discs = self.list_for_image(image_id)
        if disc_id is None:
            for summary in discs:
                if summary.state in {DiscState.NEEDED, DiscState.BURNING}:
                    return summary
            raise Conflict("all required disc slots are already registered")
        for summary in discs:
            if str(summary.disc_id) != disc_id:
                continue
            if summary.state not in {DiscState.NEEDED, DiscState.BURNING}:
                raise Conflict(f"disc is not available for registration: {disc_id}")
            return summary
        raise NotFound(f"disc not found for image: {disc_id}")

    def _sync_file_disc_visibility(self, summary: DiscSummary) -> None:
        for records in self.state.files_by_collection.values():
            for record in records.values():
                remaining_discs: list[FileDisc] = []
                for disc in record.discs:
                    if (disc.image_id, str(disc.disc_id)) != (
                        summary.image_id,
                        str(summary.disc_id),
                    ):
                        remaining_discs.append(disc)
                        continue
                    if disc_counts_toward_redundancy(summary.state.value) and summary.location:
                        remaining_discs.append(
                            FileDisc(
                                disc_id=disc.disc_id,
                                image_id=disc.image_id,
                                location=summary.location,
                                disc_path=disc.disc_path,
                                enc=disc.enc,
                                part_index=disc.part_index,
                                part_count=disc.part_count,
                                part_bytes=disc.part_bytes,
                                part_sha256=disc.part_sha256,
                            )
                        )
                record.discs = remaining_discs


def _fetch_summary_payload(summary: FetchSummary) -> dict[str, object]:
    return {
        "id": str(summary.id),
        "name": summary.name,
        "targets": [str(target) for target in summary.targets],
        "state": summary.state.value,
        "files": summary.files,
        "bytes": summary.bytes,
        "entries_total": summary.entries_total,
        "entries_pending": summary.entries_pending,
        "entries_partial": summary.entries_partial,
        "entries_byte_complete": summary.entries_byte_complete,
        "entries_uploaded": summary.entries_uploaded,
        "uploaded_bytes": summary.uploaded_bytes,
        "missing_bytes": summary.missing_bytes,
        "upload_state_expires_at": summary.upload_state_expires_at,
        "discs": [
            {
                "disc_id": str(disc.disc_id),
                "image_id": disc.image_id,
                "location": disc.location,
            }
            for disc in summary.discs
        ],
    }


class AcceptanceFetchService:
    def __init__(self, state: AcceptanceState) -> None:
        self.state = state

    @_with_state_lock
    def create(self, *, name: str, targets: list[str] | None = None) -> FetchSummary:
        canonical_targets = self._canonical_targets(targets or [])
        selected = self._selected_files_for_targets(canonical_targets, missing_ok=True)
        return self._create_fetch_record(
            name=name,
            targets=canonical_targets,
            files=selected,
            initial_state=FetchState.DRAFT,
        )

    @_with_state_lock
    def list(
        self,
        *,
        page: int,
        per_page: int,
        state: str | None = None,
        q: str | None = None,
        sort: str = "order",
        order: str = "asc",
    ) -> FetchListPage:
        normalized_order = order.casefold()
        if normalized_order not in {"asc", "desc"}:
            raise BadRequest("order must be asc or desc")
        if sort not in {"id", "name", "state", "order", "files", "bytes", "missing_bytes"}:
            raise BadRequest("invalid fetch sort field")
        summaries = [self._replace_summary(record) for record in self.state.fetches.values()]
        if state is not None:
            summaries = [summary for summary in summaries if summary.state.value == state]
        if q:
            needle = q.casefold()
            summaries = [
                summary
                for summary in summaries
                if needle
                in " ".join(
                    [
                        str(summary.id),
                        summary.name,
                        summary.state.value,
                        " ".join(str(target) for target in summary.targets),
                    ]
                ).casefold()
            ]
        sort_key = {
            "id": lambda summary: str(summary.id),
            "name": lambda summary: summary.name,
            "state": lambda summary: summary.state.value,
            "order": lambda summary: int(str(summary.id).removeprefix("fx-") or 0),
            "files": lambda summary: summary.files,
            "bytes": lambda summary: summary.bytes,
            "missing_bytes": lambda summary: summary.missing_bytes,
        }[sort]
        summaries.sort(key=sort_key, reverse=normalized_order == "desc")
        total = len(summaries)
        pages = math.ceil(total / per_page) if total else 0
        start = (page - 1) * per_page
        return FetchListPage(
            page=page,
            per_page=per_page,
            total=total,
            pages=pages,
            fetches=summaries[start : start + per_page],
        )

    @_with_state_lock
    def add_targets(self, fetch_id: str, targets: list[str]) -> FetchSummary:
        record = self._record(fetch_id)
        self._require_editable(record)
        canonical_targets = self._canonical_targets(targets)
        existing = [str(target) for target in record.summary.targets]
        merged = [*existing, *[target for target in canonical_targets if target not in existing]]
        self._replace_targets(record, merged)
        return record.summary

    @_with_state_lock
    def remove_targets(self, fetch_id: str, targets: list[str]) -> FetchSummary:
        record = self._record(fetch_id)
        self._require_editable(record)
        removals = set(self._canonical_targets(targets))
        remaining = [
            str(target) for target in record.summary.targets if str(target) not in removals
        ]
        self._replace_targets(record, remaining)
        return record.summary

    @_with_state_lock
    def start(self, fetch_id: str, *, archive: bool = False) -> FetchSummary:
        record = self._record(fetch_id)
        if record.summary.state == FetchState.DONE:
            raise InvalidState("fetch is already complete")
        if record.summary.state in {
            FetchState.QUEUED_DJDAN,
            FetchState.UPLOADING,
            FetchState.VERIFYING,
            FetchState.QUEUED_ARCHIVE,
            FetchState.RESTORING_ARCHIVE,
        }:
            raise InvalidState("fetch is already started")
        if record.summary.state != FetchState.DRAFT:
            raise InvalidState(f"fetch cannot be started from state {record.summary.state.value}")
        if not record.summary.targets:
            raise InvalidState("fetch has no targets")
        state = FetchState.QUEUED_ARCHIVE if archive else FetchState.QUEUED_DJDAN
        record.summary = self._replace_summary(record, state=state)
        return record.summary

    @_with_state_lock
    def start_plan(self, fetch_id: str, *, archive: bool = False) -> dict[str, object]:
        record = self._record(fetch_id)
        if record.summary.state == FetchState.DONE:
            raise InvalidState("fetch is already complete")
        if record.summary.state in {
            FetchState.QUEUED_DJDAN,
            FetchState.UPLOADING,
            FetchState.VERIFYING,
            FetchState.QUEUED_ARCHIVE,
            FetchState.RESTORING_ARCHIVE,
        }:
            raise InvalidState("fetch is already started")
        if record.summary.state != FetchState.DRAFT:
            raise InvalidState(f"fetch cannot be started from state {record.summary.state.value}")
        if not record.summary.targets:
            raise InvalidState("fetch has no targets")
        return {
            **_fetch_summary_payload(record.summary),
            "dry_run": True,
            "status": "would_queue_archive" if archive else "would_queue_djdan",
            "archive": archive,
            "queued_state": (
                FetchState.QUEUED_ARCHIVE.value if archive else FetchState.QUEUED_DJDAN.value
            ),
            "will_create_archive_restore": archive,
        }

    @_with_state_lock
    def cancel(self, fetch_id: str) -> FetchSummary:
        record = self._record(fetch_id)
        if record.summary.state in {
            FetchState.QUEUED_ARCHIVE,
            FetchState.RESTORING_ARCHIVE,
        }:
            raise InvalidState("archive fetches are canceled through archive restores")
        if record.summary.state == FetchState.DONE:
            raise InvalidState("completed fetches cannot be canceled")
        for entry in record.entries.values():
            entry.upload_url = None
            entry.uploaded_bytes = 0
            entry.uploaded_content = None
            entry.upload_expires_at = None
        record.entries = {}
        record.summary = self._replace_summary(record, state=FetchState.DRAFT)
        return record.summary

    @_with_state_lock
    def evict(self, targets: list[str], *, dry_run: bool = False) -> dict[str, object]:
        canonical_targets = self._canonical_targets(targets)
        if not canonical_targets:
            raise BadRequest("at least one target is required")
        selected = self._selected_files_for_targets(canonical_targets)
        noncompliant = [
            record for record in selected if not self.state.file_is_fully_compliant(record)
        ]
        if noncompliant:
            first = noncompliant[0]
            raise Conflict(
                "cannot evict hot file without verified disc redundancy: "
                f"{first.collection_id}/{first.path}"
            )
        would_evict_files = sum(1 for record in selected if record.hot)
        would_evict_bytes = sum(record.bytes for record in selected if record.hot)
        if dry_run:
            return {
                "targets": canonical_targets,
                "dry_run": True,
                "status": "would_evict",
                "files": len(selected),
                "bytes": sum(record.bytes for record in selected),
                "evicted_files": 0,
                "evicted_bytes": 0,
                "would_evict_files": would_evict_files,
                "would_evict_bytes": would_evict_bytes,
            }
        evicted_files = 0
        evicted_bytes = 0
        for record in selected:
            if not record.hot:
                continue
            record.hot = False
            evicted_files += 1
            evicted_bytes += record.bytes
        return {
            "targets": canonical_targets,
            "dry_run": False,
            "status": "evicted",
            "files": len(selected),
            "bytes": sum(record.bytes for record in selected),
            "evicted_files": evicted_files,
            "evicted_bytes": evicted_bytes,
            "would_evict_files": would_evict_files,
            "would_evict_bytes": would_evict_bytes,
        }

    @_with_state_lock
    def _create_fetch_record(
        self,
        *,
        name: str,
        targets: list[str],
        files: list[StoredFile],
        fetch_id: str | None = None,
        initial_state: FetchState = FetchState.DRAFT,
    ) -> FetchSummary:
        if fetch_id is None:
            self.state.next_fetch_number += 1
            fetch_id = f"fx-{self.state.next_fetch_number}"
        else:
            self.state.reserve_fetch_id(fetch_id)
        fetch_key = FetchId(fetch_id)
        entries = self._entries_for_files(files)
        summary = FetchSummary(
            id=fetch_key,
            name=name,
            targets=tuple(TargetStr(target) for target in targets),
            state=initial_state,
            files=len(entries),
            bytes=sum(entry.bytes for entry in entries.values()),
            discs=self._summary_discs(entries.values()),
        )
        record = FetchRecord(summary=summary, entries=entries)
        record.summary = self._replace_summary(record, state=initial_state)
        self.state.fetches[fetch_key] = record
        return record.summary

    @_with_state_lock
    def get(self, fetch_id: str) -> FetchSummary:
        record = self._record(fetch_id)
        self._expire_stale_upload_record(record)
        record.summary = self._replace_summary(record)
        return record.summary

    @_with_state_lock
    def manifest(self, fetch_id: str) -> dict[str, object]:
        record = self._record(fetch_id)
        self._require_djdan_fetch(record)
        self._expire_stale_upload_record(record)
        record.summary = self._replace_summary(record)
        return {
            "id": str(record.summary.id),
            "name": record.summary.name,
            "targets": [str(target) for target in record.summary.targets],
            "entries": [
                {
                    "id": str(entry.id),
                    "collection_id": str(entry.collection_id),
                    "path": entry.path,
                    "bytes": entry.bytes,
                    "sha256": str(entry.sha256),
                    "recovery_bytes": self._entry_recovery_bytes(entry),
                    "upload_state": self._entry_upload_state(
                        entry,
                        fetch_state=record.summary.state,
                    ),
                    "uploaded_bytes": entry.uploaded_bytes,
                    "upload_state_expires_at": entry.upload_expires_at,
                    "discs": [self._manifest_disc(entry, disc) for disc in entry.discs],
                    "parts": self._manifest_parts(entry),
                }
                for entry in record.entries.values()
            ],
        }

    @_with_state_lock
    def status(self, fetch_id: str, *, limit: int = 25) -> dict[str, object]:
        record = self._record(fetch_id)
        self._expire_stale_upload_record(record)
        record.summary = self._replace_summary(record)
        selected_files = self._selected_files_for_record(record)
        file_rollup = self._fetch_file_rollup(selected_files)
        files_preview = (
            [
                self._fetch_file_payload(file)
                for file in sorted(
                    selected_files, key=lambda item: (str(item.collection_id), item.path)
                )[:limit]
            ]
            if limit > 0
            else []
        )
        next_action, next_action_reason = self._fetch_next_action(record.summary, file_rollup)
        entries: list[dict[str, object]] = []
        if limit > 0:
            for entry in record.entries.values():
                upload_state = self._entry_upload_state(
                    entry,
                    fetch_state=record.summary.state,
                )
                if upload_state == "uploaded":
                    continue
                entries.append(
                    {
                        "id": str(entry.id),
                        "collection_id": str(entry.collection_id),
                        "path": entry.path,
                        "bytes": entry.bytes,
                        "upload_state": upload_state,
                        "uploaded_bytes": entry.uploaded_bytes,
                        "upload_state_expires_at": entry.upload_expires_at,
                    }
                )
                if len(entries) >= limit:
                    break
        return {
            "id": str(record.summary.id),
            "name": record.summary.name,
            "targets": [str(target) for target in record.summary.targets],
            "state": record.summary.state.value,
            "files": record.summary.files,
            "bytes": record.summary.bytes,
            "hot_files": file_rollup["hot_files"],
            "hot_bytes": file_rollup["hot_bytes"],
            "disc_files": file_rollup["disc_files"],
            "disc_bytes": file_rollup["disc_bytes"],
            "missing_files": file_rollup["missing_files"],
            "missing_with_disc_files": file_rollup["missing_with_disc_files"],
            "missing_without_disc_files": file_rollup["missing_without_disc_files"],
            "entries_total": record.summary.entries_total,
            "entries_pending": record.summary.entries_pending,
            "entries_partial": record.summary.entries_partial,
            "entries_byte_complete": record.summary.entries_byte_complete,
            "entries_uploaded": record.summary.entries_uploaded,
            "uploaded_bytes": record.summary.uploaded_bytes,
            "missing_bytes": record.summary.missing_bytes,
            "upload_state_expires_at": record.summary.upload_state_expires_at,
            "discs": [
                {
                    "disc_id": str(disc.disc_id),
                    "image_id": disc.image_id,
                    "location": disc.location,
                }
                for disc in record.summary.discs
            ],
            "entries_limit": limit,
            "entries_returned": len(entries),
            "entries": entries,
            "target_summaries": self._fetch_target_summaries(record),
            "files_preview_limit": limit,
            "files_preview_returned": len(files_preview),
            "files_preview": files_preview,
            "next_action": next_action,
            "next_action_reason": next_action_reason,
        }

    @_with_state_lock
    def files(
        self,
        fetch_id: str,
        *,
        page: int,
        per_page: int,
        sort: str,
        order: str,
        q: str | None = None,
        hot: bool | None = None,
        disc_coverage: bool | None = None,
    ) -> dict[str, object]:
        if page < 1:
            raise BadRequest("page must be greater than or equal to 1")
        if per_page < 1 or per_page > 100:
            raise BadRequest("per_page must be between 1 and 100")
        if sort not in {"target", "collection", "path", "bytes", "hot", "disc"}:
            raise BadRequest("invalid fetch file sort field")
        normalized_order = order.casefold()
        if normalized_order not in {"asc", "desc"}:
            raise BadRequest("order must be asc or desc")
        record = self._record(fetch_id)
        rows = [self._fetch_file_payload(file) for file in self._selected_files_for_record(record)]
        if q:
            needle = q.casefold()
            rows = [
                row
                for row in rows
                if needle
                in " ".join(
                    [str(row["target"]), str(row["collection_id"]), str(row["path"])]
                ).casefold()
            ]
        if hot is not None:
            rows = [row for row in rows if row["hot"] is hot]
        if disc_coverage is not None:
            rows = [row for row in rows if row["disc_coverage"] is disc_coverage]
        rows.sort(key=lambda row: (str(row["collection_id"]), str(row["path"])))
        sort_key = {
            "target": lambda row: str(row["target"]),
            "collection": lambda row: str(row["collection_id"]),
            "path": lambda row: str(row["path"]),
            "bytes": lambda row: int(row["bytes"]),
            "hot": lambda row: bool(row["hot"]),
            "disc": lambda row: bool(row["disc_coverage"]),
        }[sort]
        rows.sort(key=sort_key, reverse=normalized_order == "desc")
        total = len(rows)
        pages = math.ceil(total / per_page) if total else 0
        start = (page - 1) * per_page
        return {
            "fetch_id": fetch_id,
            "query": q,
            "hot": hot,
            "disc_coverage": disc_coverage,
            "page": page,
            "per_page": per_page,
            "total": total,
            "pages": pages,
            "sort": sort,
            "order": normalized_order,
            "files": rows[start : start + per_page],
        }

    @_with_state_lock
    def create_or_resume_upload(self, fetch_id: str, entry_id: str) -> dict[str, object]:
        record = self._record(fetch_id)
        self._require_djdan_fetch(record)
        self._expire_stale_upload_record(record)
        if record.summary.state == FetchState.DONE:
            raise InvalidState("fetch is already complete")
        self._raise_if_restoring_archive(record)
        entry = record.entries.get(EntryId(entry_id))
        if entry is None:
            raise NotFound(f"entry not found: {entry_id}")
        if entry.upload_url is None:
            entry.upload_url = (
                f"/v1/fetches/{quote(str(record.summary.id), safe='/')}/entries/"
                f"{quote(str(entry.id), safe='/')}/upload"
            )
        if self._entry_upload_state(entry, fetch_state=record.summary.state) in {
            "pending",
            "partial",
        }:
            entry.upload_expires_at = FIXTURE_UPLOAD_EXPIRES_AT
        record.summary = self._replace_summary(record)
        return self._entry_upload_payload(entry)

    @_with_state_lock
    def append_upload_chunk(
        self,
        fetch_id: str,
        entry_id: str,
        offset: int,
        checksum: str,
        content: bytes,
    ) -> dict[str, object]:
        record = self._record(fetch_id)
        self._expire_stale_upload_record(record)
        self._raise_if_restoring_archive(record)
        entry = record.entries.get(EntryId(entry_id))
        if entry is None:
            raise NotFound(f"entry not found: {entry_id}")
        if offset != entry.uploaded_bytes:
            raise Conflict("upload offset did not match current entry offset")
        algorithm, separator, digest = checksum.partition(" ")
        if separator != " " or algorithm != "sha256":
            raise InvalidState("upload checksum must use sha256")
        actual_digest = base64.b64encode(hashlib.sha256(content).digest()).decode("ascii")
        if digest != actual_digest:
            raise HashMismatch("upload checksum did not match the provided chunk")
        next_uploaded_bytes = offset + len(content)
        if next_uploaded_bytes > self._entry_recovery_bytes(entry):
            raise Conflict("upload chunk exceeded the expected entry length")

        current_content = entry.uploaded_content or b""
        entry.uploaded_content = current_content + content
        entry.uploaded_bytes = next_uploaded_bytes

        if entry.uploaded_bytes < self._entry_recovery_bytes(entry):
            entry.upload_expires_at = FIXTURE_UPLOAD_EXPIRES_AT
        else:
            entry.upload_expires_at = None

        if record.summary.state == FetchState.QUEUED_DJDAN:
            record.summary = self._replace_summary(record, state=FetchState.UPLOADING)
        else:
            record.summary = self._replace_summary(record)

        return {
            "offset": entry.uploaded_bytes,
            "length": self._entry_recovery_bytes(entry),
            "expires_at": entry.upload_expires_at,
        }

    @_with_state_lock
    def get_entry_upload(self, fetch_id: str, entry_id: str) -> dict[str, object]:
        record = self._record(fetch_id)
        self._expire_stale_upload_record(record)
        entry = record.entries.get(EntryId(entry_id))
        if entry is None:
            raise NotFound(f"entry not found: {entry_id}")
        if entry.upload_url is None:
            raise NotFound(f"fetch entry upload is not resumable: {entry_id}")
        record.summary = self._replace_summary(record)
        return self._entry_upload_payload(entry)

    @_with_state_lock
    def cancel_entry_upload(self, fetch_id: str, entry_id: str) -> None:
        record = self._record(fetch_id)
        self._expire_stale_upload_record(record)
        entry = record.entries.get(EntryId(entry_id))
        if entry is None:
            raise NotFound(f"entry not found: {entry_id}")
        if entry.upload_url is None:
            raise NotFound(f"fetch entry upload is not resumable: {entry_id}")
        entry.upload_url = None
        entry.uploaded_bytes = 0
        entry.uploaded_content = None
        entry.upload_expires_at = None
        if record.summary.state == FetchState.UPLOADING:
            record.summary = self._replace_summary(record, state=FetchState.QUEUED_DJDAN)
        else:
            record.summary = self._replace_summary(record)

    @_with_state_lock
    def expire_stale_uploads(self) -> None:
        for record in self.state.fetches.values():
            self._expire_stale_upload_record(record)

    @_with_state_lock
    def deliver_due_queued_notifications(self, *, limit: int = 100) -> int:
        if limit < 1:
            return 0
        current = datetime.now(UTC)
        delivered = 0
        for record in sorted(self.state.fetches.values(), key=lambda item: str(item.summary.id)):
            if delivered >= limit:
                return delivered
            self._expire_stale_upload_record(record)
            record.summary = self._replace_summary(record)
            if record.summary.state != FetchState.QUEUED_DJDAN:
                continue
            if self._active_archive_restore_id(record) is not None:
                continue
            if (
                record.notification_sent_at is not None
                and record.notification_next_attempt_at is not None
                and record.notification_next_attempt_at > _acceptance_isoformat(current)
            ):
                continue
            reminder = record.notification_sent_at is not None
            try:
                self.state.deliver_webhook_payload(
                    build_fetch_queued_payload(
                        config=self.state.webhook_config(),
                        fetch_id=str(record.summary.id),
                        name=record.summary.name,
                        files=record.summary.files,
                        bytes=record.summary.bytes,
                        discs=[
                            {
                                "disc_id": str(disc.disc_id),
                                "image_id": disc.image_id,
                                "location": disc.location,
                            }
                            for disc in record.summary.discs
                        ],
                        delivered_at=current,
                        reminder_count=record.notification_count,
                        reminder=reminder,
                    ),
                    delivered_at=current,
                    timeout_seconds=self.state.webhook_config().timeout_seconds,
                )
            except Exception as exc:
                record.notification_failure = str(exc).strip() or exc.__class__.__name__
                record.notification_next_attempt_at = _acceptance_isoformat(
                    current + timedelta(seconds=_OPERATOR_WEBHOOK_RETRY_DELAY_SECONDS)
                )
                continue
            if record.notification_sent_at is None:
                record.notification_sent_at = _acceptance_isoformat(current)
            if reminder:
                record.notification_count += 1
            record.notification_failure = None
            record.notification_next_attempt_at = _acceptance_isoformat(
                current + timedelta(seconds=_OPERATOR_WEBHOOK_REMINDER_INTERVAL_SECONDS)
            )
            delivered += 1
        return delivered

    @_with_state_lock
    def complete(self, fetch_id: str) -> dict[str, object]:
        record = self._record(fetch_id)
        if record.summary.state == FetchState.DONE:
            return {
                "id": str(record.summary.id),
                "state": record.summary.state.value,
                "hot": self._hot_payload_for_fetch(record),
            }
        self._require_djdan_fetch(record)
        self._raise_if_restoring_archive(record)
        if any(
            self._entry_upload_state(entry, fetch_state=record.summary.state) != "byte_complete"
            for entry in record.entries.values()
        ):
            raise InvalidState("fetch is missing required entry uploads")
        record.summary = self._replace_summary(record, state=FetchState.VERIFYING)
        for entry in record.entries.values():
            self._verify_uploaded_entry(entry)
            stored = self.state.files_by_collection[entry.collection_id][entry.path]
            stored.hot = True
            entry.upload_url = None
            entry.upload_expires_at = None
        record.summary = self._replace_summary(record, state=FetchState.DONE)
        hot = self._hot_payload_for_fetch(record)
        return {
            "id": str(record.summary.id),
            "state": record.summary.state.value,
            "hot": hot,
        }

    @_with_state_lock
    def upload_all_required_entries(self, fetch_id: str) -> None:
        record = self._record(fetch_id)
        for entry in record.entries.values():
            recovery_stream = b"".join(self._entry_recovery_payloads(entry))
            entry.uploaded_bytes = len(recovery_stream)
            entry.uploaded_content = recovery_stream
            entry.upload_expires_at = None
        if record.summary.state == FetchState.QUEUED_DJDAN:
            record.summary = self._replace_summary(record, state=FetchState.UPLOADING)
        else:
            record.summary = self._replace_summary(record)

    @_with_state_lock
    def upload_partial_entry(self, fetch_id: str, entry_id: str) -> int:
        record = self._record(fetch_id)
        entry = record.entries.get(EntryId(entry_id))
        if entry is None:
            raise NotFound(f"entry not found: {entry_id}")
        recovery_stream = b"".join(self._entry_recovery_payloads(entry))
        partial = recovery_stream[: max(1, len(recovery_stream) // 2)]
        entry.uploaded_bytes = len(partial)
        entry.uploaded_content = partial
        entry.upload_expires_at = FIXTURE_UPLOAD_EXPIRES_AT
        if record.summary.state == FetchState.QUEUED_DJDAN:
            record.summary = self._replace_summary(record, state=FetchState.UPLOADING)
        else:
            record.summary = self._replace_summary(record)
        return len(partial)

    @staticmethod
    def _canonical_targets(targets: list[str]) -> list[str]:
        return [parse_target(target).canonical for target in targets]

    def _selected_files_for_targets(
        self,
        targets: list[str],
        *,
        missing_ok: bool = False,
    ) -> list[StoredFile]:
        selected_by_key: dict[tuple[CollectionId, str], StoredFile] = {}
        for target in targets:
            for record in self.state.selected_files(target, missing_ok=missing_ok):
                selected_by_key[(record.collection_id, record.path)] = record
        if targets and not selected_by_key and not missing_ok:
            raise NotFound("fetch targets matched no files")
        return [
            selected_by_key[key]
            for key in sorted(selected_by_key, key=lambda item: (str(item[0]), item[1]))
        ]

    def _selected_files_for_record(self, record: FetchRecord) -> list[StoredFile]:
        return self._selected_files_for_targets(
            [str(target) for target in record.summary.targets],
            missing_ok=True,
        )

    @staticmethod
    def _fetch_file_payload(record: StoredFile) -> dict[str, object]:
        return {
            "target": record.projected_target,
            "collection_id": str(record.collection_id),
            "path": record.path,
            "bytes": record.bytes,
            "hot": record.hot,
            "disc_coverage": record.disc_coverage,
        }

    @staticmethod
    def _fetch_file_rollup(files: list[StoredFile]) -> dict[str, int]:
        hot_files = [record for record in files if record.hot]
        disc_files = [record for record in files if record.discs]
        missing_files = [record for record in files if not record.hot]
        missing_with_disc_files = [record for record in missing_files if record.discs]
        missing_without_disc_files = [record for record in missing_files if not record.discs]
        return {
            "files": len(files),
            "bytes": sum(record.bytes for record in files),
            "hot_files": len(hot_files),
            "hot_bytes": sum(record.bytes for record in hot_files),
            "disc_files": len(disc_files),
            "disc_bytes": sum(record.bytes for record in disc_files),
            "missing_files": len(missing_files),
            "missing_with_disc_files": len(missing_with_disc_files),
            "missing_without_disc_files": len(missing_without_disc_files),
        }

    def _fetch_target_summaries(self, record: FetchRecord) -> list[dict[str, object]]:
        summaries: list[dict[str, object]] = []
        for target in record.summary.targets:
            files = self._selected_files_for_targets([str(target)], missing_ok=True)
            summaries.append({"target": str(target), **self._fetch_file_rollup(files)})
        return summaries

    @staticmethod
    def _fetch_next_action(
        summary: FetchSummary,
        file_rollup: dict[str, int],
    ) -> tuple[str, str]:
        if summary.state == FetchState.DRAFT:
            if not summary.targets:
                return "add_targets", "add at least one target selector before starting"
            if file_rollup["files"] <= 0:
                return "edit_targets", "current target selectors match no files"
            if file_rollup["missing_files"] <= 0:
                return "already_hot", "all selected files are already hot"
            if file_rollup["missing_without_disc_files"] > 0:
                return "start_archive", "some missing files have no registered disc coverage"
            return "start_djdan", "missing files have registered disc coverage"
        if summary.state == FetchState.QUEUED_DJDAN:
            return "run_djdan_fetch", "queued for optical media recovery"
        if summary.state == FetchState.UPLOADING:
            return "continue_djdan_fetch", "optical media upload is in progress"
        if summary.state == FetchState.VERIFYING:
            return "wait", "server-side verification is in progress"
        if summary.state in {FetchState.QUEUED_ARCHIVE, FetchState.RESTORING_ARCHIVE}:
            return "monitor_archive_restore", "archive materialization is active"
        if summary.state == FetchState.DONE:
            return "done", "all selected files are hot"
        if summary.state == FetchState.FAILED:
            return "inspect_failure", "fetch failed and needs operator review"
        return "inspect", f"fetch is in state {summary.state.value}"

    def _replace_targets(self, record: FetchRecord, targets: list[str]) -> None:
        selected = self._selected_files_for_targets(targets, missing_ok=True)
        record.entries = self._entries_for_files(selected)
        summary = record.summary
        record.summary = FetchSummary(
            id=summary.id,
            name=summary.name,
            targets=tuple(TargetStr(target) for target in targets),
            state=summary.state,
            files=len(record.entries),
            bytes=sum(entry.bytes for entry in record.entries.values()),
            discs=self._summary_discs(record.entries.values()),
        )
        record.summary = self._replace_summary(record)

    @staticmethod
    def _entries_for_files(files: list[StoredFile]) -> dict[EntryId, FetchEntryRecord]:
        return {
            EntryId(f"e{index}"): FetchEntryRecord(
                id=EntryId(f"e{index}"),
                collection_id=record.collection_id,
                path=record.path,
                bytes=record.bytes,
                sha256=record.sha256,
                content=record.content,
                discs=list(record.discs),
            )
            for index, record in enumerate(
                sorted(files, key=lambda item: (str(item.collection_id), item.path)),
                start=1,
            )
        }

    @staticmethod
    def _require_editable(record: FetchRecord) -> None:
        if record.summary.state != FetchState.DRAFT:
            raise InvalidState(f"fetch cannot be edited from state {record.summary.state.value}")

    @staticmethod
    def _require_djdan_fetch(record: FetchRecord) -> None:
        if record.summary.state == FetchState.DONE:
            return
        if record.summary.state not in {
            FetchState.QUEUED_DJDAN,
            FetchState.UPLOADING,
            FetchState.VERIFYING,
        }:
            raise InvalidState("fetch is not queued for djdan")

    def _record(self, fetch_id: str) -> FetchRecord:
        try:
            return self.state.fetches[FetchId(fetch_id)]
        except KeyError as exc:
            raise NotFound(f"fetch not found: {fetch_id}") from exc

    def _active_archive_restore_id(self, fetch: FetchRecord) -> str | None:
        paths_by_collection: dict[CollectionId, set[str]] = {}
        for entry in fetch.entries.values():
            paths_by_collection.setdefault(entry.collection_id, set()).add(entry.path)
        active_states = {
            ArchiveRestoreState.REQUESTED,
            ArchiveRestoreState.READY,
            ArchiveRestoreState.PAUSED,
        }
        for record in sorted(
            self.state.archive_restores_by_id.values(),
            key=lambda item: item.created_at,
            reverse=True,
        ):
            if record.type != "fetch_materialization" or record.state not in active_states:
                continue
            for collection_id in record.collection_ids:
                fetch_paths = paths_by_collection.get(collection_id)
                if not fetch_paths:
                    continue
                if record.paths is None or set(record.paths) & fetch_paths:
                    return record.restore_id
        return None

    def _raise_if_restoring_archive(self, fetch: FetchRecord) -> None:
        restore_id = self._active_archive_restore_id(fetch)
        if restore_id is None:
            return
        raise InvalidState(
            f"fetch {fetch.summary.id} is actively being fetch materializationed by {restore_id}; "
            "wait for the archive restore to finish before running djdan fetch"
        )

    def _replace_summary(
        self, record: FetchRecord, *, state: FetchState | None = None
    ) -> FetchSummary:
        summary = record.summary
        entries = list(record.entries.values())
        effective_state = state or summary.state
        entries_total = len(entries)
        entries_pending = sum(
            1
            for entry in entries
            if self._entry_upload_state(entry, fetch_state=effective_state) == "pending"
        )
        entries_partial = sum(
            1
            for entry in entries
            if self._entry_upload_state(entry, fetch_state=effective_state) == "partial"
        )
        entries_byte_complete = sum(
            1
            for entry in entries
            if self._entry_upload_state(entry, fetch_state=effective_state) == "byte_complete"
        )
        entries_uploaded = sum(
            1
            for entry in entries
            if self._entry_upload_state(entry, fetch_state=effective_state) == "uploaded"
        )
        uploaded_bytes = sum(entry.uploaded_bytes for entry in entries)
        missing_bytes = max(
            sum(self._entry_recovery_bytes(entry) for entry in entries) - uploaded_bytes, 0
        )
        upload_expiries = [
            entry.upload_expires_at for entry in entries if entry.upload_expires_at is not None
        ]
        return FetchSummary(
            id=summary.id,
            name=summary.name,
            targets=summary.targets,
            state=effective_state,
            files=summary.files,
            bytes=summary.bytes,
            discs=list(summary.discs),
            entries_total=entries_total,
            entries_pending=entries_pending,
            entries_partial=entries_partial,
            entries_byte_complete=entries_byte_complete,
            entries_uploaded=entries_uploaded,
            uploaded_bytes=uploaded_bytes,
            missing_bytes=missing_bytes,
            upload_state_expires_at=max(upload_expiries) if upload_expiries else None,
        )

    def _expire_stale_upload_record(self, record: FetchRecord) -> None:
        now = datetime.now(UTC)
        expired = False
        for entry in record.entries.values():
            if entry.upload_expires_at is None:
                continue
            expires_at = datetime.fromisoformat(entry.upload_expires_at.replace("Z", "+00:00"))
            if expires_at > now:
                continue
            expired = True
            entry.upload_url = None
            entry.uploaded_bytes = 0
            entry.uploaded_content = None
            entry.upload_expires_at = None
        if expired and record.summary.state == FetchState.UPLOADING:
            record.summary = self._replace_summary(record, state=FetchState.QUEUED_DJDAN)
        else:
            record.summary = self._replace_summary(record)

    def _entry_upload_state(self, entry: FetchEntryRecord, *, fetch_state: FetchState) -> str:
        if (
            entry.uploaded_bytes >= self._entry_recovery_bytes(entry)
            and self._entry_recovery_bytes(entry) > 0
        ):
            if fetch_state == FetchState.DONE:
                return "uploaded"
            return "byte_complete"
        if entry.uploaded_bytes > 0:
            return "partial"
        return "pending"

    def _entry_upload_payload(self, entry: FetchEntryRecord) -> dict[str, object]:
        return {
            "entry": str(entry.id),
            "protocol": "tus",
            "upload_url": entry.upload_url,
            "offset": entry.uploaded_bytes,
            "length": self._entry_recovery_bytes(entry),
            "checksum_algorithm": "sha256",
            "expires_at": entry.upload_expires_at,
        }

    @staticmethod
    def _summary_discs(entries: Iterator[FetchEntryRecord]) -> list[FetchDiscHint]:
        out: list[FetchDiscHint] = []
        seen: set[tuple[str, DiscId]] = set()
        for entry in entries:
            for disc in entry.discs:
                key = (disc.image_id, disc.disc_id)
                if key in seen:
                    continue
                seen.add(key)
                out.append(disc.hint)
        return out

    def _manifest_disc(self, entry: FetchEntryRecord, disc: FileDisc) -> dict[str, object]:
        recovery_payload = self._disc_recovery_payload(entry, disc)
        return {
            "disc_id": str(disc.disc_id),
            "image_id": disc.image_id,
            "location": disc.location,
            "disc_path": disc.disc_path,
            "recovery_bytes": len(recovery_payload),
            "recovery_sha256": hashlib.sha256(recovery_payload).hexdigest(),
        }

    def _manifest_parts(self, entry: FetchEntryRecord) -> list[dict[str, object]]:
        if not entry.discs:
            return []

        if all(disc.part_index is None for disc in entry.discs):
            return [
                {
                    "index": 0,
                    "bytes": entry.bytes,
                    "sha256": str(entry.sha256),
                    "recovery_bytes": self._entry_recovery_bytes(entry),
                    "discs": [self._manifest_disc(entry, disc) for disc in entry.discs],
                }
            ]

        part_count = max((disc.part_count or 1) for disc in entry.discs)
        parts: list[dict[str, object]] = []
        for part_index in range(part_count):
            part_discs = [disc for disc in entry.discs if disc.part_index == part_index]
            if not part_discs:
                raise NotFound(f"missing disc hints for part {part_index} of entry {entry.id}")
            bytes_hint = part_discs[0].part_bytes
            sha256_hint = part_discs[0].part_sha256
            if bytes_hint is None or sha256_hint is None:
                raise NotFound(f"missing part metadata for part {part_index} of entry {entry.id}")
            parts.append(
                {
                    "index": part_index,
                    "bytes": bytes_hint,
                    "sha256": sha256_hint,
                    "recovery_bytes": len(self._disc_recovery_payload(entry, part_discs[0])),
                    "discs": [self._manifest_disc(entry, disc) for disc in part_discs],
                }
            )
        return parts

    def _entry_recovery_payloads(self, entry: FetchEntryRecord) -> tuple[bytes, ...]:
        if not entry.discs or all(disc.part_index is None for disc in entry.discs):
            return (fixture_encrypt_bytes(entry.content),)
        part_count = max((disc.part_count or 1) for disc in entry.discs)
        return tuple(
            fixture_encrypt_bytes(part)
            for part in split_fixture_plaintext(entry.content, part_count)
        )

    def _entry_recovery_bytes(self, entry: FetchEntryRecord) -> int:
        return sum(len(payload) for payload in self._entry_recovery_payloads(entry))

    def _disc_recovery_payload(self, entry: FetchEntryRecord, disc: FileDisc) -> bytes:
        payloads = self._entry_recovery_payloads(entry)
        if disc.part_index is None:
            return payloads[0]
        return payloads[disc.part_index]

    def _verify_uploaded_entry(self, entry: FetchEntryRecord) -> None:
        if entry.uploaded_content is None:
            raise InvalidState("fetch is missing required entry uploads")
        recovery_payloads = self._entry_recovery_payloads(entry)
        offset = 0
        plaintext_parts: list[bytes] = []
        for recovery_payload in recovery_payloads:
            next_offset = offset + len(recovery_payload)
            chunk = entry.uploaded_content[offset:next_offset]
            if len(chunk) != len(recovery_payload):
                raise HashMismatch(
                    "uploaded recovery stream did not match expected recovery boundaries"
                )
            try:
                plaintext_parts.append(fixture_decrypt_bytes(chunk))
            except ValueError as exc:
                raise HashMismatch("uploaded recovery bytes did not decrypt cleanly") from exc
            offset = next_offset
        if offset != len(entry.uploaded_content):
            raise HashMismatch("uploaded recovery stream contained trailing bytes")
        actual_sha = hashlib.sha256(b"".join(plaintext_parts)).hexdigest()
        if actual_sha != entry.sha256:
            raise HashMismatch("sha256 did not match expected entry hash")

    def _hot_payload_for_fetch(self, record: FetchRecord) -> dict[str, object]:
        selected = self._selected_files_for_targets(
            [str(target) for target in record.summary.targets],
            missing_ok=True,
        )
        present_bytes = sum(record.bytes for record in selected if record.hot)
        missing_bytes = sum(record.bytes for record in selected if not record.hot)
        return {
            "state": "ready" if missing_bytes == 0 else "waiting",
            "present_bytes": present_bytes,
            "missing_bytes": missing_bytes,
        }


class AcceptanceFileService:
    def __init__(self, state: AcceptanceState) -> None:
        self.state = state

    @_with_state_lock
    def query_by_target(
        self,
        raw_target: str,
        *,
        page: int,
        per_page: int,
    ) -> dict[str, object]:
        from riverhog_core.domain.selectors import parse_target

        target = parse_target(raw_target)
        selected = self.state.selected_files(raw_target, missing_ok=True)
        result = sorted(
            [
                {
                    "target": record.projected_target,
                    "collection": str(record.collection_id),
                    "path": record.path,
                    "bytes": record.bytes,
                    "sha256": str(record.sha256),
                    "hot": record.hot,
                    "disc_coverage": record.disc_coverage,
                }
                for record in selected
            ],
            key=lambda r: str(r["target"]),
        )
        total = len(result)
        pages = math.ceil(total / per_page) if total else 0
        start = (page - 1) * per_page
        return {
            "target": target.canonical,
            "page": page,
            "per_page": per_page,
            "total": total,
            "pages": pages,
            "files": result[start : start + per_page],
        }

    @_with_state_lock
    def get_content(self, raw_target: str) -> bytes:
        from riverhog_core.domain.errors import InvalidTarget, NotFound
        from riverhog_core.domain.selectors import parse_target

        target = parse_target(raw_target)
        if target.is_dir:
            raise InvalidTarget("directory selectors are not supported for content download")
        selected = self.state.selected_files(raw_target, missing_ok=True)
        if len(selected) != 1:
            raise NotFound(f"file not found: {raw_target}")
        record = selected[0]
        if not record.hot:
            raise NotFound(f"file is not hot: {raw_target}")
        return self.state.file_content(record.collection_id, record.path)


def _fixture_real_iso_bytes(*, image_root: Path, image_id: str) -> bytes:
    proc = subprocess.run(
        build_iso_cmd_from_root(image_root=image_root, volume_id=image_id),
        capture_output=True,
        check=False,
    )
    if proc.returncode == 0:
        return proc.stdout
    detail = proc.stderr.decode("utf-8", errors="replace")[-1500:] or (
        f"xorriso exited {proc.returncode}"
    )
    raise RuntimeError(f"acceptance fixture could not build real ISO: {detail}")


class _LiveServerHandle:
    def __init__(self, app: Any, *, host: str, port: int) -> None:
        self._config = uvicorn.Config(app, host=host, port=port, log_level="warning")
        self._server = uvicorn.Server(self._config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self.base_url = f"http://{host}:{port}"

    def start(self) -> None:
        self._thread.start()
        deadline = time.monotonic() + 5.0
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                with httpx.Client(base_url=self.base_url, timeout=0.5) as client:
                    response = client.get("/openapi.json")
                if response.status_code == 200:
                    return
            except Exception as exc:  # pragma: no cover
                last_error = exc
            time.sleep(0.05)
        raise RuntimeError(
            f"Timed out waiting for live riverhog test server at {self.base_url}"
        ) from last_error

    def close(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5.0)
        if self._thread.is_alive():  # pragma: no cover
            raise RuntimeError("Timed out stopping live riverhog test server")


@dataclass(slots=True)
class _PortReservation:
    socket: socket.socket
    port: int

    def close(self) -> None:
        self.socket.close()

    def __enter__(self) -> _PortReservation:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()


def _reserve_local_port() -> _PortReservation:
    reserved = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    reserved.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    reserved.bind(("127.0.0.1", 0))
    reserved.listen(1)
    return _PortReservation(socket=reserved, port=int(reserved.getsockname()[1]))


def _fixture_tus_upload_headers(payload: Mapping[str, object]) -> dict[str, str]:
    headers = {
        "Tus-Resumable": "1.0.0",
        "Upload-Offset": str(payload["offset"]),
        "Upload-Length": str(payload["length"]),
    }
    if payload.get("expires_at") is not None:
        headers["Upload-Expires"] = str(payload["expires_at"])
    return headers


def _install_live_collection_upload_routes(
    app: Any,
    container_slot: _ContainerSlot,
) -> None:
    @app.api_route(
        "/files/collections/{tail:path}",
        methods=["PATCH", "HEAD", "DELETE", "OPTIONS"],
        include_in_schema=False,
    )
    async def direct_collection_upload(
        tail: str,
        request: Request,
    ) -> Response:
        method = request.method.upper()
        raw_path = request.scope.get("raw_path", b"")
        if isinstance(raw_path, bytes):
            parsed_path = raw_path.decode("ascii", errors="ignore")
        else:
            parsed_path = str(raw_path or request.url.path)
        path_tail = parsed_path.removeprefix("/files/collections/")
        encoded_collection_id, separator, encoded_relpath = path_tail.partition("/")
        if not separator:
            return JSONResponse(status_code=404, content={"error": {"code": "not_found"}})
        collection_id = unquote(encoded_collection_id)
        relpath = unquote(encoded_relpath)
        _ = tail
        collections = container_slot.container.collections
        try:
            if method == "PATCH":
                payload = collections.append_upload_chunk(
                    collection_id,
                    relpath,
                    offset=int(request.headers.get("Upload-Offset", "0")),
                    checksum=str(request.headers.get("Upload-Checksum", "")),
                    content=await request.body(),
                )
                return Response(status_code=204, headers=_fixture_tus_upload_headers(payload))
            if method == "HEAD":
                payload = collections.get_file_upload(collection_id, relpath)
                return Response(status_code=204, headers=_fixture_tus_upload_headers(payload))
            if method == "DELETE":
                collections.cancel_file_upload(collection_id, relpath)
                return Response(status_code=204, headers={"Tus-Resumable": "1.0.0"})
            return Response(
                status_code=204,
                headers={
                    "Tus-Resumable": "1.0.0",
                    "Tus-Version": "1.0.0",
                    "Tus-Extension": "checksum,expiration,termination",
                    "Tus-Checksum-Algorithm": "sha256",
                },
            )
        except Exception as exc:
            return JSONResponse(
                status_code=400,
                content={"error": {"code": "bad_request", "message": str(exc)}},
            )


def _timeout_text(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = value
    if len(text) <= _CLI_TIMEOUT_OUTPUT_CHARS:
        return text
    return text[:_CLI_TIMEOUT_OUTPUT_CHARS] + "\n... output truncated ..."


def _set_or_restore_env(key: str, value: str | None) -> None:
    if value is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = value


@dataclass(slots=True)
class AcceptanceSystem:
    workspace: Path
    state: AcceptanceState
    app: Any
    server: _LiveServerHandle
    base_url: str
    fixture_path: Path
    collections: AcceptanceCollectionService
    search: AcceptanceSearchService
    planning: AcceptancePlanningService
    archive_uploads: AcceptanceArchiveUploadService
    archive_reporting: AcceptanceArchiveReportingService
    archive_restores: AcceptanceArchiveRestoreService
    discs: AcceptanceDiscService
    fetches: AcceptanceFetchService
    files: AcceptanceFileService
    _container_slot: _ContainerSlot
    _runtime_env_overrides: dict[str, str | None]

    @classmethod
    def create(cls, workspace: Path) -> AcceptanceSystem:
        with time_block("fixture.acceptance_system.create"):
            _clear_workspace(workspace)
            state = AcceptanceState()
            system = cls._build_runtime(workspace, state)
            system.server.start()
            system._apply_live_server_env()
            system.state.public_base_url = system.server.base_url
            return system

    @classmethod
    def _build_runtime(cls, workspace: Path, state: AcceptanceState) -> AcceptanceSystem:
        collections = AcceptanceCollectionService(state)
        search = AcceptanceSearchService(state)
        planning = AcceptancePlanningService(state)
        archive_uploads = AcceptanceArchiveUploadService(state)
        archive_reporting = AcceptanceArchiveReportingService(state)
        archive_restores = AcceptanceArchiveRestoreService(state)
        discs = AcceptanceDiscService(state)
        fetches = AcceptanceFetchService(state)
        files = AcceptanceFileService(state)

        container = ServiceContainer(
            collections=collections,
            search=search,
            planning=planning,
            archive_uploads=archive_uploads,
            archive_reporting=archive_reporting,
            archive_restores=archive_restores,
            discs=discs,
            fetches=fetches,
            files=files,
        )
        container_slot = _ContainerSlot(container=container)
        app = create_app(
            container_provider=lambda: container_slot.container,
            upload_expiry_reaper_interval=_UPLOAD_EXPIRY_SWEEP_INTERVAL_SECONDS,
            archive_upload_reaper_interval=_ARCHIVE_UPLOAD_SWEEP_INTERVAL_SECONDS,
            archive_restore_reaper_interval=_ARCHIVE_RESTORE_SWEEP_INTERVAL_SECONDS,
        )
        _install_live_collection_upload_routes(app, container_slot)
        fixture_path = workspace / "djdan_fixture.json"
        with _reserve_local_port() as reserved:
            server = _LiveServerHandle(app, host="127.0.0.1", port=reserved.port)
        return cls(
            workspace=workspace,
            state=state,
            app=app,
            server=server,
            base_url=server.base_url,
            fixture_path=fixture_path,
            collections=collections,
            search=search,
            planning=planning,
            archive_uploads=archive_uploads,
            archive_reporting=archive_reporting,
            archive_restores=archive_restores,
            discs=discs,
            fetches=fetches,
            files=files,
            _container_slot=container_slot,
            _runtime_env_overrides={},
        )

    def _apply_live_server_env(self) -> None:
        updates = {
            "RIVERHOG_TUSD_BASE_URL": f"{self.base_url}/files",
            "RIVERHOG_TUSD_PUBLIC_BASE_URL": f"{self.base_url}/files",
        }
        for key, value in updates.items():
            if key not in self._runtime_env_overrides:
                self._runtime_env_overrides[key] = os.environ.get(key)
            os.environ[key] = value

    def _restore_live_server_env(self) -> None:
        for key, value in self._runtime_env_overrides.items():
            _set_or_restore_env(key, value)
        self._runtime_env_overrides.clear()

    def restart(self) -> None:
        with time_block("fixture.acceptance_system.restart"):
            state = self.state
            self.server.close()
            restarted = self._build_runtime(self.workspace, state)
            restarted.server.start()
            restarted.state.public_base_url = restarted.server.base_url
            self.app = restarted.app
            self.server = restarted.server
            self.base_url = restarted.base_url
            self._apply_live_server_env()
            self.collections = restarted.collections
            self.search = restarted.search
            self.planning = restarted.planning
            self.archive_uploads = restarted.archive_uploads
            self.archive_reporting = restarted.archive_reporting
            self.archive_restores = restarted.archive_restores
            self.discs = restarted.discs
            self.fetches = restarted.fetches
            self.files = restarted.files
            self._container_slot = restarted._container_slot

    def reset(self) -> None:
        with time_block("fixture.acceptance_system.reset"):
            _clear_workspace(self.workspace)
            reset = self._build_runtime(self.workspace, AcceptanceState())
            reset.state.public_base_url = self.base_url
            self.state = reset.state
            self.collections = reset.collections
            self.search = reset.search
            self.planning = reset.planning
            self.archive_uploads = reset.archive_uploads
            self.archive_reporting = reset.archive_reporting
            self.archive_restores = reset.archive_restores
            self.discs = reset.discs
            self.fetches = reset.fetches
            self.files = reset.files
            self._container_slot.container = reset._container_slot.container

    def close(self) -> None:
        with time_block("fixture.acceptance_system.close"):
            try:
                self.server.close()
            finally:
                self._restore_live_server_env()

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, object] | None = None,
        json_body: Mapping[str, object] | None = None,
        headers: Mapping[str, str] | None = None,
        content: bytes | None = None,
    ) -> httpx.Response:
        direct_upload = self._direct_collection_upload_request(
            method,
            path,
            headers=headers,
            content=content,
        )
        if direct_upload is not None:
            return direct_upload
        with time_block(f"http {method} {path}"):
            for attempt in range(3):
                try:
                    with httpx.Client(base_url=self.base_url, timeout=5.0) as client:
                        return client.request(
                            method,
                            path,
                            params=params,
                            json=json_body,
                            headers=headers,
                            content=content,
                        )
                except httpx.RemoteProtocolError:
                    if not path.endswith("/iso") or attempt == 2:
                        raise
                    time.sleep(0.05)
        raise RuntimeError("unreachable")

    def _direct_collection_upload_request(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | None,
        content: bytes | None,
    ) -> httpx.Response | None:
        parsed_path = urlsplit(path).path
        prefix = "/files/collections/"
        if not parsed_path.startswith(prefix):
            return None
        tail = parsed_path.removeprefix(prefix)
        encoded_collection_id, separator, encoded_relpath = tail.partition("/")
        if not separator:
            return httpx.Response(404, json={"error": {"code": "not_found"}})
        collection_id = unquote(encoded_collection_id)
        relpath = unquote(encoded_relpath)
        method = method.upper()
        try:
            if method == "PATCH":
                request_headers = headers or {}
                payload = self.collections.append_upload_chunk(
                    collection_id,
                    relpath,
                    offset=int(request_headers.get("Upload-Offset", "0")),
                    checksum=str(request_headers.get("Upload-Checksum", "")),
                    content=content or b"",
                )
                return httpx.Response(204, headers=_fixture_tus_upload_headers(payload))
            if method == "HEAD":
                payload = self.collections.get_file_upload(collection_id, relpath)
                return httpx.Response(204, headers=_fixture_tus_upload_headers(payload))
            if method == "DELETE":
                self.collections.cancel_file_upload(collection_id, relpath)
                return httpx.Response(204, headers={"Tus-Resumable": "1.0.0"})
            if method == "OPTIONS":
                return httpx.Response(
                    204,
                    headers={
                        "Tus-Resumable": "1.0.0",
                        "Tus-Version": "1.0.0",
                        "Tus-Extension": "checksum,expiration,termination",
                        "Tus-Checksum-Algorithm": "sha256",
                    },
                )
            return httpx.Response(405)
        except Exception as exc:
            return httpx.Response(
                400,
                json={"error": {"code": "bad_request", "message": str(exc)}},
            )

    def seed_finalized_image(self, candidate_id: str, *, force_ready: bool = False) -> None:
        with self.state.lock:
            candidate_key = ImageId(candidate_id)
            candidate = self.state.candidates_by_id[candidate_key]
            if force_ready and not candidate.iso_ready:
                candidate = CandidateRecord(
                    candidate_id=candidate.candidate_id,
                    finalized_id=candidate.finalized_id,
                    filename=candidate.filename,
                    image_root=candidate.image_root,
                    bytes=candidate.bytes,
                    iso_ready=True,
                    covered_paths=candidate.covered_paths,
                )
                self.state.candidates_by_id[candidate_key] = candidate
            self.state.finalized_images_by_id[ImageId(candidate.finalized_id)] = candidate

    def ensure_disc_rebuild_restore(self, *, restore_id: str, image_id: str) -> None:
        with self.state.lock:
            image = self.state.finalized_images_by_id[ImageId(image_id)]
            collection_ids = tuple(
                sorted({collection_id for collection_id, _path in image.covered_paths})
            )
            for collection_id in collection_ids:
                if (
                    self.state.collection_archive_status(str(collection_id)).state
                    != ArchiveState.UPLOADED
                ):
                    self.state.mark_collection_archive_uploaded(str(collection_id))
            self.state.archive_restores_by_id[restore_id] = AcceptanceArchiveRestoreRecord(
                restore_id=restore_id,
                image_id=ImageId(image_id),
                state=ArchiveRestoreState.REQUESTED,
                created_at=_acceptance_isoformat(datetime.now(UTC)),
                type="disc_rebuild",
                image_ids=(ImageId(image_id),),
                collection_ids=collection_ids,
                latest_message=(
                    "Archive restore queued; Riverhog will restore collection data from "
                    "the archive."
                ),
            )

    def wait_for_collection_archive_state(
        self,
        collection_id: str,
        state: str,
        *,
        timeout: float = 5.0,
    ) -> dict[str, object]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            response = self.request(
                "GET",
                f"/v1/collections/{quote(normalize_collection_id(collection_id), safe='/')}",
            )
            if response.status_code == 200:
                payload = response.json()
                if payload["archive"]["state"] == state:
                    return payload
            with self.state.lock:
                status = self.state.collection_archive_status(collection_id)
            if status.state.value == state:
                return {"id": normalize_collection_id(collection_id), "archive": {"state": state}}
            time.sleep(0.05)
        raise AssertionError(
            f"timed out waiting for collection archive state {collection_id} -> {state}"
        )

    def wait_for_collection_upload_state(
        self,
        collection_id: str,
        state: str,
        *,
        timeout: float = 5.0,
    ) -> dict[str, object]:
        normalized_collection_id = normalize_collection_id(collection_id)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            response = self.request(
                "GET",
                f"/v1/collection-uploads/{quote(normalized_collection_id, safe='/')}",
            )
            if response.status_code == 200:
                payload = response.json()
                if payload["state"] == state:
                    return payload
            if state == "finalized":
                collection_response = self.request(
                    "GET",
                    f"/v1/collections/{quote(normalized_collection_id, safe='/')}",
                )
                if collection_response.status_code == 200:
                    return {
                        "collection_id": normalized_collection_id,
                        "state": "finalized",
                        "collection": collection_response.json(),
                    }
            time.sleep(0.05)
        raise AssertionError(
            f"timed out waiting for collection upload state {collection_id} -> {state}"
        )

    def defer_collection_archive_archiving(
        self,
        collection_id: str,
        *,
        seconds: float = 1.0,
    ) -> None:
        normalized_collection_id = normalize_collection_id(collection_id)
        with self.state.lock:
            self.state.archive_upload_defer_until_by_collection[
                CollectionId(normalized_collection_id)
            ] = time.monotonic() + seconds

    def wait_for_archive_restore_state(
        self,
        restore_id: str,
        state: str,
        *,
        timeout: float = 5.0,
    ) -> dict[str, object]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            response = self.request("GET", f"/v1/archive-restores/{restore_id}")
            assert response.status_code == 200, response.text
            payload = response.json()
            if payload["state"] == state:
                return payload
            time.sleep(0.05)
        raise AssertionError(f"timed out waiting for archive restore state {restore_id} -> {state}")

    def list_webhook_deliveries(self) -> list[dict[str, object]]:
        return self.state.list_webhook_deliveries()

    def list_webhook_attempts(self) -> list[dict[str, object]]:
        return self.state.list_webhook_attempts()

    def configure_webhook_failure(
        self,
        event: str,
        *,
        status_code: int = 503,
        remaining: int = 1,
        delay_seconds: float = 0.0,
        mode: str = "status",
    ) -> None:
        self.state.add_webhook_behavior(
            event=event,
            status_code=status_code,
            remaining=remaining,
            delay_seconds=delay_seconds,
            mode=mode,
        )

    def wait_for_webhook_event(
        self,
        event: str,
        *,
        delivery: int = 1,
        timeout: float = 5.0,
    ) -> dict[str, object]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            matches = [
                payload
                for payload in self.list_webhook_deliveries()
                if str(payload.get("event")) == event
            ]
            if len(matches) >= delivery:
                return matches[delivery - 1]
            time.sleep(0.05)
        raise AssertionError(f"timed out waiting for captured webhook event {event} #{delivery}")

    def wait_for_webhook_attempt(
        self,
        event: str,
        *,
        result: str,
        attempt: int = 1,
        timeout: float = 5.0,
    ) -> dict[str, object]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            matches = [
                payload
                for payload in self.list_webhook_attempts()
                if str(payload.get("event")) == event and str(payload.get("result")) == result
            ]
            if len(matches) >= attempt:
                return matches[attempt - 1]
            time.sleep(0.05)
        raise AssertionError(
            f"timed out waiting for captured webhook attempt {event} {result} #{attempt}"
        )

    def fail_collection_archive_upload(self, collection_id: str, *, error: str) -> None:
        with self.state.lock:
            self.state.archive_upload_failures_by_collection[
                CollectionId(normalize_collection_id(collection_id))
            ] = error

    def mark_collection_archive_uploaded(self, collection_id: str) -> None:
        with self.state.lock:
            self.state.mark_collection_archive_uploaded(collection_id)

    def collection_archive_failure_configured(self, collection_id: str) -> bool:
        with self.state.lock:
            return (
                CollectionId(normalize_collection_id(collection_id))
                in self.state.archive_upload_failures_by_collection
            )

    def collection_archive_failure(self, collection_id: str) -> str:
        with self.state.lock:
            return self.state.archive_upload_failures_by_collection[
                CollectionId(normalize_collection_id(collection_id))
            ]

    def start_collection_archive_archiving(self, collection_id: str) -> None:
        normalized_collection_id = normalize_collection_id(collection_id)
        with self.state.lock:
            if CollectionId(normalized_collection_id) not in self.state.files_by_collection:
                raise NotFound(f"collection not found: {normalized_collection_id}")
        self.seed_collection_source(
            normalized_collection_id,
            {
                record.path: record.content
                for record in self.state.collection_files(normalized_collection_id)
            },
        )
        with self.state.lock:
            records = self.state.collection_files(normalized_collection_id)
            self.state.collection_uploads[CollectionId(normalized_collection_id)] = (
                CollectionUploadRecord(
                    collection_id=CollectionId(normalized_collection_id),
                    ingest_source=None,
                    files={
                        record.path: CollectionUploadFileRecord(
                            path=record.path,
                            bytes=record.bytes,
                            sha256=record.sha256,
                            uploaded_bytes=record.bytes,
                            uploaded_content=record.content,
                        )
                        for record in records
                    },
                    state="archiving",
                )
            )

    def clear_collection_archive_upload_failure(self, collection_id: str) -> None:
        with self.state.lock:
            self.state.archive_upload_failures_by_collection.pop(
                CollectionId(normalize_collection_id(collection_id)),
                None,
            )

    def enable_real_iso_streams(self) -> None:
        with self.state.lock:
            self.state.real_iso_streams_enabled = True

    def run_riverhog(self, *args: str) -> subprocess.CompletedProcess[str]:
        with time_block("subprocess riverhog"):
            try:
                return subprocess.run(
                    [sys.executable, "-m", "riverhog_cli.main", *args],
                    cwd=REPO_ROOT,
                    env=self._subprocess_env(),
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=_CLI_SUBPROCESS_TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired as exc:
                command_text = " ".join([sys.executable, "-m", "riverhog_cli.main", *args])
                stdout = _timeout_text(exc.stdout)
                stderr = _timeout_text(exc.stderr)
                raise AssertionError(
                    "riverhog subprocess timed out after "
                    f"{_CLI_SUBPROCESS_TIMEOUT_SECONDS:.0f}s\n"
                    f"command: {command_text}\n"
                    f"stdout:\n{stdout}\n"
                    f"stderr:\n{stderr}"
                ) from exc

    def run_djdan(
        self, *args: str, input_text: str = "\n" * 16
    ) -> subprocess.CompletedProcess[str]:
        if not self.fixture_path.exists():
            self._write_djdan_fixture(self._default_djdan_fixture())
        env = self._subprocess_env(
            {
                "DJDAN_FIXTURE_PATH": str(self.fixture_path),
                "DJDAN_READER_FACTORY": "tests.fixtures.djdan_fakes:FixtureOpticalReader",
                "DJDAN_ISO_VERIFIER_FACTORY": "tests.fixtures.djdan_fakes:FixtureIsoVerifier",
                "DJDAN_BURNER_FACTORY": "tests.fixtures.djdan_fakes:FixtureDiscBurner",
                "DJDAN_BURNED_MEDIA_VERIFIER_FACTORY": (
                    "tests.fixtures.djdan_fakes:FixtureBurnedMediaVerifier"
                ),
                "DJDAN_BURN_PROMPTS_FACTORY": "tests.fixtures.djdan_fakes:FixtureBurnPrompts",
                "DJDAN_STAGING_DIR": str(self.workspace / "djdan_staging"),
            }
        )
        with time_block("subprocess djdan"):
            return subprocess.run(
                [sys.executable, "-m", "djdan.main", *args],
                cwd=REPO_ROOT,
                env=env,
                input=input_text,
                capture_output=True,
                text=True,
                check=False,
            )

    def delete_hot_backing_file(self, target: str) -> None:
        with self.state.lock:
            selected = self.state.selected_files(target)
            if len(selected) != 1:
                raise AssertionError(f"expected exactly one file target: {target}")
            selected[0].hot_backing_missing = True

    def has_committed_collection_file(self, collection_id: str, path: str) -> bool:
        with self.state.lock:
            normalized_collection_id = CollectionId(normalize_collection_id(collection_id))
            records = self.state.files_by_collection.get(normalized_collection_id)
            if records is None:
                return False
            record = records.get(path)
            return bool(record and record.hot and not record.hot_backing_missing)

    def collection_source_root(self, collection_id: str) -> Path:
        with self.state.lock:
            collection_key = CollectionId(normalize_collection_id(collection_id))
            return self.state.local_collection_sources[collection_key]

    def inspect_downloaded_iso(self, *, image_id: str, iso_bytes: bytes) -> InspectedIso:
        with self.state.lock:
            image = self.state.finalized_images_by_id.get(ImageId(image_id))
        if image is None:
            raise AssertionError(f"image not found for inspection: {image_id}")
        return inspect_fixture_image_root(
            image_id=image_id,
            image_root=image.image_root,
            iso_bytes=iso_bytes,
            workspace=self.workspace,
        )

    def expire_collection_upload(self, collection_id: str) -> None:
        with self.state.lock:
            upload = self.state.collection_uploads[
                CollectionId(normalize_collection_id(collection_id))
            ]
            for file_record in upload.files.values():
                file_record.upload_expires_at = "2000-01-01T00:00:00Z"

    def expire_fetch_upload(self, fetch_id: str, entry_id: str) -> None:
        with self.state.lock:
            record = self.state.fetches[FetchId(fetch_id)]
            entry = record.entries[EntryId(entry_id)]
            entry.upload_expires_at = "2000-01-01T00:00:00Z"

    def wait_for_collection_upload_cleanup(self, collection_id: str, timeout: float = 2.0) -> None:
        normalized_collection_id = CollectionId(normalize_collection_id(collection_id))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.state.lock:
                if normalized_collection_id not in self.state.collection_uploads:
                    return
            time.sleep(0.05)
        raise AssertionError(f"timed out waiting for collection upload cleanup: {collection_id}")

    def wait_for_fetch_upload_cleanup(
        self,
        fetch_id: str,
        entry_id: str,
        timeout: float = 2.0,
    ) -> None:
        fetch_key = FetchId(fetch_id)
        entry_key = EntryId(entry_id)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.state.lock:
                record = self.state.fetches.get(fetch_key)
                if record is None:
                    raise AssertionError(f"fetch not found while waiting for cleanup: {fetch_id}")
                entry = record.entries.get(entry_key)
                if entry is None:
                    raise AssertionError(
                        f"entry not found while waiting for cleanup: {fetch_id}/{entry_id}"
                    )
                if (
                    entry.upload_url is None
                    and entry.uploaded_bytes == 0
                    and entry.uploaded_content is None
                    and entry.upload_expires_at is None
                ):
                    return
            time.sleep(0.05)
        raise AssertionError(f"timed out waiting for fetch upload cleanup: {fetch_id}/{entry_id}")

    def seed_collection_source(
        self, collection_id: str, files: Mapping[str, bytes] | None = None
    ) -> None:
        with time_block("fixture.seed_collection_source"):
            normalized_collection_id = normalize_collection_id(collection_id)
            root = write_tree(
                self.workspace / "collections-src" / normalized_collection_id,
                files or PHOTOS_2024_FILES,
            )
            self.state.register_local_collection_source(normalized_collection_id, root)

    def upload_collection_source(
        self, collection_id: str, files: Mapping[str, bytes] | None = None
    ) -> dict[str, object]:
        with time_block("fixture.upload_collection_source"):
            normalized_collection_id = normalize_collection_id(collection_id)
            source_files = files or PHOTOS_2024_FILES
            self.seed_collection_source(normalized_collection_id, source_files)
            with self.state.lock:
                root = self.state.local_collection_sources[CollectionId(normalized_collection_id)]
            manifest = []
            for path, content in sorted(source_files.items()):
                manifest.append(
                    {
                        "path": path,
                        "bytes": len(content),
                        "sha256": hashlib.sha256(content).hexdigest(),
                    }
                )
            response = self.request(
                "POST",
                "/v1/collection-uploads",
                json_body={
                    "slug": normalized_collection_id,
                    "ingest_source": str(root),
                    "files": manifest,
                },
            )
            assert response.status_code == 200, response.text
            payload = response.json()
            minted_collection_id = str(payload["collection_id"])
            for file_payload in payload["files"]:
                upload = self.request(
                    "POST",
                    (
                        f"/v1/collection-uploads/{minted_collection_id}/files/"
                        f"{file_payload['path']}/upload"
                    ),
                )
                assert upload.status_code == 200, upload.text
                upload_payload = upload.json()
                content = source_files[str(file_payload["path"])]
                response = self.request(
                    "PATCH",
                    str(upload_payload["upload_url"]),
                    headers={
                        "Content-Type": "application/offset+octet-stream",
                        "Tus-Resumable": "1.0.0",
                        "Upload-Offset": str(upload_payload["offset"]),
                        "Upload-Checksum": "sha256 "
                        + base64.b64encode(hashlib.sha256(content).digest()).decode("ascii"),
                    },
                    content=content,
                )
                assert response.status_code == 204, response.text
            final = self.wait_for_collection_upload_state(minted_collection_id, "finalized")
            return cast(dict[str, object], final["collection"])

    def stage_collection_upload_archiving(
        self, collection_id: str, files: Mapping[str, bytes] | None = None
    ) -> dict[str, object]:
        with time_block("fixture.stage_collection_upload_archiving"):
            normalized_collection_id = normalize_collection_id(collection_id)
            source_files = files or PHOTOS_2024_FILES
            self.seed_collection_source(normalized_collection_id, source_files)
            with self.state.lock:
                root = self.state.local_collection_sources[CollectionId(normalized_collection_id)]
            manifest = [
                {
                    "path": path,
                    "bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
                for path, content in sorted(source_files.items())
            ]
            response = self.request(
                "POST",
                "/v1/collection-uploads",
                json_body={
                    "slug": normalized_collection_id,
                    "ingest_source": str(root),
                    "files": manifest,
                },
            )
            assert response.status_code == 200, response.text
            payload = response.json()
            minted_collection_id = str(payload["collection_id"])
            for file_payload in payload["files"]:
                upload = self.request(
                    "POST",
                    (
                        f"/v1/collection-uploads/{minted_collection_id}/files/"
                        f"{file_payload['path']}/upload"
                    ),
                )
                assert upload.status_code == 200, upload.text
                upload_payload = upload.json()
                content = source_files[str(file_payload["path"])]
                response = self.request(
                    "PATCH",
                    str(upload_payload["upload_url"]),
                    headers={
                        "Content-Type": "application/offset+octet-stream",
                        "Tus-Resumable": "1.0.0",
                        "Upload-Offset": str(upload_payload["offset"]),
                        "Upload-Checksum": "sha256 "
                        + base64.b64encode(hashlib.sha256(content).digest()).decode("ascii"),
                    },
                    content=content,
                )
                assert response.status_code == 204, response.text
            payload = self.wait_for_collection_upload_state(minted_collection_id, "archiving")
            self.defer_collection_archive_archiving(minted_collection_id)
            return payload

    def seed_photos_hot(self) -> None:
        self._seed_hot_fixture_collection(PHOTOS_COLLECTION_ID, PHOTOS_2024_FILES)

    def seed_nested_photos_hot(self) -> None:
        self._seed_hot_fixture_collection(PHOTOS_NESTED_COLLECTION_ID, PHOTOS_2024_FILES)

    def seed_parent_photos_hot(self) -> None:
        self._seed_hot_fixture_collection(PHOTOS_PARENT_COLLECTION_ID, PHOTOS_2024_FILES)

    def seed_docs_hot(self) -> None:
        self._seed_hot_fixture_collection(DOCS_COLLECTION_ID, DOCS_FILES)

    def _seed_hot_fixture_collection(self, collection_id: str, files: Mapping[str, bytes]) -> None:
        normalized = normalize_collection_id(collection_id)
        collection_key = CollectionId(normalized)
        with self.state.lock:
            if collection_key in self.state.files_by_collection:
                return
        self.seed_collection_source(normalized, files)
        self.state.seed_collection(
            normalized,
            files,
            hot_paths={path.lstrip("/") for path in files},
        )

    def seed_docs_archive(self) -> None:
        docs_key = CollectionId(normalize_collection_id(DOCS_COLLECTION_ID))
        with self.state.lock:
            docs = self.state.files_by_collection.get(docs_key, {})
            invoice = docs.get("tax/2022/invoice-123.pdf")
            if invoice and invoice.disc_coverage:
                return
        self.seed_docs_hot()
        self.seed_image_fixtures((IMAGE_FIXTURES[0],))
        resp = self.request(
            "POST", f"/v1/plan/candidates/{IMAGE_FIXTURES[0].candidate_id}/finalize"
        )
        assert resp.status_code == 200, resp.text
        image_id = resp.json()["id"]
        resp = self.request(
            "POST",
            f"/v1/images/{image_id}/discs",
            json_body={"location": "vault-a/shelf-01"},
        )
        assert resp.status_code == 200, resp.text
        with self.state.lock:
            self.state.files_by_collection[docs_key]["tax/2022/invoice-123.pdf"].hot = False

    def seed_docs_tax_fully_compliant(self) -> None:
        self.seed_planner_fixtures()
        self.planning.finalize_image(IMAGE_ID)
        for disc_id, location in (
            ("20260420T040001Z-1", "vault-a/shelf-01"),
            ("20260420T040001Z-2", "vault-b/shelf-01"),
        ):
            with self.state.lock:
                existing = self.state.disc_summaries.get(("20260420T040001Z", DiscId(disc_id)))
            if existing is None or existing.state == DiscState.NEEDED:
                self.discs.register("20260420T040001Z", location, disc_id=disc_id)
            self.discs.update(
                "20260420T040001Z",
                disc_id,
                location=location,
                state="verified",
                verification_state="verified",
            )

    def seed_docs_archive_with_split_invoice(self) -> None:
        docs_key = CollectionId(normalize_collection_id(DOCS_COLLECTION_ID))
        with self.state.lock:
            docs = self.state.files_by_collection.get(docs_key, {})
            invoice = docs.get(SPLIT_FILE_RELPATH)
            if (
                invoice
                and invoice.disc_coverage
                and any(c.part_index is not None for c in invoice.discs)
            ):
                return
        self.seed_docs_hot()
        self.seed_image_fixtures(SPLIT_IMAGE_FIXTURES)
        for fixture, _disc_id, location in zip(
            SPLIT_IMAGE_FIXTURES,
            (SPLIT_DISC_ONE_ID, SPLIT_DISC_TWO_ID),
            (SPLIT_DISC_ONE_LOCATION, SPLIT_DISC_TWO_LOCATION),
            strict=True,
        ):
            resp = self.request("POST", f"/v1/plan/candidates/{fixture.candidate_id}/finalize")
            assert resp.status_code == 200, resp.text
            image_id = resp.json()["id"]
            resp = self.request(
                "POST",
                f"/v1/images/{image_id}/discs",
                json_body={"location": location},
            )
            assert resp.status_code == 200, resp.text
        with self.state.lock:
            self.state.files_by_collection[docs_key][SPLIT_FILE_RELPATH].hot = False

    def seed_search_fixtures(self) -> None:
        self.seed_docs_archive()
        self.seed_photos_hot()

    def seed_planner_fixtures(self) -> None:
        self.seed_docs_hot()
        self.seed_photos_hot()
        self.state.mark_collection_archive_uploaded(DOCS_COLLECTION_ID)
        self.state.mark_collection_archive_uploaded(PHOTOS_COLLECTION_ID)
        self.seed_image_fixtures(IMAGE_FIXTURES)

    def seed_split_planner_fixtures(self) -> None:
        self.seed_docs_hot()
        self.state.mark_collection_archive_uploaded(DOCS_COLLECTION_ID)
        self.seed_image_fixtures(SPLIT_IMAGE_FIXTURES)

    def constrain_collection_to_paths(
        self,
        collection_id: str,
        paths: Sequence[str],
        *,
        hot: bool,
    ) -> None:
        with self.state.lock:
            collection_key = CollectionId(normalize_collection_id(collection_id))
            records = self.state.files_by_collection.get(collection_key)
            if records is None:
                raise NotFound(f"collection not found: {collection_key}")
            kept_paths = {normalize_relpath(path) for path in paths}
            for path in list(records):
                if path not in kept_paths:
                    del records[path]
            for record in records.values():
                record.hot = hot

    def constrain_collection_to_finalized_image_coverage(
        self,
        collection_id: str,
        image_id: str,
        *,
        hot: bool,
    ) -> None:
        with self.state.lock:
            image = self.state.finalized_images_by_id[ImageId(image_id)]
            paths = [
                path
                for covered_collection_id, path in image.covered_paths
                if str(covered_collection_id) == collection_id
            ]
        assert paths
        self.constrain_collection_to_paths(
            collection_id,
            paths,
            hot=hot,
        )

    def seed_image_fixtures(self, fixtures: tuple[Any, ...]) -> None:
        images_root = self.workspace / "images"
        for fixture in fixtures:
            image_root = write_tree(images_root / fixture.candidate_id, fixture.files)
            self.state.seed_image(
                CandidateRecord(
                    candidate_id=ImageId(fixture.candidate_id),
                    finalized_id=fixture.image_id,
                    filename=fixture.filename,
                    image_root=image_root,
                    bytes=fixture.bytes,
                    iso_ready=fixture.iso_ready,
                    covered_paths=tuple(
                        (CollectionId(collection_id), path)
                        for collection_id, path in fixture.covered_paths
                    ),
                )
            )

    def seed_candidate_for_collection(self, collection_id: str) -> None:
        normalized_collection_id = normalize_collection_id(collection_id)
        with self.state.lock:
            records = self.state.collection_uploads.get(CollectionId(normalized_collection_id))
            source_files = (
                {
                    path: file_record.uploaded_content or b""
                    for path, file_record in records.files.items()
                }
                if records is not None
                else {
                    record.path: record.content
                    for record in self.state.collection_files(normalized_collection_id)
                }
            )
        image_root = write_tree(
            self.workspace / "images" / f"img_{normalized_collection_id.replace('/', '_')}",
            source_files,
        )
        self.state.seed_image(
            CandidateRecord(
                candidate_id=ImageId(f"img_{normalized_collection_id.replace('/', '_')}"),
                finalized_id=ImageId("20260420T050000Z"),
                filename="20260420T050000Z.iso",
                image_root=image_root,
                bytes=sum(len(content) for content in source_files.values()),
                iso_ready=True,
                covered_paths=tuple(
                    (CollectionId(normalized_collection_id), path) for path in sorted(source_files)
                ),
            )
        )

    def upload_required_entries(self, fetch_id: str) -> None:
        self.fetches.upload_all_required_entries(fetch_id)

    def upload_partial_entry(self, fetch_id: str, entry_id: str) -> int:
        return self.fetches.upload_partial_entry(fetch_id, entry_id)

    def recovery_upload_absent(self, fetch_id: str) -> bool:
        with self.state.lock:
            return FetchId(fetch_id) not in self.state.fetches

    def list_read_only_browsing_paths(self) -> set[str]:
        with self.state.lock:
            return {
                file.projected_target
                for records in self.state.files_by_collection.values()
                for file in records.values()
                if file.hot and not file.hot_backing_missing
            }

    def write_through_read_only_browsing_surface(self, path: str) -> httpx.Response:
        request = httpx.Request("PUT", f"http://fixture.invalid/{path.lstrip('/')}")
        return httpx.Response(
            status_code=405,
            request=request,
            text="read-only browsing surface rejects writes",
        )

    def storage_lifecycle_configuration(self, *, storage: str = "hot") -> dict[str, object]:
        assert storage in {"hot", "archive"}
        return {
            "Rules": [
                {
                    "ID": "abort-incomplete-riverhog-uploads",
                    "Status": "Enabled",
                    "Filter": {},
                    "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 3},
                }
            ]
        }

    def bucket_contains_object(self, *, storage: str, key: str) -> bool:
        with self.state.lock:
            if storage == "archive":
                return any(
                    status.object_path == key and status.state == ArchiveState.UPLOADED
                    for status in self.state.collection_archive_status_by_collection.values()
                )
            if storage != "hot":
                raise AssertionError(f"unsupported storage bucket kind: {storage}")
            for records in self.state.files_by_collection.values():
                for file in records.values():
                    key_for_file = (
                        f"collections/{normalize_collection_id(str(file.collection_id))}/"
                        f"{file.path}"
                    )
                    if key_for_file == key and file.hot and not file.hot_backing_missing:
                        return True
            return False

    def bucket_object_metadata(self, *, storage: str, key: str) -> dict[str, str]:
        with self.state.lock:
            if storage != "archive":
                raise AssertionError(f"unsupported object metadata bucket kind: {storage}")
            for status in self.state.collection_archive_status_by_collection.values():
                if status.object_path == key and status.state == ArchiveState.UPLOADED:
                    stored_bytes = status.stored_bytes or 0
                    return {
                        "riverhog-archive-bytes": str(stored_bytes),
                        "riverhog-archive-sha256": hashlib.sha256(key.encode("utf-8")).hexdigest(),
                        "riverhog-archive-format": "tar",
                        "riverhog-compression": "none",
                    }
            raise AssertionError(f"archive object not found: {key}")

    def bucket_contains_prefix(self, *, storage: str, prefix: str) -> bool:
        with self.state.lock:
            if storage == "archive":
                return any(
                    (status.object_path or "").startswith(prefix)
                    and status.state == ArchiveState.UPLOADED
                    for status in self.state.collection_archive_status_by_collection.values()
                )
            if storage != "hot":
                raise AssertionError(f"unsupported storage bucket kind: {storage}")
            if prefix == ".riverhog/uploads/":
                for upload in self.state.collection_uploads.values():
                    for file_record in upload.files.values():
                        if file_record.uploaded_bytes > 0:
                            return True
                for fetch in self.state.fetches.values():
                    for entry in fetch.entries.values():
                        if entry.uploaded_bytes > 0:
                            return True
                return False
            if prefix == "collections/":
                return any(
                    file.hot and not file.hot_backing_missing
                    for records in self.state.files_by_collection.values()
                    for file in records.values()
                )
            return False

    def bucket_write_is_rejected(
        self,
        *,
        credentials: str,
        storage: str,
        key: str,
    ) -> bool:
        assert credentials in {"hot", "archive"}
        assert storage in {"hot", "archive"}
        assert key
        return credentials != storage

    def bucket_read_is_rejected(
        self,
        *,
        credentials: str,
        storage: str,
        key: str,
    ) -> bool:
        assert credentials in {"hot", "archive"}
        assert storage in {"hot", "archive"}
        assert key
        return credentials != storage

    def bucket_list_is_rejected(
        self,
        *,
        credentials: str,
        storage: str,
        prefix: str,
    ) -> bool:
        assert credentials in {"hot", "archive"}
        assert storage in {"hot", "archive"}
        assert prefix
        return credentials != storage

    def fetch_targets_list(self) -> list[str]:
        per_page = max(len(self.state.fetches), 1)
        page = self.fetches.list(page=1, per_page=per_page)
        return [str(target) for fetch in page.fetches for target in fetch.targets]

    def uploaded_entry_content(self, fetch_id: str, entry_path: str) -> bytes | None:
        with self.state.lock:
            record = self.state.fetches[FetchId(fetch_id)]
            for entry in record.entries.values():
                if entry.path == entry_path:
                    return entry.uploaded_content
        raise NotFound(f"entry not found for {fetch_id}: {entry_path}")

    @staticmethod
    def _default_djdan_fixture() -> dict[str, Any]:
        return {
            "reader": {
                "payload_by_disc_path": {},
                "fail_disc_paths": [],
            },
            "burn": {
                "confirmed_disc_ids": [],
                "available_disc_ids": [],
                "location_by_disc_id": {},
                "label_text_by_disc_id": {},
                "fail_disc_ids": [],
                "verify_fail_disc_ids": [],
                "verify_fail_once_disc_ids": [],
                "blank_media_blocked_disc_ids": [],
            },
        }

    def _load_djdan_fixture(self) -> dict[str, Any]:
        if not self.fixture_path.exists():
            return self._default_djdan_fixture()
        return cast(dict[str, Any], json.loads(self.fixture_path.read_text(encoding="utf-8")))

    def _write_djdan_fixture(self, payload: dict[str, Any]) -> None:
        self.fixture_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def configure_djdan_fixture(
        self,
        *,
        fetch_id: str = "fx-1",
        fail_path: str | None = None,
        corrupt_path: str | None = None,
        fail_disc_ids: set[str] | None = None,
        corrupt_disc_ids: set[str] | None = None,
    ) -> None:
        manifest = cast(dict[str, Any], self.fetches.manifest(fetch_id))
        with self.state.lock:
            fetch_record = self.state.fetches[FetchId(fetch_id)]
            content_by_file = {
                (str(entry.collection_id), entry.path): entry.content
                for entry in fetch_record.entries.values()
            }
        payload_by_disc_path: dict[str, str] = {}
        fail_disc_paths: list[str] = []
        fail_disc_ids = fail_disc_ids or set()
        corrupt_disc_ids = corrupt_disc_ids or set()

        for entry in cast(list[dict[str, Any]], manifest["entries"]):
            entry_collection_id = str(entry["collection_id"])
            entry_path = str(entry["path"])
            parts = cast(list[dict[str, Any]], entry["parts"])
            plaintext_parts = split_fixture_plaintext(
                content_by_file[(entry_collection_id, entry_path)],
                len(parts),
            )
            for part in parts:
                part_index = int(part["index"])
                part_plaintext = plaintext_parts[part_index]
                for disc_info in cast(list[dict[str, Any]], part["discs"]):
                    disc_id = str(disc_info["disc_id"])
                    disc_path = str(disc_info["disc_path"])
                    payload = fixture_encrypt_bytes(part_plaintext)
                    if entry_path == corrupt_path or disc_id in corrupt_disc_ids:
                        payload = b"X" + payload[1:]
                    payload_by_disc_path[disc_path] = base64.b64encode(payload).decode("ascii")
                    if entry_path == fail_path or disc_id in fail_disc_ids:
                        fail_disc_paths.append(disc_path)

        payload = self._load_djdan_fixture()
        payload["reader"] = {
            "payload_by_disc_path": payload_by_disc_path,
            "fail_disc_paths": fail_disc_paths,
        }
        self._write_djdan_fixture(payload)

    def confirm_djdan_burn_disc(self, disc_id: str, *, location: str) -> None:
        payload = self._load_djdan_fixture()
        burn = cast(dict[str, Any], payload["burn"])
        confirmed = set(cast(list[str], burn.get("confirmed_disc_ids", [])))
        confirmed.add(disc_id)
        burn["confirmed_disc_ids"] = sorted(confirmed)
        location_by_disc_id = dict(cast(dict[str, str], burn.get("location_by_disc_id", {})))
        location_by_disc_id[disc_id] = location
        burn["location_by_disc_id"] = location_by_disc_id
        label_text_by_disc_id = dict(cast(dict[str, str], burn.get("label_text_by_disc_id", {})))
        label_text_by_disc_id[disc_id] = disc_id
        burn["label_text_by_disc_id"] = label_text_by_disc_id
        self._write_djdan_fixture(payload)

    def set_djdan_burn_disc_available(self, disc_id: str, *, available: bool) -> None:
        payload = self._load_djdan_fixture()
        burn = cast(dict[str, Any], payload["burn"])
        available_disc_ids = set(cast(list[str], burn.get("available_disc_ids", [])))
        if available:
            available_disc_ids.add(disc_id)
        else:
            available_disc_ids.discard(disc_id)
        burn["available_disc_ids"] = sorted(available_disc_ids)
        self._write_djdan_fixture(payload)

    def fail_djdan_burn_disc(self, disc_id: str) -> None:
        payload = self._load_djdan_fixture()
        burn = cast(dict[str, Any], payload["burn"])
        failures = set(cast(list[str], burn.get("fail_disc_ids", [])))
        failures.add(disc_id)
        burn["fail_disc_ids"] = sorted(failures)
        self._write_djdan_fixture(payload)

    def fail_djdan_burn_disc_verification(self, disc_id: str) -> None:
        payload = self._load_djdan_fixture()
        burn = cast(dict[str, Any], payload["burn"])
        failures = set(cast(list[str], burn.get("verify_fail_disc_ids", [])))
        failures.add(disc_id)
        burn["verify_fail_disc_ids"] = sorted(failures)
        self._write_djdan_fixture(payload)

    def fail_djdan_burn_disc_verification_once(self, disc_id: str) -> None:
        payload = self._load_djdan_fixture()
        burn = cast(dict[str, Any], payload["burn"])
        failures = set(cast(list[str], burn.get("verify_fail_once_disc_ids", [])))
        failures.add(disc_id)
        burn["verify_fail_once_disc_ids"] = sorted(failures)
        self._write_djdan_fixture(payload)

    def clear_djdan_burn_failures(self) -> None:
        payload = self._load_djdan_fixture()
        burn = cast(dict[str, Any], payload["burn"])
        burn["fail_disc_ids"] = []
        burn["verify_fail_disc_ids"] = []
        burn["verify_fail_once_disc_ids"] = []
        burn["blank_media_blocked_disc_ids"] = []
        self._write_djdan_fixture(payload)

    def corrupt_djdan_staged_iso(self, image_id: str) -> None:
        image = self.planning.get_image(image_id)
        staging_path = self.workspace / "djdan_staging" / image_id / str(image["filename"])
        if not staging_path.is_file():
            raise AssertionError(f"staged ISO not found: {staging_path}")
        staging_path.write_bytes(staging_path.read_bytes() + b"corrupted-by-fixture\n")

    def djdan_staged_iso_exists(self, image_id: str) -> bool:
        image = self.planning.get_image(image_id)
        staging_path = self.workspace / "djdan_staging" / image_id / str(image["filename"])
        return staging_path.is_file()

    def _subprocess_env(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        env = os.environ.copy()
        pythonpath_parts = [str(ROOT) for ROOT in (SRC_ROOT, REPO_ROOT)]
        existing = env.get("PYTHONPATH")
        if existing:
            pythonpath_parts.append(existing)
        env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
        env["RIVERHOG_BASE_URL"] = self.base_url
        if extra:
            env.update(extra)
        return env


@pytest.fixture(scope="session")
def shared_acceptance_system(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[AcceptanceSystem]:
    system = AcceptanceSystem.create(tmp_path_factory.mktemp("acceptance-system"))
    try:
        yield system
    finally:
        system.close()


@pytest.fixture
def acceptance_system(shared_acceptance_system: AcceptanceSystem) -> Iterator[AcceptanceSystem]:
    shared_acceptance_system.reset()
    yield shared_acceptance_system
