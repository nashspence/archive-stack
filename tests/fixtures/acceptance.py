from __future__ import annotations

import base64
import hashlib
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
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

import httpx
import pytest
import uvicorn
import yaml

from arc_api.app import create_app
from arc_api.deps import ServiceContainer
from arc_core.archive_compliance import (
    collection_protection_state,
    copy_counts_toward_protection,
    image_protection_state,
    normalize_copy_state,
    normalize_required_copy_count,
    registered_copy_shortfall,
)
from arc_core.domain.enums import (
    CopyState,
    FetchState,
    GlacierState,
    ProtectionState,
    RecoverySessionState,
    VerificationState,
)
from arc_core.domain.errors import Conflict, HashMismatch, InvalidState, NotFound
from arc_core.domain.models import (
    CollectionCoverageImage,
    CollectionSummary,
    CopyHistoryEntry,
    CopySummary,
    FetchCopyHint,
    FetchSummary,
    GlacierArchiveStatus,
    GlacierBillingActual,
    GlacierBillingActualsView,
    GlacierBillingExportBreakdown,
    GlacierBillingExportView,
    GlacierBillingForecast,
    GlacierBillingForecastView,
    GlacierBillingInvoiceSummary,
    GlacierBillingInvoicesView,
    GlacierBillingSummary,
    GlacierCollectionContribution,
    GlacierPricingBasis,
    GlacierUsageCollection,
    GlacierUsageImage,
    GlacierUsageReport,
    GlacierUsageSnapshot,
    GlacierUsageTotals,
    PinSummary,
    RecoveryCostEstimate,
    RecoveryNotificationStatus,
    RecoverySessionImage,
    RecoverySessionSummary,
)
from arc_core.domain.selectors import parse_target
from arc_core.domain.types import (
    CollectionId,
    CopyId,
    EntryId,
    FetchId,
    ImageId,
    Sha256Hex,
    TargetStr,
)
from arc_core.fs_paths import (
    find_collection_id_conflict,
    normalize_collection_id,
    normalize_relpath,
)
from arc_core.planner.manifest import MANIFEST_FILENAME
from arc_core.runtime_config import load_runtime_config
from arc_core.services.planning import ImageRootPlanningService, ImageRootRecord
from arc_core.webhooks import (
    WebhookConfig,
    build_glacier_upload_failed_payload,
    build_recovery_ready_payload,
)
from tests.fixtures.data import (
    DEFAULT_COPY_CREATED_AT,
    DOCS_COLLECTION_ID,
    DOCS_FILES,
    IMAGE_FIXTURES,
    MIN_FILL_BYTES,
    PHOTOS_2024_FILES,
    PHOTOS_COLLECTION_ID,
    PHOTOS_NESTED_COLLECTION_ID,
    PHOTOS_PARENT_COLLECTION_ID,
    SPLIT_COPY_ONE_ID,
    SPLIT_COPY_ONE_LOCATION,
    SPLIT_COPY_TWO_ID,
    SPLIT_COPY_TWO_LOCATION,
    SPLIT_FILE_RELPATH,
    SPLIT_IMAGE_FIXTURES,
    TARGET_BYTES,
    build_file_copy,
    fixture_decrypt_bytes,
    fixture_encrypt_bytes,
    split_fixture_plaintext,
    write_tree,
)
from tests.timing_profile import time_block

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
FIXTURE_UPLOAD_EXPIRES_AT = "2099-12-31T23:59:59Z"
_UPLOAD_EXPIRY_SWEEP_INTERVAL_SECONDS = 0.05
_GLACIER_UPLOAD_SWEEP_INTERVAL_SECONDS = 1.0
_GLACIER_UPLOAD_RETRY_LIMIT = 2
_GLACIER_RECOVERY_SWEEP_INTERVAL_SECONDS = 0.05
_GLACIER_RECOVERY_RESTORE_LATENCY_SECONDS = 0.2
_GLACIER_RECOVERY_READY_TTL_SECONDS = 4.0
_GLACIER_RECOVERY_WEBHOOK_RETRY_DELAY_SECONDS = 1.0
_GLACIER_RECOVERY_WEBHOOK_REMINDER_INTERVAL_SECONDS = 2.0


def _generated_copy_id(image_id: str, ordinal: int) -> str:
    return f"{image_id}-{ordinal}"


def _copy_counts_toward_slot_pool(state: CopyState) -> bool:
    return state in {CopyState.NEEDED, CopyState.BURNING} or copy_counts_toward_protection(
        state.value
    )


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


def _copy_history(
    *,
    at: str,
    event: str,
    state: CopyState,
    verification_state,
    location: str | None,
) -> tuple[CopyHistoryEntry, ...]:
    return (
        CopyHistoryEntry(
            at=at,
            event=event,
            state=state,
            verification_state=verification_state,
            location=location,
        ),
    )


@dataclass(frozen=True, slots=True)
class FileCopy:
    id: CopyId
    volume_id: str
    location: str
    disc_path: str
    enc: dict[str, object]
    part_index: int | None = None
    part_count: int | None = None
    part_bytes: int | None = None
    part_sha256: str | None = None

    @property
    def hint(self) -> FetchCopyHint:
        return FetchCopyHint(id=self.id, volume_id=self.volume_id, location=self.location)


@dataclass(slots=True)
class StoredFile:
    collection_id: CollectionId
    path: str
    content: bytes
    hot: bool
    archived: bool
    hot_backing_missing: bool = False
    copies: list[FileCopy] = field(default_factory=list)

    @property
    def bytes(self) -> int:
        return len(self.content)

    @property
    def sha256(self) -> Sha256Hex:
        return cast(Sha256Hex, hashlib.sha256(self.content).hexdigest())

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
            "fill": self.fill,
            "files": self.files,
            "collections": len(self.collections),
            "collection_ids": self.collections,
            "iso_ready": self.iso_ready,
        }

    def finalized_image_payload(
        self,
        *,
        physical_copies_registered: int = 0,
        glacier: GlacierArchiveStatus | None = None,
    ) -> dict[str, object]:
        required_copy_count = normalize_required_copy_count(None)
        glacier = glacier or GlacierArchiveStatus(state=GlacierState.PENDING)
        protection_state = image_protection_state(
            required_copy_count=required_copy_count,
            registered_copy_count=physical_copies_registered,
            glacier_state=glacier.state,
        )
        return {
            "id": self.finalized_id,
            "filename": self.filename,
            "finalized_at": self.finalized_at,
            "bytes": self.bytes,
            "fill": self.fill,
            "files": self.files,
            "collections": len(self.collections),
            "collection_ids": self.collections,
            "iso_ready": True,
            "protection_state": protection_state.value,
            "physical_copies_required": required_copy_count,
            "physical_copies_registered": physical_copies_registered,
            "physical_copies_missing": registered_copy_shortfall(
                required_copy_count=required_copy_count,
                registered_copy_count=physical_copies_registered,
            ),
            "glacier": {
                "state": glacier.state.value,
                "object_path": glacier.object_path,
                "stored_bytes": glacier.stored_bytes,
                "backend": glacier.backend,
                "storage_class": glacier.storage_class,
                "last_uploaded_at": glacier.last_uploaded_at,
                "last_verified_at": glacier.last_verified_at,
                "failure": glacier.failure,
            },
        }

    def image_root_record(self) -> ImageRootRecord:
        return ImageRootRecord(
            image_id=self.finalized_id,
            volume_id=self.finalized_id,
            filename=self.filename,
            image_root=self.image_root,
        )


@dataclass(slots=True)
class FetchEntryRecord:
    id: EntryId
    collection_id: CollectionId
    path: str
    bytes: int
    sha256: Sha256Hex
    content: bytes
    copies: list[FileCopy]
    uploaded_bytes: int = 0
    uploaded_content: bytes | None = None
    upload_expires_at: str | None = None
    upload_url: str | None = None


@dataclass(slots=True)
class FetchRecord:
    summary: FetchSummary
    entries: dict[EntryId, FetchEntryRecord]


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


@dataclass(slots=True)
class AcceptanceGlacierUploadJob:
    image_id: ImageId
    attempt_count: int = 0
    completed: bool = False
    failed: bool = False


@dataclass(slots=True)
class AcceptanceRecoverySessionRecord:
    session_id: str
    image_id: ImageId
    state: RecoverySessionState
    created_at: str
    approved_at: str | None = None
    restore_requested_at: str | None = None
    restore_ready_at: str | None = None
    restore_expires_at: str | None = None
    completed_at: str | None = None
    latest_message: str | None = None
    reminder_count: int = 0
    next_reminder_at: str | None = None
    last_notified_at: str | None = None


@dataclass(slots=True)
class AcceptanceState:
    local_collection_sources: dict[CollectionId, Path] = field(default_factory=dict)
    files_by_collection: dict[CollectionId, dict[str, StoredFile]] = field(default_factory=dict)
    candidates_by_id: dict[ImageId, CandidateRecord] = field(default_factory=dict)
    finalized_images_by_id: dict[ImageId, CandidateRecord] = field(default_factory=dict)
    copy_summaries: dict[tuple[str, CopyId], CopySummary] = field(default_factory=dict)
    exact_pins: set[TargetStr] = field(default_factory=set)
    fetches: dict[FetchId, FetchRecord] = field(default_factory=dict)
    collection_uploads: dict[CollectionId, CollectionUploadRecord] = field(default_factory=dict)
    glacier_status_by_image: dict[ImageId, GlacierArchiveStatus] = field(default_factory=dict)
    glacier_jobs_by_image: dict[ImageId, AcceptanceGlacierUploadJob] = field(default_factory=dict)
    glacier_usage_snapshots: list[GlacierUsageSnapshot] = field(default_factory=list)
    recovery_sessions_by_id: dict[str, AcceptanceRecoverySessionRecord] = field(
        default_factory=dict
    )
    glacier_upload_failures_by_image: dict[ImageId, str] = field(default_factory=dict)
    webhook_deliveries: list[dict[str, object]] = field(default_factory=list)
    webhook_attempts: list[dict[str, object]] = field(default_factory=list)
    webhook_behaviors: list[dict[str, object]] = field(default_factory=list)
    public_base_url: str = ""
    glacier_billing_metadata_available: bool = False
    next_fetch_number: int = 0

    def clear_webhook_deliveries(self) -> None:
        self.webhook_deliveries.clear()
        self.webhook_attempts.clear()
        self.webhook_behaviors.clear()

    def record_webhook_delivery(self, payload: dict[str, object]) -> None:
        normalized = json.loads(json.dumps(payload, sort_keys=True))
        assert isinstance(normalized, dict)
        self.webhook_deliveries.append(normalized)

    def record_webhook_attempt(self, payload: dict[str, object]) -> None:
        normalized = json.loads(json.dumps(payload, sort_keys=True))
        assert isinstance(normalized, dict)
        self.webhook_attempts.append(normalized)

    def list_webhook_deliveries(self) -> list[dict[str, object]]:
        return [
            cast(dict[str, object], json.loads(json.dumps(item)))
            for item in self.webhook_deliveries
        ]

    def list_webhook_attempts(self) -> list[dict[str, object]]:
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
        url = f"{self.public_base_url.rstrip('/')}/_test/webhooks" if self.public_base_url else ""
        return WebhookConfig(
            url=url,
            base_url=self.public_base_url,
            retry_seconds=_GLACIER_RECOVERY_WEBHOOK_RETRY_DELAY_SECONDS,
            reminder_interval_seconds=_GLACIER_RECOVERY_WEBHOOK_REMINDER_INTERVAL_SECONDS,
        )

    def register_local_collection_source(self, collection_id: str, root: Path) -> None:
        self.local_collection_sources[CollectionId(collection_id)] = root

    def seed_collection(
        self,
        collection_id: str,
        files: Mapping[str, bytes],
        *,
        hot_paths: set[str],
        archived_paths: set[str],
    ) -> None:
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
                archived=normalized in archived_paths,
            )
        self.files_by_collection[collection_key] = records

    def seed_image(self, image: CandidateRecord) -> None:
        self.candidates_by_id[image.candidate_id] = image

    def enqueue_glacier_upload(self, image: CandidateRecord) -> None:
        image_key = ImageId(image.finalized_id)
        self.glacier_status_by_image.setdefault(
            image_key,
            GlacierArchiveStatus(state=GlacierState.PENDING),
        )
        self.glacier_jobs_by_image.setdefault(
            image_key,
            AcceptanceGlacierUploadJob(image_id=image_key),
        )

    def glacier_status(self, image_id: str) -> GlacierArchiveStatus:
        return self.glacier_status_by_image.get(
            ImageId(image_id),
            GlacierArchiveStatus(state=GlacierState.PENDING),
        )

    def ensure_glacier_recovery_session(self, image_id: str) -> None:
        if self.glacier_status(image_id).state != GlacierState.UPLOADED:
            return
        if self._protected_copy_count(image_id) > 0:
            return
        if not self._has_recovery_triggering_copy_history(image_id):
            return
        if self.active_recovery_session(image_id) is not None:
            return
        session_id = self._generated_recovery_session_id(image_id)
        self.recovery_sessions_by_id[session_id] = AcceptanceRecoverySessionRecord(
            session_id=session_id,
            image_id=ImageId(image_id),
            state=RecoverySessionState.PENDING_APPROVAL,
            created_at=_acceptance_isoformat(datetime.now(UTC)),
            latest_message=(
                "Approve the estimated restore cost before Riverhog requests archive restore."
            ),
        )

    def active_recovery_session(self, image_id: str) -> AcceptanceRecoverySessionRecord | None:
        active_states = {
            RecoverySessionState.PENDING_APPROVAL,
            RecoverySessionState.RESTORE_REQUESTED,
            RecoverySessionState.READY,
        }
        sessions = [
            record
            for record in self.recovery_sessions_by_id.values()
            if str(record.image_id) == image_id and record.state in active_states
        ]
        if not sessions:
            return None
        return sorted(sessions, key=lambda current: current.created_at, reverse=True)[0]

    def latest_recovery_session(self, image_id: str) -> AcceptanceRecoverySessionRecord | None:
        sessions = [
            record
            for record in self.recovery_sessions_by_id.values()
            if str(record.image_id) == image_id
        ]
        if not sessions:
            return None
        return sorted(sessions, key=lambda current: current.created_at, reverse=True)[0]

    def _generated_recovery_session_id(self, image_id: str) -> str:
        existing_ids = {
            record.session_id
            for record in self.recovery_sessions_by_id.values()
            if str(record.image_id) == image_id
        }
        ordinal = 1
        while True:
            candidate = f"rs-{image_id}-{ordinal}"
            ordinal += 1
            if candidate not in existing_ids:
                return candidate

    def _protected_copy_count(self, image_id: str) -> int:
        return sum(
            1
            for (volume_id, _copy_id), summary in self.copy_summaries.items()
            if volume_id == image_id and copy_counts_toward_protection(summary.state.value)
        )

    def _has_recovery_triggering_copy_history(self, image_id: str) -> bool:
        return any(
            volume_id == image_id
            and normalize_copy_state(summary.state.value)
            not in {CopyState.NEEDED, CopyState.BURNING}
            for (volume_id, _copy_id), summary in self.copy_summaries.items()
        )

    def ensure_required_copy_slots(self, image_id: str) -> None:
        image = self.finalized_images_by_id.get(ImageId(image_id))
        if image is None:
            raise NotFound(f"image not found: {image_id}")
        required_copy_count = normalize_required_copy_count(None)
        copies = [
            summary
            for (volume_id, _copy_id), summary in sorted(self.copy_summaries.items())
            if volume_id == image_id
        ]
        existing_ids = {
            str(copy_id)
            for volume_id, copy_id in self.copy_summaries
            if volume_id == image_id
        }

        if not copies:
            while len(existing_ids) < required_copy_count:
                self._create_generated_copy_slot(image_id, existing_ids=existing_ids)
            return

        active_slot_count = sum(
            1 for summary in copies if _copy_counts_toward_slot_pool(summary.state)
        )
        protected_copy_count = sum(
            1 for summary in copies if copy_counts_toward_protection(summary.state.value)
        )
        if protected_copy_count > 0:
            while active_slot_count < required_copy_count:
                self._create_generated_copy_slot(image_id, existing_ids=existing_ids)
                active_slot_count += 1

    def _create_generated_copy_slot(self, image_id: str, *, existing_ids: set[str]) -> CopySummary:
        ordinal = 1
        while True:
            copy_id = _generated_copy_id(image_id, ordinal)
            ordinal += 1
            if copy_id in existing_ids:
                continue
            summary = CopySummary(
                id=CopyId(copy_id),
                volume_id=image_id,
                label_text=copy_id,
                location=None,
                created_at=DEFAULT_COPY_CREATED_AT,
                state=CopyState.NEEDED,
                verification_state=VerificationState.PENDING,
                history=_copy_history(
                    at=DEFAULT_COPY_CREATED_AT,
                    event="created",
                    state=CopyState.NEEDED,
                    verification_state=VerificationState.PENDING,
                    location=None,
                ),
            )
            self.copy_summaries[(image_id, CopyId(copy_id))] = summary
            existing_ids.add(copy_id)
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
        image_coverage, covered_paths = self.collection_image_coverage(collection_id)
        protected_bytes = 0
        image_states = {str(image.id): image.protection_state for image in image_coverage}
        for record in records:
            image_ids = covered_paths.get(record.path, set())
            if image_ids and all(
                image_states.get(image_id) == ProtectionState.PROTECTED for image_id in image_ids
            ):
                protected_bytes += record.bytes
        archived_bytes = sum(record.bytes for record in records if record.archived)
        return CollectionSummary(
            id=CollectionId(str(collection_id)),
            files=len(records),
            bytes=sum(record.bytes for record in records),
            hot_bytes=sum(record.bytes for record in records if record.hot),
            archived_bytes=archived_bytes,
            protection_state=collection_protection_state(
                bytes_total=sum(record.bytes for record in records),
                protected_bytes=protected_bytes,
                archived_bytes=archived_bytes,
                image_states=(image.protection_state for image in image_coverage),
            ),
            protected_bytes=protected_bytes,
            image_coverage=image_coverage,
        )

    def collection_image_coverage(
        self, collection_id: str | CollectionId
    ) -> tuple[list[CollectionCoverageImage], dict[str, set[str]]]:
        normalized_collection_id = CollectionId(str(collection_id))
        covered_paths: dict[str, set[str]] = {}
        image_coverage: list[CollectionCoverageImage] = []
        for image in sorted(
            self.finalized_images_by_id.values(),
            key=lambda current: current.finalized_id,
        ):
            if normalized_collection_id not in {
                collection_id for collection_id, _ in image.covered_paths
            }:
                continue
            for covered_collection_id, path in image.covered_paths:
                if covered_collection_id != normalized_collection_id:
                    continue
                covered_paths.setdefault(path, set()).add(image.finalized_id)
            copies = [
                summary
                for (volume_id, _copy_id), summary in sorted(self.copy_summaries.items())
                if volume_id == image.finalized_id
            ]
            physical_copies_registered = sum(
                1 for copy in copies if copy_counts_toward_protection(copy.state.value)
            )
            physical_copies_required = normalize_required_copy_count(None)
            glacier = self.glacier_status(image.finalized_id)
            image_coverage.append(
                CollectionCoverageImage(
                    id=ImageId(image.finalized_id),
                    filename=image.filename,
                    protection_state=image_protection_state(
                        required_copy_count=physical_copies_required,
                        registered_copy_count=physical_copies_registered,
                        glacier_state=glacier.state,
                    ),
                    physical_copies_required=physical_copies_required,
                    physical_copies_registered=physical_copies_registered,
                    physical_copies_missing=registered_copy_shortfall(
                        required_copy_count=physical_copies_required,
                        registered_copy_count=physical_copies_registered,
                    ),
                    copies=copies,
                    glacier=glacier,
                )
            )
        return image_coverage, covered_paths

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

    def reconcile_hot_from_pins(self) -> None:
        selected_paths: set[tuple[CollectionId, str]] = set()
        for raw_target in self.exact_pins:
            for record in self.selected_files(str(raw_target), missing_ok=True):
                selected_paths.add((record.collection_id, record.path))
        for collection_files in self.files_by_collection.values():
            for record in collection_files.values():
                record.hot = (record.collection_id, record.path) in selected_paths

    def reserve_fetch_id(self, fetch_id: str) -> None:
        if fetch_id.startswith("fx-"):
            suffix = fetch_id.removeprefix("fx-")
            if suffix.isdigit():
                self.next_fetch_number = max(self.next_fetch_number, int(suffix))

    @staticmethod
    def _copy_from_dict(item: dict[str, object]) -> FileCopy:
        return FileCopy(
            id=CopyId(str(item["id"])),
            volume_id=str(item["volume_id"]),
            location=str(item["location"]),
            disc_path=str(item["disc_path"]),
            enc=cast(dict[str, object], item["enc"]),
            part_index=cast(int | None, item.get("part_index")),
            part_count=cast(int | None, item.get("part_count")),
            part_bytes=cast(int | None, item.get("part_bytes")),
            part_sha256=cast(str | None, item.get("part_sha256")),
        )


class AcceptanceCollectionService:
    def __init__(self, state: AcceptanceState) -> None:
        self.state = state

    def create_or_resume_upload(
        self,
        *,
        collection_id: str,
        files: list[dict[str, object]],
        ingest_source: str | None = None,
    ) -> dict[str, object]:
        normalized_collection_id = normalize_collection_id(collection_id)
        collection_key = CollectionId(normalized_collection_id)
        if collection_key in self.state.files_by_collection:
            raise Conflict(f"collection already exists: {normalized_collection_id}")

        normalized_files = self._normalize_files(files)
        upload = self.state.collection_uploads.get(collection_key)
        if upload is not None:
            upload = self._expire_upload(upload)

        if upload is None:
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
            )
            self.state.collection_uploads[collection_key] = upload
        else:
            existing_manifest = [
                {
                    "path": file_record.path,
                    "bytes": file_record.bytes,
                    "sha256": str(file_record.sha256),
                }
                for file_record in upload.files.values()
            ]
            if existing_manifest != normalized_files:
                raise Conflict(
                    f"collection upload manifest does not match: {normalized_collection_id}"
                )
            upload.ingest_source = ingest_source

        if self._is_complete(upload):
            summary = self._finalize_upload(upload)
            return self._upload_payload(upload, state="finalized", collection=summary)
        return self._upload_payload(upload, state="uploading", collection=None)

    def get_upload(self, collection_id: str) -> dict[str, object]:
        normalized_collection_id = normalize_collection_id(collection_id)
        upload = self.state.collection_uploads.get(CollectionId(normalized_collection_id))
        if upload is None:
            raise NotFound(f"collection upload not found: {normalized_collection_id}")
        upload = self._expire_upload(upload)
        if upload is None:
            raise NotFound(f"collection upload not found: {normalized_collection_id}")
        if self._is_complete(upload):
            summary = self._finalize_upload(upload)
            return self._upload_payload(upload, state="finalized", collection=summary)
        return self._upload_payload(upload, state="uploading", collection=None)

    def create_or_resume_file_upload(self, collection_id: str, path: str) -> dict[str, object]:
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
            file_record.upload_url = (
                f"/v1/collection-uploads/{quote(normalized_collection_id, safe='/')}/files/"
                f"{quote(normalized_path, safe='/')}/upload"
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

        if self._is_complete(upload):
            self._finalize_upload(upload)

        return {
            "offset": file_record.uploaded_bytes,
            "length": file_record.bytes,
            "expires_at": file_record.upload_expires_at,
        }

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

    def expire_stale_uploads(self) -> None:
        for collection_id in list(self.state.collection_uploads):
            upload = self.state.collection_uploads.get(collection_id)
            if upload is None:
                continue
            self._expire_upload(upload)

    def get(self, collection_id: str) -> CollectionSummary:
        return self.state.collection_summary(collection_id)

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
        if (
            file_record.uploaded_content is not None
            and file_record.uploaded_bytes >= file_record.bytes
        ):
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
            archived_paths=set(),
        )
        summary = self.state.collection_summary(str(upload.collection_id))
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
                    "archived_bytes": collection.archived_bytes,
                    "pending_bytes": collection.pending_bytes,
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

    def search(self, query: str, limit: int) -> list[dict[str, object]]:
        needle = query.casefold()
        results: list[dict[str, object]] = []

        for collection_id in sorted(self.state.files_by_collection):
            collection_name = str(collection_id)
            if needle in collection_name.casefold():
                summary = self.state.collection_summary(collection_id)
                results.append(
                    {
                        "kind": "collection",
                        "target": f"{collection_name}/",
                        "collection": collection_name,
                        "files": summary.files,
                        "bytes": summary.bytes,
                        "hot_bytes": summary.hot_bytes,
                        "archived_bytes": summary.archived_bytes,
                        "pending_bytes": summary.pending_bytes,
                    }
                )

        for collection_id in sorted(self.state.files_by_collection):
            collection_name = str(collection_id)
            for record in sorted(
                self.state.collection_files(collection_id), key=lambda item: item.path
            ):
                full_path = record.projected_target
                if needle not in full_path.casefold():
                    continue
                results.append(
                    {
                        "kind": "file",
                        "target": record.projected_target,
                        "collection": collection_name,
                        "path": f"/{record.path}",
                        "bytes": record.bytes,
                        "hot": record.hot,
                        "copies": [
                            {
                                "id": str(copy.id),
                                "volume_id": copy.volume_id,
                                "location": copy.location,
                            }
                            for copy in record.copies
                        ],
                    }
                )

        results.sort(key=lambda item: (str(item["kind"]), str(item["target"])))
        return results[:limit]


class AcceptancePlanningService:
    def __init__(self, state: AcceptanceState) -> None:
        self.state = state
        self._iso_service = ImageRootPlanningService(
            image_lookup=self._image_root_record,
            list_lookup=self.list_images,
            plan_lookup=self.get_plan,
        )

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

    def list_images(
        self,
        *,
        page: int,
        per_page: int,
        sort: str,
        order: str,
        q: str | None,
        collection: str | None,
        has_copies: bool | None,
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
        if has_copies is not None:
            images = [
                image
                for image in images
                if (self._physical_copies_registered(image) > 0) is has_copies
            ]

        reverse = order == "desc"
        sort_key = {
            "finalized_at": lambda image: (image.finalized_id, image.filename),
            "bytes": lambda image: (image.bytes, image.finalized_id),
            "physical_copies_registered": lambda image: (
                self._physical_copies_registered(image),
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
                    physical_copies_registered=self._physical_copies_registered(image),
                    glacier=self.state.glacier_status(image.finalized_id),
                )
                for image in page_images
            ],
        }

    def get_image(self, image_id: str) -> dict[str, object]:
        image = self._finalized_image_record(image_id)
        return image.finalized_image_payload(
            physical_copies_registered=self._physical_copies_registered(image),
            glacier=self.state.glacier_status(image.finalized_id),
        )

    def finalize_image(self, candidate_id: str) -> dict[str, object]:
        candidate = self._candidate_record(candidate_id)
        if not candidate.iso_ready:
            raise InvalidState("image must be ISO-ready before finalization")
        finalized_key = ImageId(candidate.finalized_id)
        self.state.finalized_images_by_id.setdefault(finalized_key, candidate)
        self.state.ensure_required_copy_slots(candidate.finalized_id)
        self.state.enqueue_glacier_upload(candidate)
        image = self.state.finalized_images_by_id[finalized_key]
        return image.finalized_image_payload(
            physical_copies_registered=self._physical_copies_registered(image),
            glacier=self.state.glacier_status(image.finalized_id),
        )

    async def get_iso_stream(self, image_id: str) -> object:
        return await self._iso_service.get_iso_stream(image_id)

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

    def _image_root_record(self, image_id: str) -> ImageRootRecord:
        return self._finalized_image_record(image_id).image_root_record()

    def _physical_copies_registered(self, image: CandidateRecord) -> int:
        return sum(
            1
            for (volume_id, _copy_id), summary in self.state.copy_summaries.items()
            if volume_id == image.finalized_id
            and copy_counts_toward_protection(summary.state.value)
        )


class AcceptanceGlacierUploadService:
    def __init__(self, state: AcceptanceState) -> None:
        self.state = state

    def process_due_uploads(self, *, limit: int = 1) -> int:
        attempted = 0
        for image_id, job in sorted(self.state.glacier_jobs_by_image.items()):
            if attempted >= limit or job.completed or job.failed:
                continue
            image = self.state.finalized_images_by_id.get(image_id)
            if image is None:
                continue
            failure = self.state.glacier_upload_failures_by_image.get(image_id)
            job.attempt_count += 1
            if failure is not None:
                if job.attempt_count < _GLACIER_UPLOAD_RETRY_LIMIT:
                    self.state.glacier_status_by_image[image_id] = GlacierArchiveStatus(
                        state=GlacierState.RETRYING,
                        object_path=None,
                        stored_bytes=None,
                        backend=None,
                        storage_class=None,
                        last_uploaded_at=None,
                        last_verified_at=None,
                        failure=failure,
                    )
                    attempted += 1
                    continue
                current_text = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                self.state.glacier_status_by_image[image_id] = GlacierArchiveStatus(
                    state=GlacierState.FAILED,
                    object_path=None,
                    stored_bytes=None,
                    backend=None,
                    storage_class=None,
                    last_uploaded_at=None,
                    last_verified_at=None,
                    failure=failure,
                )
                self.state.deliver_webhook_payload(
                    build_glacier_upload_failed_payload(
                        config=self.state.webhook_config(),
                        image_id=image.finalized_id,
                        error=failure,
                        attempts=job.attempt_count,
                        failed_at=current_text,
                    ),
                    delivered_at=datetime.now(UTC),
                    timeout_seconds=self.state.webhook_config().timeout_seconds,
                )
                job.failed = True
                attempted += 1
                continue
            object_path = f"glacier/finalized-images/{image.finalized_id}/{image.finalized_id}.iso"
            now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            self.state.glacier_status_by_image[image_id] = GlacierArchiveStatus(
                state=GlacierState.UPLOADED,
                object_path=object_path,
                stored_bytes=image.bytes,
                backend="s3",
                storage_class="DEEP_ARCHIVE",
                last_uploaded_at=now,
                last_verified_at=now,
                failure=None,
            )
            self.state.ensure_glacier_recovery_session(image.finalized_id)
            self.state.glacier_usage_snapshots.append(_acceptance_glacier_snapshot(self.state))
            job.completed = True
            attempted += 1
        return attempted


class AcceptanceRecoverySessionService:
    def __init__(self, state: AcceptanceState) -> None:
        self.state = state

    def get(self, session_id: str) -> RecoverySessionSummary:
        record = self.state.recovery_sessions_by_id.get(session_id)
        if record is None:
            raise NotFound(f"recovery session not found: {session_id}")
        return self._summary(record)

    def get_for_image(self, image_id: str) -> RecoverySessionSummary:
        record = self.state.latest_recovery_session(image_id)
        if record is None:
            raise NotFound(f"recovery session not found for image: {image_id}")
        return self._summary(record)

    def create_or_resume_for_image(self, image_id: str) -> RecoverySessionSummary:
        image = self.state.finalized_images_by_id.get(ImageId(image_id))
        if image is None:
            raise NotFound(f"image not found: {image_id}")
        active = self.state.active_recovery_session(image_id)
        if active is not None:
            return self._summary(active)
        if self.state.glacier_status(image_id).state != GlacierState.UPLOADED:
            raise InvalidState(
                f"image archive is not uploaded and cannot be restored yet: {image_id}"
            )
        if self.state._protected_copy_count(image_id) > 0:
            raise Conflict(
                "image still has protected copies and does not require "
                f"archive recovery: {image_id}"
            )
        self.state.ensure_glacier_recovery_session(image_id)
        record = self.state.active_recovery_session(image_id)
        assert record is not None
        return self._summary(record)

    def approve(self, session_id: str) -> RecoverySessionSummary:
        record = self.state.recovery_sessions_by_id.get(session_id)
        if record is None:
            raise NotFound(f"recovery session not found: {session_id}")
        if record.state == RecoverySessionState.EXPIRED:
            raise InvalidState("recovery session expired; re-initiate recovery to request restore")
        if record.state != RecoverySessionState.PENDING_APPROVAL:
            raise InvalidState("recovery session is not waiting for approval")
        current = datetime.now(UTC)
        current_text = _acceptance_isoformat(current)
        record.state = RecoverySessionState.RESTORE_REQUESTED
        record.approved_at = current_text
        record.restore_requested_at = current_text
        record.restore_ready_at = _acceptance_isoformat(
            current + timedelta(seconds=_GLACIER_RECOVERY_RESTORE_LATENCY_SECONDS)
        )
        record.latest_message = (
            "Archive restore requested; wait for the ready notification before downloading or "
            "burning replacement media."
        )
        return self._summary(record)

    def complete(self, session_id: str) -> RecoverySessionSummary:
        record = self.state.recovery_sessions_by_id.get(session_id)
        if record is None:
            raise NotFound(f"recovery session not found: {session_id}")
        if record.state not in {
            RecoverySessionState.READY,
            RecoverySessionState.EXPIRED,
        }:
            raise InvalidState("recovery session is not ready to complete")
        current_text = _acceptance_isoformat(datetime.now(UTC))
        record.state = RecoverySessionState.COMPLETED
        record.completed_at = current_text
        record.restore_expires_at = current_text
        record.next_reminder_at = None
        record.latest_message = (
            "Recovery session completed and restored ISO data was cleaned up immediately."
        )
        return self._summary(record)

    def process_due_sessions(self, *, limit: int = 100) -> int:
        if limit < 1:
            return 0
        current = datetime.now(UTC)
        current_text = _acceptance_isoformat(current)
        processed = 0
        for record in sorted(
            self.state.recovery_sessions_by_id.values(),
            key=lambda current_record: (current_record.created_at, current_record.session_id),
        ):
            if processed >= limit:
                break
            if (
                record.state == RecoverySessionState.RESTORE_REQUESTED
                and record.restore_ready_at is not None
                and record.restore_ready_at <= current_text
            ):
                record.state = RecoverySessionState.READY
                record.restore_expires_at = _acceptance_isoformat(
                    current + timedelta(seconds=_GLACIER_RECOVERY_READY_TTL_SECONDS)
                )
                record.latest_message = (
                    "Restored ISO data is ready; reopen the session to complete download, verify "
                    "the ISO, and burn replacement media before cleanup."
                )
                image = self.state.finalized_images_by_id[record.image_id]
                try:
                    self.state.deliver_webhook_payload(
                        build_recovery_ready_payload(
                            config=self.state.webhook_config(),
                            session_id=record.session_id,
                            restore_expires_at=record.restore_expires_at,
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
                        current
                        + timedelta(seconds=_GLACIER_RECOVERY_WEBHOOK_RETRY_DELAY_SECONDS)
                    )
                    processed += 1
                    continue
                record.last_notified_at = current_text
                record.next_reminder_at = _acceptance_isoformat(
                    current + timedelta(seconds=_GLACIER_RECOVERY_WEBHOOK_REMINDER_INTERVAL_SECONDS)
                )
                processed += 1
                continue
            if (
                record.state == RecoverySessionState.READY
                and record.next_reminder_at is not None
                and record.next_reminder_at <= current_text
            ):
                image = self.state.finalized_images_by_id[record.image_id]
                initial_notification_succeeded = record.last_notified_at is not None
                try:
                    self.state.deliver_webhook_payload(
                        build_recovery_ready_payload(
                            config=self.state.webhook_config(),
                            session_id=record.session_id,
                            restore_expires_at=record.restore_expires_at,
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
                        current
                        + timedelta(seconds=_GLACIER_RECOVERY_WEBHOOK_RETRY_DELAY_SECONDS)
                    )
                    processed += 1
                    continue
                record.last_notified_at = current_text
                record.next_reminder_at = _acceptance_isoformat(
                    current + timedelta(seconds=_GLACIER_RECOVERY_WEBHOOK_REMINDER_INTERVAL_SECONDS)
                )
                if initial_notification_succeeded:
                    record.reminder_count += 1
                processed += 1
                continue
            if (
                record.state == RecoverySessionState.READY
                and record.restore_expires_at is not None
                and record.restore_expires_at <= current_text
            ):
                record.state = RecoverySessionState.EXPIRED
                record.next_reminder_at = None
                record.latest_message = (
                    "Restored ISO data expired and was cleaned up; re-initiate recovery to "
                    "request a new restore."
                )
                processed += 1
        return processed

    def _summary(self, record: AcceptanceRecoverySessionRecord) -> RecoverySessionSummary:
        image = self.state.finalized_images_by_id[record.image_id]
        glacier = self.state.glacier_status(image.finalized_id)
        pricing_basis = _acceptance_pricing_basis()
        stored_bytes = int(glacier.stored_bytes or image.bytes)
        total_gib = Decimal(stored_bytes) / _BYTES_PER_GIB
        hold_days = 1
        retrieval_cost = (total_gib * Decimal("0.0025")).quantize(_USD_QUANTUM)
        request_fees = Decimal("0.000025").quantize(_USD_QUANTUM)
        temporary_storage_cost = (
            total_gib
            * Decimal(str(pricing_basis.standard_storage_rate_usd_per_gib_month))
            * Decimal(hold_days)
            / Decimal(30)
        ).quantize(_USD_QUANTUM)
        estimate = RecoveryCostEstimate(
            currency_code=pricing_basis.currency_code or "USD",
            retrieval_tier="bulk",
            hold_days=hold_days,
            image_count=1,
            total_bytes=stored_bytes,
            restore_request_count=1,
            retrieval_rate_usd_per_gib=0.0025,
            request_rate_usd_per_1000=0.025,
            standard_storage_rate_usd_per_gib_month=(
                pricing_basis.standard_storage_rate_usd_per_gib_month
            ),
            retrieval_cost_usd=float(retrieval_cost),
            request_fees_usd=float(request_fees),
            temporary_storage_cost_usd=float(temporary_storage_cost),
            total_estimated_cost_usd=float(
                (retrieval_cost + request_fees + temporary_storage_cost).quantize(_USD_QUANTUM)
            ),
            assumptions=(
                "Excludes network egress or operator-local media costs.",
                "Uses the configured ready-to-download cleanup window.",
                "Assumes one archive restore request per image.",
            ),
        )
        warnings = (
            "Archive restore requests take time; the configured restore latency "
            "estimate is short in test fixtures.",
            "Riverhog will notify and remind the operator through the configured "
            "recovery webhook in test fixtures.",
            "Restored ISO data will be cleaned up after the configured ready "
            "window if recovery is not completed sooner.",
        )
        return RecoverySessionSummary(
            id=record.session_id,
            state=record.state,
            created_at=record.created_at,
            approved_at=record.approved_at,
            restore_requested_at=record.restore_requested_at,
            restore_ready_at=record.restore_ready_at,
            restore_expires_at=record.restore_expires_at,
            completed_at=record.completed_at,
            latest_message=record.latest_message,
            warnings=warnings,
            cost_estimate=estimate,
            notification=RecoveryNotificationStatus(
                webhook_configured=True,
                reminder_count=record.reminder_count,
                next_reminder_at=record.next_reminder_at,
                last_notified_at=record.last_notified_at,
            ),
            images=(
                RecoverySessionImage(
                    id=record.image_id,
                    filename=image.filename,
                    glacier=glacier,
                    stored_bytes=stored_bytes,
                ),
            ),
        )


class AcceptanceGlacierReportingService:
    def __init__(self, state: AcceptanceState) -> None:
        self.state = state

    def get_report(
        self,
        *,
        image_id: str | None = None,
        collection: str | None = None,
    ) -> GlacierUsageReport:
        pricing_basis = _acceptance_pricing_basis()
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
        image_reports = tuple(
            _acceptance_glacier_image(image, self.state, pricing_basis)
            for image in images
        )
        collection_reports = tuple(
            _acceptance_glacier_collections(
                images=images,
                state=self.state,
                collection_filter=collection,
                pricing_basis=pricing_basis,
            )
        )
        if collection is None:
            totals = GlacierUsageTotals(
                images=len(image_reports),
                uploaded_images=sum(
                    1 for image in image_reports if image.measured_storage_bytes > 0
                ),
                measured_storage_bytes=sum(image.measured_storage_bytes for image in image_reports),
                estimated_billable_bytes=sum(
                    image.estimated_billable_bytes for image in image_reports
                ),
                estimated_monthly_cost_usd=_round_usd(
                    sum(image.estimated_monthly_cost_usd for image in image_reports)
                ),
            )
        else:
            totals = GlacierUsageTotals(
                images=len(
                    {
                        contribution.image_id
                        for report in collection_reports
                        for contribution in report.images
                    }
                ),
                uploaded_images=len(
                    {
                        contribution.image_id
                        for report in collection_reports
                        for contribution in report.images
                        if contribution.derived_stored_bytes is not None
                    }
                ),
                measured_storage_bytes=sum(
                    report.derived_stored_bytes for report in collection_reports
                ),
                estimated_billable_bytes=sum(
                    report.derived_billable_bytes for report in collection_reports
                ),
                estimated_monthly_cost_usd=_round_usd(
                    sum(report.estimated_monthly_cost_usd for report in collection_reports)
                ),
            )
        history = (
            tuple(self.state.glacier_usage_snapshots)
            if image_id is None and collection is None
            else ()
        )
        billing = _acceptance_glacier_billing(
            self.state,
            include=image_id is None and collection is None,
        )
        return GlacierUsageReport(
            scope=_acceptance_glacier_scope(image_id=image_id, collection=collection),
            measured_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            pricing_basis=pricing_basis,
            totals=totals,
            images=image_reports,
            collections=collection_reports,
            history=history,
            billing=billing,
        )


def _acceptance_isoformat(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


_BYTES_PER_GIB = Decimal(1024**3)
_USD_QUANTUM = Decimal("0.000000000001")


def _acceptance_glacier_scope(*, image_id: str | None, collection: str | None) -> str:
    if image_id is not None and collection is not None:
        return "filtered"
    if image_id is not None:
        return "image"
    if collection is not None:
        return "collection"
    return "all"


def _acceptance_glacier_billing(
    state: AcceptanceState,
    *,
    include: bool,
) -> GlacierBillingSummary | None:
    if not include:
        return None
    if not state.glacier_billing_metadata_available:
        return GlacierBillingSummary(
            actuals=GlacierBillingActualsView(
                source="unavailable",
                scope="unavailable",
                notes=("AWS Cost Explorer billing is unavailable for this runtime.",),
            ),
            forecast=GlacierBillingForecastView(
                source="unavailable",
                scope="unavailable",
                notes=("AWS Cost Explorer forecast is unavailable for this runtime.",),
            ),
            exports=GlacierBillingExportView(
                source="unavailable",
                scope="unavailable",
                notes=("CUR or Data Exports billing detail is unavailable for this runtime.",),
            ),
            invoices=GlacierBillingInvoicesView(
                source="unavailable",
                scope="unavailable",
                notes=("AWS invoice summaries are unavailable for this runtime.",),
            ),
            notes=("AWS Cost Explorer billing is unavailable for this runtime.",),
        )
    config = load_runtime_config()
    return GlacierBillingSummary(
        actuals=GlacierBillingActualsView(
            source="aws_cost_explorer_resource",
            scope="bucket",
            filter_label=config.glacier_bucket,
            service="Amazon Simple Storage Service",
            billing_view_arn="arn:aws:billing::123456789012:billingview/primary",
            granularity="DAILY",
            periods=(
                GlacierBillingActual(
                    start="2026-04-14",
                    end="2026-04-15",
                    estimated=False,
                    unblended_cost_usd=0.44,
                    usage_quantity=11.0,
                    usage_unit="GB-Mo",
                ),
            ),
            notes=(
                (
                    "Bucket-scoped Cost Explorer actuals use AWS resource-level daily "
                    "data and are limited to the last 14 days."
                ),
                "Riverhog queried bucket-scoped actuals through the resolved AWS billing view.",
            ),
        ),
        forecast=GlacierBillingForecastView(
            source="aws_cost_explorer",
            scope="service",
            filter_label=f"Amazon Simple Storage Service in {config.glacier_pricing_region_code}",
            service="Amazon Simple Storage Service",
            currency_code=config.glacier_billing_currency_code,
            granularity="MONTHLY",
            periods=(
                GlacierBillingForecast(
                    start="2026-05-01",
                    end="2026-06-01",
                    mean_cost_usd=14.5,
                    lower_bound_cost_usd=11.0,
                    upper_bound_cost_usd=18.0,
                    currency_code=config.glacier_billing_currency_code,
                ),
            ),
            notes=(
                (
                    "AWS Cost Explorer forecast does not expose bucket-resource "
                    "forecasting, so Riverhog falls back to tag-scoped or "
                    "service-scoped forecast data."
                ),
            ),
        ),
        exports=GlacierBillingExportView(
            source="aws_data_exports_s3",
            scope="bucket",
            filter_label=config.glacier_bucket,
            service="Amazon Simple Storage Service",
            export_arn="arn:aws:bcm-data-exports:us-east-1:123456789012:export/glacier",
            export_name="glacier-export",
            execution_id="execution-0002",
            manifest_key="billing/glacier-export/metadata/execution-0002/manifest.json",
            billing_period="2026-04-01..2026-05-01",
            bucket="billing-bucket",
            prefix="billing",
            object_key=None,
            exported_at="2026-04-28T08:00:00Z",
            currency_code=config.glacier_billing_currency_code,
            files_read=2,
            rows_scanned=4,
            breakdowns=(
                GlacierBillingExportBreakdown(
                    usage_type="TimedStorage-GlacierByteHrs",
                    operation="StandardStorage",
                    resource_id=f"arn:aws:s3:::{config.glacier_bucket}",
                    tag_value=None,
                    unblended_cost_usd=2.0,
                    usage_quantity=150.0,
                    usage_unit="GB-Mo",
                ),
            ),
            notes=(
                (
                    "Riverhog selected the AWS Data Exports manifest for the latest "
                    "successful execution."
                ),
            ),
        ),
        invoices=GlacierBillingInvoicesView(
            source="aws_invoicing",
            scope="account",
            account_id="123456789012",
            invoices=(
                GlacierBillingInvoiceSummary(
                    invoice_id="INV-001",
                    account_id="123456789012",
                    billing_period_start="2026-04-01",
                    billing_period_end="2026-05-01",
                    invoice_type="Invoice",
                    invoicing_entity="Amazon Web Services, Inc.",
                    issued_at="2026-05-01T00:00:00Z",
                    due_at="2026-05-08T00:00:00Z",
                    base_currency_code="USD",
                    base_total_amount=99.5,
                    payment_currency_code="USD",
                    payment_total_amount=99.5,
                    original_invoice_id=None,
                ),
            ),
            notes=(
                (
                    "AWS invoice summaries are account-level totals and do not "
                    "attribute cost to a single Glacier bucket."
                ),
            ),
        ),
        notes=(),
    )


def _acceptance_pricing_basis() -> GlacierPricingBasis:
    config = load_runtime_config()
    return GlacierPricingBasis(
        label=config.glacier_pricing_label,
        source="manual",
        storage_class=config.glacier_storage_class,
        currency_code=config.glacier_pricing_currency_code,
        region_code=config.glacier_pricing_region_code,
        effective_at=None,
        price_list_arn=None,
        glacier_storage_rate_usd_per_gib_month=config.glacier_storage_rate_usd_per_gib_month,
        standard_storage_rate_usd_per_gib_month=config.glacier_standard_rate_usd_per_gib_month,
        archived_metadata_bytes_per_object=config.glacier_archived_metadata_bytes_per_object,
        standard_metadata_bytes_per_object=config.glacier_standard_metadata_bytes_per_object,
        minimum_storage_duration_days=config.glacier_minimum_storage_duration_days,
    )


def _acceptance_glacier_status(
    state: AcceptanceState,
    image_id: str,
) -> GlacierArchiveStatus:
    return state.glacier_status(image_id)


def _acceptance_glacier_image(
    image: CandidateRecord,
    state: AcceptanceState,
    pricing_basis: GlacierPricingBasis,
) -> GlacierUsageImage:
    glacier = _acceptance_glacier_status(state, image.finalized_id)
    measured_storage_bytes = (
        int(glacier.stored_bytes or 0) if glacier.state == GlacierState.UPLOADED else 0
    )
    return GlacierUsageImage(
        id=image.finalized_id,
        filename=image.filename,
        collection_ids=image.collections,
        glacier=glacier,
        measured_storage_bytes=measured_storage_bytes,
        estimated_billable_bytes=_acceptance_billable_bytes(
            measured_storage_bytes,
            pricing_basis=pricing_basis,
        ),
        estimated_monthly_cost_usd=_acceptance_estimated_monthly_cost_usd(
            measured_storage_bytes,
            object_count=1 if measured_storage_bytes > 0 else 0,
            pricing_basis=pricing_basis,
        ),
    )


def _acceptance_glacier_collections(
    *,
    images: list[CandidateRecord],
    state: AcceptanceState,
    collection_filter: str | None,
    pricing_basis: GlacierPricingBasis,
) -> list[GlacierUsageCollection]:
    collections: dict[str, list[GlacierCollectionContribution]] = defaultdict(list)
    collection_total_bytes: dict[str, int] = {}
    for collection_id, records in state.files_by_collection.items():
        collection_total_bytes[str(collection_id)] = sum(
            record.bytes for record in records.values()
        )

    for image in images:
        represented_by_collection = _acceptance_represented_bytes_by_collection(state, image)
        if collection_filter is not None:
            represented_by_collection = {
                collection_id: represented
                for collection_id, represented in represented_by_collection.items()
                if collection_id == collection_filter
            }
        total_represented_bytes = sum(represented_by_collection.values())
        glacier = _acceptance_glacier_status(state, image.finalized_id)
        measured_storage_bytes = (
            int(glacier.stored_bytes or 0) if glacier.state == GlacierState.UPLOADED else 0
        )
        billable_bytes = _acceptance_billable_bytes(
            measured_storage_bytes,
            pricing_basis=pricing_basis,
        )
        for collection_id, represented_bytes in represented_by_collection.items():
            represented_fraction = (
                represented_bytes / total_represented_bytes if total_represented_bytes > 0 else None
            )
            if represented_fraction is None or measured_storage_bytes <= 0:
                derived_stored_bytes = None
                derived_billable_bytes = None
                estimated_monthly_cost_usd = None
            else:
                derived_stored_bytes = _round_int(measured_storage_bytes * represented_fraction)
                derived_billable_bytes = _round_int(billable_bytes * represented_fraction)
                estimated_monthly_cost_usd = _round_usd(
                    _acceptance_estimated_monthly_cost_usd(
                        derived_stored_bytes,
                        object_count=0,
                        pricing_basis=pricing_basis,
                        archived_metadata_bytes=pricing_basis.archived_metadata_bytes_per_object
                        * represented_fraction,
                        standard_metadata_bytes=pricing_basis.standard_metadata_bytes_per_object
                        * represented_fraction,
                    )
                )
            collections[collection_id].append(
                GlacierCollectionContribution(
                    image_id=image.finalized_id,
                    filename=image.filename,
                    glacier=glacier,
                    represented_bytes=represented_bytes,
                    represented_fraction=represented_fraction,
                    derived_stored_bytes=derived_stored_bytes,
                    derived_billable_bytes=derived_billable_bytes,
                    estimated_monthly_cost_usd=estimated_monthly_cost_usd,
                )
            )

    reports: list[GlacierUsageCollection] = []
    for collection_id in sorted(collections):
        contributions = sorted(
            collections[collection_id],
            key=lambda current: str(current.image_id),
            reverse=True,
        )
        reports.append(
            GlacierUsageCollection(
                id=collection_id,
                bytes=collection_total_bytes.get(collection_id, 0),
                represented_bytes=sum(item.represented_bytes for item in contributions),
                attribution_state=(
                    "derived"
                    if any(item.derived_stored_bytes is not None for item in contributions)
                    else "unavailable"
                ),
                derived_stored_bytes=sum(item.derived_stored_bytes or 0 for item in contributions),
                derived_billable_bytes=sum(
                    item.derived_billable_bytes or 0 for item in contributions
                ),
                estimated_monthly_cost_usd=_round_usd(
                    sum(item.estimated_monthly_cost_usd or 0.0 for item in contributions)
                ),
                images=tuple(contributions),
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


def _acceptance_glacier_snapshot(state: AcceptanceState) -> GlacierUsageSnapshot:
    pricing_basis = _acceptance_pricing_basis()
    image_reports = tuple(
        _acceptance_glacier_image(image, state, pricing_basis)
        for image in sorted(
            state.finalized_images_by_id.values(),
            key=lambda current: current.finalized_id,
            reverse=True,
        )
    )
    return GlacierUsageSnapshot(
        captured_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        uploaded_images=sum(1 for image in image_reports if image.measured_storage_bytes > 0),
        measured_storage_bytes=sum(image.measured_storage_bytes for image in image_reports),
        estimated_billable_bytes=sum(image.estimated_billable_bytes for image in image_reports),
        estimated_monthly_cost_usd=_round_usd(
            sum(image.estimated_monthly_cost_usd for image in image_reports)
        ),
    )


def _acceptance_billable_bytes(
    measured_storage_bytes: int,
    *,
    pricing_basis: GlacierPricingBasis,
) -> int:
    if measured_storage_bytes <= 0:
        return 0
    return (
        measured_storage_bytes
        + pricing_basis.archived_metadata_bytes_per_object
        + pricing_basis.standard_metadata_bytes_per_object
    )


def _acceptance_estimated_monthly_cost_usd(
    measured_storage_bytes: int,
    *,
    object_count: int,
    pricing_basis: GlacierPricingBasis,
    archived_metadata_bytes: float | None = None,
    standard_metadata_bytes: float | None = None,
) -> float:
    archived_bytes = Decimal(measured_storage_bytes) + Decimal(
        str(
            archived_metadata_bytes
            if archived_metadata_bytes is not None
            else pricing_basis.archived_metadata_bytes_per_object * object_count
        )
    )
    standard_bytes = Decimal(
        str(
            standard_metadata_bytes
            if standard_metadata_bytes is not None
            else pricing_basis.standard_metadata_bytes_per_object * object_count
        )
    )
    glacier_rate = Decimal(str(pricing_basis.glacier_storage_rate_usd_per_gib_month))
    standard_rate = Decimal(str(pricing_basis.standard_storage_rate_usd_per_gib_month))
    return float(
        (
            (archived_bytes / _BYTES_PER_GIB * glacier_rate)
            + (standard_bytes / _BYTES_PER_GIB * standard_rate)
        ).quantize(_USD_QUANTUM, rounding=ROUND_HALF_UP)
    )


def _split_part_length(total_bytes: int, *, part_count: int, part_index: int) -> int:
    base, remainder = divmod(total_bytes, part_count)
    return base + int(part_index < remainder)


def _round_usd(value: float) -> float:
    return float(Decimal(str(value)).quantize(_USD_QUANTUM, rounding=ROUND_HALF_UP))


def _round_int(value: float) -> int:
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


class AcceptanceCopyService:
    def __init__(self, state: AcceptanceState) -> None:
        self.state = state

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

    def register(
        self,
        image_id: str,
        location: str,
        *,
        copy_id: str | None = None,
    ) -> CopySummary:
        image = self.state.finalized_images_by_id.get(ImageId(image_id))
        if image is None:
            raise NotFound(f"image not found: {image_id}")
        self.state.ensure_required_copy_slots(image.finalized_id)
        target = self._registration_target(image.finalized_id, copy_id)
        summary = CopySummary(
            id=target.id,
            volume_id=image.finalized_id,
            label_text=target.label_text,
            location=location,
            created_at=target.created_at,
            state=CopyState.REGISTERED,
            verification_state=target.verification_state,
            history=(
                *target.history,
                CopyHistoryEntry(
                    at=DEFAULT_COPY_CREATED_AT,
                    event="registered",
                    state=CopyState.REGISTERED,
                    verification_state=target.verification_state,
                    location=location,
                ),
            ),
        )
        scoped_key = (image.finalized_id, target.id)
        self.state.copy_summaries[scoped_key] = summary
        disc_info = self._read_disc_part_info(image)
        for collection_id, path in image.covered_paths:
            record = self.state.files_by_collection[collection_id][path]
            record.archived = True
            if all(
                (existing.id, existing.volume_id) != (target.id, image.finalized_id)
                for existing in record.copies
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
                record.copies.append(
                    AcceptanceState._copy_from_dict(
                        build_file_copy(
                            copy_id=str(target.id),
                            volume_id=image.finalized_id,
                            location=location,
                            collection_id=str(collection_id),
                            path=path,
                            **kwargs,
                        )
                    )
                )
        return summary

    def list_for_image(self, image_id: str) -> list[CopySummary]:
        self.state.ensure_required_copy_slots(image_id)
        return [
            summary
            for (volume_id, _copy_id), summary in sorted(self.state.copy_summaries.items())
            if volume_id == image_id
        ]

    def update(
        self,
        image_id: str,
        copy_id: str,
        *,
        location: str | None = None,
        state: str | None = None,
        verification_state: str | None = None,
    ) -> CopySummary:
        self.state.ensure_required_copy_slots(image_id)
        scoped_key = (image_id, CopyId(copy_id))
        summary = self.state.copy_summaries.get(scoped_key)
        if summary is None:
            raise NotFound(f"copy not found for image: {copy_id}")
        previous_state = summary.state
        next_state = CopyState(state) if state is not None else summary.state
        next_verification_state = (
            VerificationState(verification_state)
            if verification_state is not None
            else summary.verification_state
        )
        next_location = location if location is not None else summary.location
        location_changed = location is not None and location != summary.location
        state_changed = next_state != summary.state
        verification_changed = next_verification_state != summary.verification_state
        updated = CopySummary(
            id=summary.id,
            volume_id=summary.volume_id,
            label_text=summary.label_text,
            location=next_location,
            created_at=summary.created_at,
            state=next_state,
            verification_state=next_verification_state,
            history=(
                *summary.history,
                CopyHistoryEntry(
                    at=DEFAULT_COPY_CREATED_AT,
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
        self.state.copy_summaries[scoped_key] = updated
        self._sync_file_copy_visibility(updated)
        if copy_counts_toward_protection(previous_state.value) and next_state in {
            CopyState.LOST,
            CopyState.DAMAGED,
        }:
            self.state.ensure_required_copy_slots(image_id)
            self.state.ensure_glacier_recovery_session(image_id)
        return updated

    def _registration_target(self, image_id: str, copy_id: str | None) -> CopySummary:
        copies = self.list_for_image(image_id)
        if copy_id is None:
            for summary in copies:
                if summary.state in {CopyState.NEEDED, CopyState.BURNING}:
                    return summary
            raise Conflict("all required copy slots are already registered")
        for summary in copies:
            if str(summary.id) != copy_id:
                continue
            if summary.state not in {CopyState.NEEDED, CopyState.BURNING}:
                raise Conflict(f"copy is not available for registration: {copy_id}")
            return summary
        raise NotFound(f"copy not found for image: {copy_id}")

    def _sync_file_copy_visibility(self, summary: CopySummary) -> None:
        for records in self.state.files_by_collection.values():
            for record in records.values():
                remaining_copies: list[FileCopy] = []
                updated_existing = False
                for copy in record.copies:
                    if (copy.volume_id, str(copy.id)) != (summary.volume_id, str(summary.id)):
                        remaining_copies.append(copy)
                        continue
                    if copy_counts_toward_protection(summary.state.value) and summary.location:
                        remaining_copies.append(
                            FileCopy(
                                id=copy.id,
                                volume_id=copy.volume_id,
                                location=summary.location,
                                disc_path=copy.disc_path,
                                enc=copy.enc,
                                part_index=copy.part_index,
                                part_count=copy.part_count,
                                part_bytes=copy.part_bytes,
                                part_sha256=copy.part_sha256,
                            )
                        )
                        updated_existing = True
                record.copies = remaining_copies
                record.archived = bool(record.copies)
                if updated_existing and summary.location:
                    record.archived = True


class AcceptanceFetchService:
    def __init__(self, state: AcceptanceState) -> None:
        self.state = state

    def find_reusable_fetch(self, target: TargetStr) -> FetchSummary | None:
        for record in self.state.fetches.values():
            if record.summary.target != target:
                continue
            if record.summary.state == FetchState.FAILED:
                continue
            return record.summary
        return None

    def create_fetch(
        self,
        target: TargetStr,
        files: list[StoredFile],
        *,
        fetch_id: str | None = None,
        initial_state: FetchState = FetchState.WAITING_MEDIA,
    ) -> FetchSummary:
        if fetch_id is None:
            self.state.next_fetch_number += 1
            fetch_id = f"fx-{self.state.next_fetch_number}"
        else:
            self.state.reserve_fetch_id(fetch_id)
        fetch_key = FetchId(fetch_id)
        entries = {
            EntryId(f"e{index}"): FetchEntryRecord(
                id=EntryId(f"e{index}"),
                collection_id=record.collection_id,
                path=record.path,
                bytes=record.bytes,
                sha256=record.sha256,
                content=record.content,
                copies=list(record.copies),
            )
            for index, record in enumerate(sorted(files, key=lambda item: item.path), start=1)
        }
        summary = FetchSummary(
            id=fetch_key,
            target=target,
            state=initial_state,
            files=len(entries),
            bytes=sum(entry.bytes for entry in entries.values()),
            copies=self._summary_copies(entries.values()),
        )
        record = FetchRecord(summary=summary, entries=entries)
        record.summary = self._replace_summary(record, state=initial_state)
        self.state.fetches[fetch_key] = record
        return record.summary

    def find_for_target(self, target: TargetStr) -> FetchSummary:
        summary = self.find_reusable_fetch(target)
        if summary is None:
            raise NotFound(f"fetch not found for target: {target}")
        return summary

    def remove_for_target(self, target: TargetStr) -> None:
        to_delete = [
            fetch_id
            for fetch_id, record in self.state.fetches.items()
            if record.summary.target == target
        ]
        for fetch_id in to_delete:
            del self.state.fetches[fetch_id]

    def get(self, fetch_id: str) -> FetchSummary:
        record = self._record(fetch_id)
        self._expire_stale_upload_record(record)
        record.summary = self._replace_summary(record)
        return record.summary

    def manifest(self, fetch_id: str) -> dict[str, object]:
        record = self._record(fetch_id)
        self._expire_stale_upload_record(record)
        record.summary = self._replace_summary(record)
        return {
            "id": str(record.summary.id),
            "target": str(record.summary.target),
            "entries": [
                {
                    "id": str(entry.id),
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
                    "copies": [self._manifest_copy(entry, copy) for copy in entry.copies],
                    "parts": self._manifest_parts(entry),
                }
                for entry in record.entries.values()
            ],
        }

    def create_or_resume_upload(self, fetch_id: str, entry_id: str) -> dict[str, object]:
        record = self._record(fetch_id)
        self._expire_stale_upload_record(record)
        if record.summary.state == FetchState.DONE:
            raise InvalidState("fetch is already complete")
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

        if record.summary.state == FetchState.WAITING_MEDIA:
            record.summary = self._replace_summary(record, state=FetchState.UPLOADING)
        else:
            record.summary = self._replace_summary(record)

        return {
            "offset": entry.uploaded_bytes,
            "length": self._entry_recovery_bytes(entry),
            "expires_at": entry.upload_expires_at,
        }

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
            record.summary = self._replace_summary(record, state=FetchState.WAITING_MEDIA)
        else:
            record.summary = self._replace_summary(record)

    def expire_stale_uploads(self) -> None:
        for record in self.state.fetches.values():
            self._expire_stale_upload_record(record)

    def complete(self, fetch_id: str) -> dict[str, object]:
        record = self._record(fetch_id)
        if record.summary.state == FetchState.DONE:
            return {
                "id": str(record.summary.id),
                "state": record.summary.state.value,
                "hot": self._hot_payload(str(record.summary.target)),
            }
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
        hot = self._hot_payload(str(record.summary.target))
        return {
            "id": str(record.summary.id),
            "state": record.summary.state.value,
            "hot": hot,
        }

    def upload_all_required_entries(self, fetch_id: str) -> None:
        record = self._record(fetch_id)
        for entry in record.entries.values():
            recovery_stream = b"".join(self._entry_recovery_payloads(entry))
            entry.uploaded_bytes = len(recovery_stream)
            entry.uploaded_content = recovery_stream
            entry.upload_expires_at = None
        if record.summary.state == FetchState.WAITING_MEDIA:
            record.summary = self._replace_summary(record, state=FetchState.UPLOADING)
        else:
            record.summary = self._replace_summary(record)

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
        if record.summary.state == FetchState.WAITING_MEDIA:
            record.summary = self._replace_summary(record, state=FetchState.UPLOADING)
        else:
            record.summary = self._replace_summary(record)
        return len(partial)

    def _record(self, fetch_id: str) -> FetchRecord:
        try:
            return self.state.fetches[FetchId(fetch_id)]
        except KeyError as exc:
            raise NotFound(f"fetch not found: {fetch_id}") from exc

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
            target=summary.target,
            state=effective_state,
            files=summary.files,
            bytes=summary.bytes,
            copies=list(summary.copies),
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
            record.summary = self._replace_summary(record, state=FetchState.WAITING_MEDIA)
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
    def _summary_copies(entries: Iterator[FetchEntryRecord]) -> list[FetchCopyHint]:
        out: list[FetchCopyHint] = []
        seen: set[tuple[str, CopyId]] = set()
        for entry in entries:
            for copy in entry.copies:
                key = (copy.volume_id, copy.id)
                if key in seen:
                    continue
                seen.add(key)
                out.append(copy.hint)
        return out

    def _manifest_copy(self, entry: FetchEntryRecord, copy: FileCopy) -> dict[str, object]:
        recovery_payload = self._copy_recovery_payload(entry, copy)
        return {
            "copy": str(copy.id),
            "volume_id": copy.volume_id,
            "location": copy.location,
            "disc_path": copy.disc_path,
            "recovery_bytes": len(recovery_payload),
            "recovery_sha256": hashlib.sha256(recovery_payload).hexdigest(),
        }

    def _manifest_parts(self, entry: FetchEntryRecord) -> list[dict[str, object]]:
        if not entry.copies:
            return []

        if all(copy.part_index is None for copy in entry.copies):
            return [
                {
                    "index": 0,
                    "bytes": entry.bytes,
                    "sha256": str(entry.sha256),
                    "recovery_bytes": self._entry_recovery_bytes(entry),
                    "copies": [self._manifest_copy(entry, copy) for copy in entry.copies],
                }
            ]

        part_count = max((copy.part_count or 1) for copy in entry.copies)
        parts: list[dict[str, object]] = []
        for part_index in range(part_count):
            part_copies = [copy for copy in entry.copies if copy.part_index == part_index]
            if not part_copies:
                raise NotFound(f"missing copy hints for part {part_index} of entry {entry.id}")
            bytes_hint = part_copies[0].part_bytes
            sha256_hint = part_copies[0].part_sha256
            if bytes_hint is None or sha256_hint is None:
                raise NotFound(f"missing part metadata for part {part_index} of entry {entry.id}")
            parts.append(
                {
                    "index": part_index,
                    "bytes": bytes_hint,
                    "sha256": sha256_hint,
                    "recovery_bytes": len(self._copy_recovery_payload(entry, part_copies[0])),
                    "copies": [self._manifest_copy(entry, copy) for copy in part_copies],
                }
            )
        return parts

    def _entry_recovery_payloads(self, entry: FetchEntryRecord) -> tuple[bytes, ...]:
        if not entry.copies or all(copy.part_index is None for copy in entry.copies):
            return (fixture_encrypt_bytes(entry.content),)
        part_count = max((copy.part_count or 1) for copy in entry.copies)
        return tuple(
            fixture_encrypt_bytes(part)
            for part in split_fixture_plaintext(entry.content, part_count)
        )

    def _entry_recovery_bytes(self, entry: FetchEntryRecord) -> int:
        return sum(len(payload) for payload in self._entry_recovery_payloads(entry))

    def _copy_recovery_payload(self, entry: FetchEntryRecord, copy: FileCopy) -> bytes:
        payloads = self._entry_recovery_payloads(entry)
        if copy.part_index is None:
            return payloads[0]
        return payloads[copy.part_index]

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

    def _hot_payload(self, raw_target: str) -> dict[str, object]:
        selected = self.state.selected_files(raw_target)
        present_bytes = sum(record.bytes for record in selected if record.hot)
        missing_bytes = sum(record.bytes for record in selected if not record.hot)
        return {
            "state": "ready" if missing_bytes == 0 else "waiting",
            "present_bytes": present_bytes,
            "missing_bytes": missing_bytes,
        }


class AcceptancePinService:
    def __init__(self, state: AcceptanceState, fetches: AcceptanceFetchService) -> None:
        self.state = state
        self.fetches = fetches

    def pin(self, raw_target: str) -> dict[str, object]:
        target = parse_target(raw_target)
        canonical = cast(TargetStr, target.canonical)
        selected = self.state.selected_files(target.canonical)
        self.state.exact_pins.add(canonical)

        present_bytes = sum(record.bytes for record in selected if record.hot)
        missing_bytes = sum(record.bytes for record in selected if not record.hot)
        summary = self.fetches.find_reusable_fetch(canonical)
        if summary is None:
            summary = self.fetches.create_fetch(
                canonical,
                selected,
                initial_state=FetchState.DONE if missing_bytes == 0 else FetchState.WAITING_MEDIA,
            )
        fetch_payload = {
            "id": str(summary.id),
            "state": summary.state.value,
            "copies": [
                {"id": str(copy.id), "volume_id": copy.volume_id, "location": copy.location}
                for copy in summary.copies
            ],
        }
        return {
            "target": str(canonical),
            "pin": True,
            "hot": {
                "state": "ready" if missing_bytes == 0 else "waiting",
                "present_bytes": present_bytes,
                "missing_bytes": missing_bytes,
            },
            "fetch": fetch_payload,
        }

    def release(self, raw_target: str) -> dict[str, object]:
        target = parse_target(raw_target)
        canonical = cast(TargetStr, target.canonical)
        removed = canonical in self.state.exact_pins
        self.state.exact_pins.discard(canonical)
        if removed:
            self.fetches.remove_for_target(canonical)
        self.state.reconcile_hot_from_pins()
        return {
            "target": str(canonical),
            "pin": False,
        }

    def list_pins(self) -> list[PinSummary]:
        return [
            PinSummary(target=target, fetch=self.fetches.find_for_target(target))
            for target in sorted(self.state.exact_pins)
        ]


class AcceptanceFileService:
    def __init__(self, state: AcceptanceState) -> None:
        self.state = state

    def list_collection_files(
        self,
        collection_id: str,
        *,
        page: int,
        per_page: int,
    ) -> dict[str, object]:
        records = sorted(
            [
                {
                    "path": record.path,
                    "bytes": record.bytes,
                    "hot": record.hot,
                    "archived": record.archived,
                }
                for record in self.state.collection_files(collection_id)
            ],
            key=lambda r: str(r["path"]),
        )
        total = len(records)
        pages = math.ceil(total / per_page) if total else 0
        start = (page - 1) * per_page
        return {
            "collection_id": collection_id,
            "page": page,
            "per_page": per_page,
            "total": total,
            "pages": pages,
            "files": records[start : start + per_page],
        }

    def query_by_target(
        self,
        raw_target: str,
        *,
        page: int,
        per_page: int,
    ) -> dict[str, object]:
        from arc_core.domain.selectors import parse_target

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
                    "archived": record.archived,
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

    def get_content(self, raw_target: str) -> bytes:
        from arc_core.domain.errors import InvalidTarget, NotFound
        from arc_core.domain.selectors import parse_target

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
            f"Timed out waiting for live arc test server at {self.base_url}"
        ) from last_error

    def close(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5.0)
        if self._thread.is_alive():  # pragma: no cover
            raise RuntimeError("Timed out stopping live arc test server")


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
    glacier_uploads: AcceptanceGlacierUploadService
    glacier_reporting: AcceptanceGlacierReportingService
    recovery_sessions: AcceptanceRecoverySessionService
    copies: AcceptanceCopyService
    pins: AcceptancePinService
    fetches: AcceptanceFetchService
    files: AcceptanceFileService
    _container_slot: _ContainerSlot

    @classmethod
    def create(cls, workspace: Path) -> AcceptanceSystem:
        with time_block("fixture.acceptance_system.create"):
            _clear_workspace(workspace)
            state = AcceptanceState()
            system = cls._build_runtime(workspace, state)
            system.server.start()
            system.state.public_base_url = system.server.base_url
            return system

    @classmethod
    def _build_runtime(cls, workspace: Path, state: AcceptanceState) -> AcceptanceSystem:
        collections = AcceptanceCollectionService(state)
        search = AcceptanceSearchService(state)
        planning = AcceptancePlanningService(state)
        glacier_uploads = AcceptanceGlacierUploadService(state)
        glacier_reporting = AcceptanceGlacierReportingService(state)
        recovery_sessions = AcceptanceRecoverySessionService(state)
        copies = AcceptanceCopyService(state)
        fetches = AcceptanceFetchService(state)
        pins = AcceptancePinService(state, fetches)
        files = AcceptanceFileService(state)

        container = ServiceContainer(
            collections=collections,
            search=search,
            planning=planning,
            glacier_uploads=glacier_uploads,
            glacier_reporting=glacier_reporting,
            recovery_sessions=recovery_sessions,
            copies=copies,
            pins=pins,
            fetches=fetches,
            files=files,
        )
        container_slot = _ContainerSlot(container=container)
        app = create_app(
            container_provider=lambda: container_slot.container,
            upload_expiry_reaper_interval=_UPLOAD_EXPIRY_SWEEP_INTERVAL_SECONDS,
            glacier_upload_reaper_interval=_GLACIER_UPLOAD_SWEEP_INTERVAL_SECONDS,
            glacier_recovery_reaper_interval=_GLACIER_RECOVERY_SWEEP_INTERVAL_SECONDS,
        )
        fixture_path = workspace / "arc_disc_fixture.json"
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
            glacier_uploads=glacier_uploads,
            glacier_reporting=glacier_reporting,
            recovery_sessions=recovery_sessions,
            copies=copies,
            pins=pins,
            fetches=fetches,
            files=files,
            _container_slot=container_slot,
        )

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
            self.collections = restarted.collections
            self.search = restarted.search
            self.planning = restarted.planning
            self.glacier_uploads = restarted.glacier_uploads
            self.glacier_reporting = restarted.glacier_reporting
            self.recovery_sessions = restarted.recovery_sessions
            self.copies = restarted.copies
            self.pins = restarted.pins
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
            self.glacier_uploads = reset.glacier_uploads
            self.glacier_reporting = reset.glacier_reporting
            self.recovery_sessions = reset.recovery_sessions
            self.copies = reset.copies
            self.pins = reset.pins
            self.fetches = reset.fetches
            self.files = reset.files
            self._container_slot.container = reset._container_slot.container

    def close(self) -> None:
        with time_block("fixture.acceptance_system.close"):
            self.server.close()

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

    def seed_finalized_image(self, candidate_id: str, *, force_ready: bool = False) -> None:
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
        self.state.enqueue_glacier_upload(candidate)

    def wait_for_image_glacier_state(
        self,
        image_id: str,
        state: str,
        *,
        timeout: float = 5.0,
    ) -> dict[str, object]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            response = self.request("GET", f"/v1/images/{image_id}")
            assert response.status_code == 200, response.text
            payload = response.json()
            if payload["glacier"]["state"] == state:
                return payload
            time.sleep(0.05)
        raise AssertionError(f"timed out waiting for image glacier state {image_id} -> {state}")

    def wait_for_recovery_session_state(
        self,
        session_id: str,
        state: str,
        *,
        timeout: float = 5.0,
    ) -> dict[str, object]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            response = self.request("GET", f"/v1/recovery-sessions/{session_id}")
            assert response.status_code == 200, response.text
            payload = response.json()
            if payload["state"] == state:
                return payload
            time.sleep(0.05)
        raise AssertionError(
            f"timed out waiting for recovery session state {session_id} -> {state}"
        )

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

    def fail_glacier_upload(self, image_id: str, *, error: str) -> None:
        self.state.glacier_upload_failures_by_image[ImageId(image_id)] = error

    def run_arc(self, *args: str) -> subprocess.CompletedProcess[str]:
        with time_block("subprocess arc"):
            return subprocess.run(
                [sys.executable, "-m", "arc_cli.main", *args],
                cwd=REPO_ROOT,
                env=self._subprocess_env(),
                capture_output=True,
                text=True,
                check=False,
            )

    def run_arc_disc(
        self, *args: str, input_text: str = "\n" * 16
    ) -> subprocess.CompletedProcess[str]:
        if not self.fixture_path.exists():
            self._write_arc_disc_fixture(self._default_arc_disc_fixture())
        env = self._subprocess_env(
            {
                "ARC_DISC_FIXTURE_PATH": str(self.fixture_path),
                "ARC_DISC_READER_FACTORY": "tests.fixtures.arc_disc_fakes:FixtureOpticalReader",
                "ARC_DISC_ISO_VERIFIER_FACTORY": "tests.fixtures.arc_disc_fakes:FixtureIsoVerifier",
                "ARC_DISC_BURNER_FACTORY": "tests.fixtures.arc_disc_fakes:FixtureDiscBurner",
                "ARC_DISC_BURNED_MEDIA_VERIFIER_FACTORY": (
                    "tests.fixtures.arc_disc_fakes:FixtureBurnedMediaVerifier"
                ),
                "ARC_DISC_BURN_PROMPTS_FACTORY": "tests.fixtures.arc_disc_fakes:FixtureBurnPrompts",
                "ARC_DISC_STAGING_DIR": str(self.workspace / "arc_disc_staging"),
            }
        )
        with time_block("subprocess arc-disc"):
            return subprocess.run(
                [sys.executable, "-m", "arc_disc.main", *args],
                cwd=REPO_ROOT,
                env=env,
                input=input_text,
                capture_output=True,
                text=True,
                check=False,
            )

    def delete_hot_backing_file(self, target: str) -> None:
        selected = self.state.selected_files(target)
        if len(selected) != 1:
            raise AssertionError(f"expected exactly one file target: {target}")
        selected[0].hot_backing_missing = True

    def has_committed_collection_file(self, collection_id: str, path: str) -> bool:
        normalized_collection_id = CollectionId(normalize_collection_id(collection_id))
        records = self.state.files_by_collection.get(normalized_collection_id)
        if records is None:
            return False
        record = records.get(path)
        return bool(record and record.hot and not record.hot_backing_missing)

    def collection_source_root(self, collection_id: str) -> Path:
        collection_key = CollectionId(normalize_collection_id(collection_id))
        return self.state.local_collection_sources[collection_key]

    def expire_collection_upload(self, collection_id: str) -> None:
        upload = self.state.collection_uploads[CollectionId(normalize_collection_id(collection_id))]
        for file_record in upload.files.values():
            file_record.upload_expires_at = "2000-01-01T00:00:00Z"

    def expire_fetch_upload(self, fetch_id: str, entry_id: str) -> None:
        record = self.state.fetches[FetchId(fetch_id)]
        entry = record.entries[EntryId(entry_id)]
        entry.upload_expires_at = "2000-01-01T00:00:00Z"

    def wait_for_collection_upload_cleanup(self, collection_id: str, timeout: float = 2.0) -> None:
        normalized_collection_id = CollectionId(normalize_collection_id(collection_id))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
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
                    "collection_id": normalized_collection_id,
                    "ingest_source": str(root),
                    "files": manifest,
                },
            )
            assert response.status_code == 200, response.text
            payload = response.json()
            for file_payload in payload["files"]:
                upload = self.request(
                    "POST",
                    (
                        f"/v1/collection-uploads/{normalized_collection_id}/files/"
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
            final = self.request("GET", f"/v1/collections/{normalized_collection_id}")
            assert final.status_code == 200, final.text
            return cast(dict[str, object], final.json())

    def seed_photos_hot(self) -> None:
        normalized = normalize_collection_id(PHOTOS_COLLECTION_ID)
        if CollectionId(normalized) in self.state.files_by_collection:
            return
        self.upload_collection_source(PHOTOS_COLLECTION_ID, PHOTOS_2024_FILES)

    def seed_nested_photos_hot(self) -> None:
        normalized = normalize_collection_id(PHOTOS_NESTED_COLLECTION_ID)
        if CollectionId(normalized) in self.state.files_by_collection:
            return
        self.upload_collection_source(PHOTOS_NESTED_COLLECTION_ID, PHOTOS_2024_FILES)

    def seed_parent_photos_hot(self) -> None:
        normalized = normalize_collection_id(PHOTOS_PARENT_COLLECTION_ID)
        if CollectionId(normalized) in self.state.files_by_collection:
            return
        self.upload_collection_source(PHOTOS_PARENT_COLLECTION_ID, PHOTOS_2024_FILES)

    def seed_docs_hot(self) -> None:
        normalized = normalize_collection_id(DOCS_COLLECTION_ID)
        if CollectionId(normalized) in self.state.files_by_collection:
            return
        self.upload_collection_source(DOCS_COLLECTION_ID, DOCS_FILES)

    def seed_docs_archive(self) -> None:
        docs_key = CollectionId(normalize_collection_id(DOCS_COLLECTION_ID))
        docs = self.state.files_by_collection.get(docs_key, {})
        if docs.get("tax/2022/invoice-123.pdf") and docs["tax/2022/invoice-123.pdf"].archived:
            return
        self.seed_docs_hot()
        self.seed_image_fixtures((IMAGE_FIXTURES[0],))
        resp = self.request("POST", f"/v1/plan/candidates/{IMAGE_FIXTURES[0].id}/finalize")
        assert resp.status_code == 200, resp.text
        image_id = resp.json()["id"]
        resp = self.request(
            "POST",
            f"/v1/images/{image_id}/copies",
            json_body={"location": "vault-a/shelf-01"},
        )
        assert resp.status_code == 200, resp.text
        self.state.files_by_collection[docs_key]["tax/2022/invoice-123.pdf"].hot = False

    def seed_docs_archive_with_split_invoice(self) -> None:
        docs_key = CollectionId(normalize_collection_id(DOCS_COLLECTION_ID))
        docs = self.state.files_by_collection.get(docs_key, {})
        invoice = docs.get(SPLIT_FILE_RELPATH)
        if invoice and invoice.archived and any(c.part_index is not None for c in invoice.copies):
            return
        self.seed_docs_hot()
        self.seed_image_fixtures(SPLIT_IMAGE_FIXTURES)
        for fixture, _copy_id, location in zip(
            SPLIT_IMAGE_FIXTURES,
            (SPLIT_COPY_ONE_ID, SPLIT_COPY_TWO_ID),
            (SPLIT_COPY_ONE_LOCATION, SPLIT_COPY_TWO_LOCATION),
            strict=True,
        ):
            resp = self.request("POST", f"/v1/plan/candidates/{fixture.id}/finalize")
            assert resp.status_code == 200, resp.text
            image_id = resp.json()["id"]
            resp = self.request(
                "POST",
                f"/v1/images/{image_id}/copies",
                json_body={"location": location},
            )
            assert resp.status_code == 200, resp.text
        self.state.files_by_collection[docs_key][SPLIT_FILE_RELPATH].hot = False

    def seed_search_fixtures(self) -> None:
        self.seed_docs_archive()
        self.seed_photos_hot()

    def seed_planner_fixtures(self) -> None:
        self.seed_docs_hot()
        self.seed_photos_hot()
        self.seed_image_fixtures(IMAGE_FIXTURES)

    def seed_split_planner_fixtures(self) -> None:
        self.seed_docs_hot()
        self.seed_image_fixtures(SPLIT_IMAGE_FIXTURES)

    def seed_image_fixtures(self, fixtures: tuple[Any, ...]) -> None:
        images_root = self.workspace / "images"
        for fixture in fixtures:
            image_root = write_tree(images_root / fixture.id, fixture.files)
            self.state.seed_image(
                CandidateRecord(
                    candidate_id=ImageId(fixture.id),
                    finalized_id=fixture.volume_id,
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

    def upload_required_entries(self, fetch_id: str) -> None:
        self.fetches.upload_all_required_entries(fetch_id)

    def upload_partial_entry(self, fetch_id: str, entry_id: str) -> int:
        return self.fetches.upload_partial_entry(fetch_id, entry_id)

    def recovery_upload_absent(self, fetch_id: str) -> bool:
        return FetchId(fetch_id) not in self.state.fetches

    def list_read_only_browsing_paths(self) -> set[str]:
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
        if storage == "archive":
            return any(
                status.object_path == key and status.state == GlacierState.UPLOADED
                for status in self.state.glacier_status_by_image.values()
            )
        if storage != "hot":
            raise AssertionError(f"unsupported storage bucket kind: {storage}")
        for records in self.state.files_by_collection.values():
            for file in records.values():
                key_for_file = (
                    f"collections/{normalize_collection_id(str(file.collection_id))}/{file.path}"
                )
                if key_for_file == key and file.hot and not file.hot_backing_missing:
                    return True
        return False

    def bucket_contains_prefix(self, *, storage: str, prefix: str) -> bool:
        if storage == "archive":
            return any(
                (status.object_path or "").startswith(prefix)
                and status.state == GlacierState.UPLOADED
                for status in self.state.glacier_status_by_image.values()
            )
        if storage != "hot":
            raise AssertionError(f"unsupported storage bucket kind: {storage}")
        if prefix == ".arc/uploads/":
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

    def pins_list(self) -> list[str]:
        return [str(item.target) for item in self.pins.list_pins()]

    def uploaded_entry_content(self, fetch_id: str, entry_path: str) -> bytes | None:
        record = self.state.fetches[FetchId(fetch_id)]
        for entry in record.entries.values():
            if entry.path == entry_path:
                return entry.uploaded_content
        raise NotFound(f"entry not found for {fetch_id}: {entry_path}")

    @staticmethod
    def _default_arc_disc_fixture() -> dict[str, Any]:
        return {
            "reader": {
                "payload_by_disc_path": {},
                "fail_disc_paths": [],
            },
            "burn": {
                "confirmed_copy_ids": [],
                "available_copy_ids": [],
                "location_by_copy_id": {},
                "label_text_by_copy_id": {},
                "fail_copy_ids": [],
                "verify_fail_copy_ids": [],
                "blank_media_blocked_copy_ids": [],
            },
        }

    def _load_arc_disc_fixture(self) -> dict[str, Any]:
        if not self.fixture_path.exists():
            return self._default_arc_disc_fixture()
        return cast(dict[str, Any], json.loads(self.fixture_path.read_text(encoding="utf-8")))

    def _write_arc_disc_fixture(self, payload: dict[str, Any]) -> None:
        self.fixture_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def configure_arc_disc_fixture(
        self,
        *,
        fetch_id: str = "fx-1",
        fail_path: str | None = None,
        corrupt_path: str | None = None,
        fail_copy_ids: set[str] | None = None,
        corrupt_copy_ids: set[str] | None = None,
    ) -> None:
        manifest = cast(dict[str, Any], self.fetches.manifest(fetch_id))
        fetch_record = self.state.fetches[FetchId(fetch_id)]
        content_by_path = {entry.path: entry.content for entry in fetch_record.entries.values()}
        payload_by_disc_path: dict[str, str] = {}
        fail_disc_paths: list[str] = []
        fail_copy_ids = fail_copy_ids or set()
        corrupt_copy_ids = corrupt_copy_ids or set()

        for entry in cast(list[dict[str, Any]], manifest["entries"]):
            entry_path = str(entry["path"])
            parts = cast(list[dict[str, Any]], entry["parts"])
            plaintext_parts = split_fixture_plaintext(content_by_path[entry_path], len(parts))
            for part in parts:
                part_index = int(part["index"])
                part_plaintext = plaintext_parts[part_index]
                for copy_info in cast(list[dict[str, Any]], part["copies"]):
                    copy_id = str(copy_info["copy"])
                    disc_path = str(copy_info["disc_path"])
                    payload = fixture_encrypt_bytes(part_plaintext)
                    if entry_path == corrupt_path or copy_id in corrupt_copy_ids:
                        payload = payload + b"corrupted-by-fixture\n"
                    payload_by_disc_path[disc_path] = base64.b64encode(payload).decode("ascii")
                    if entry_path == fail_path or copy_id in fail_copy_ids:
                        fail_disc_paths.append(disc_path)

        payload = self._load_arc_disc_fixture()
        payload["reader"] = {
            "payload_by_disc_path": payload_by_disc_path,
            "fail_disc_paths": fail_disc_paths,
        }
        self._write_arc_disc_fixture(payload)

    def confirm_arc_disc_burn_copy(self, copy_id: str, *, location: str) -> None:
        payload = self._load_arc_disc_fixture()
        burn = cast(dict[str, Any], payload["burn"])
        confirmed = set(cast(list[str], burn.get("confirmed_copy_ids", [])))
        confirmed.add(copy_id)
        burn["confirmed_copy_ids"] = sorted(confirmed)
        location_by_copy_id = dict(cast(dict[str, str], burn.get("location_by_copy_id", {})))
        location_by_copy_id[copy_id] = location
        burn["location_by_copy_id"] = location_by_copy_id
        label_text_by_copy_id = dict(cast(dict[str, str], burn.get("label_text_by_copy_id", {})))
        label_text_by_copy_id[copy_id] = copy_id
        burn["label_text_by_copy_id"] = label_text_by_copy_id
        self._write_arc_disc_fixture(payload)

    def set_arc_disc_burn_copy_available(self, copy_id: str, *, available: bool) -> None:
        payload = self._load_arc_disc_fixture()
        burn = cast(dict[str, Any], payload["burn"])
        available_copy_ids = set(cast(list[str], burn.get("available_copy_ids", [])))
        if available:
            available_copy_ids.add(copy_id)
        else:
            available_copy_ids.discard(copy_id)
        burn["available_copy_ids"] = sorted(available_copy_ids)
        self._write_arc_disc_fixture(payload)

    def fail_arc_disc_burn_copy(self, copy_id: str) -> None:
        payload = self._load_arc_disc_fixture()
        burn = cast(dict[str, Any], payload["burn"])
        failures = set(cast(list[str], burn.get("fail_copy_ids", [])))
        failures.add(copy_id)
        burn["fail_copy_ids"] = sorted(failures)
        self._write_arc_disc_fixture(payload)

    def fail_arc_disc_burn_copy_verification(self, copy_id: str) -> None:
        payload = self._load_arc_disc_fixture()
        burn = cast(dict[str, Any], payload["burn"])
        failures = set(cast(list[str], burn.get("verify_fail_copy_ids", [])))
        failures.add(copy_id)
        burn["verify_fail_copy_ids"] = sorted(failures)
        self._write_arc_disc_fixture(payload)

    def clear_arc_disc_burn_failures(self) -> None:
        payload = self._load_arc_disc_fixture()
        burn = cast(dict[str, Any], payload["burn"])
        burn["fail_copy_ids"] = []
        burn["verify_fail_copy_ids"] = []
        burn["blank_media_blocked_copy_ids"] = []
        self._write_arc_disc_fixture(payload)

    def corrupt_arc_disc_staged_iso(self, image_id: str) -> None:
        image = self.planning.get_image(image_id)
        staging_path = self.workspace / "arc_disc_staging" / image_id / str(image["filename"])
        if not staging_path.is_file():
            raise AssertionError(f"staged ISO not found: {staging_path}")
        staging_path.write_bytes(staging_path.read_bytes() + b"corrupted-by-fixture\n")

    def arc_disc_staged_iso_exists(self, image_id: str) -> bool:
        image = self.planning.get_image(image_id)
        staging_path = self.workspace / "arc_disc_staging" / image_id / str(image["filename"])
        return staging_path.is_file()

    def _subprocess_env(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        env = os.environ.copy()
        pythonpath_parts = [str(ROOT) for ROOT in (SRC_ROOT, REPO_ROOT)]
        existing = env.get("PYTHONPATH")
        if existing:
            pythonpath_parts.append(existing)
        env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
        env["ARC_BASE_URL"] = self.base_url
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
