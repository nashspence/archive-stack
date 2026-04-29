from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree

import httpx
import pytest
from sqlalchemy import select

from arc_core.catalog_models import (
    ActivePinRecord,
    CandidateCoveredPathRecord,
    CollectionFileRecord,
    CollectionRecord,
    CollectionUploadFileRecord,
    FetchEntryRecord,
    FileCopyRecord,
    FinalizedImageCoveredPathRecord,
    FinalizedImageRecord,
    ImageCopyRecord,
    PlannedCandidateRecord,
)
from arc_core.domain.enums import CopyState, FetchState, ProtectionState, VerificationState
from arc_core.domain.models import CollectionSummary, CopySummary, FetchCopyHint, FetchSummary
from arc_core.domain.selectors import parse_target
from arc_core.domain.types import CollectionId, CopyId, FetchId, TargetStr
from arc_core.fs_paths import normalize_collection_id
from arc_core.runtime_config import load_runtime_config
from arc_core.services.glacier_uploads import enqueue_glacier_upload_job
from arc_core.sqlite_db import make_session_factory, session_scope
from arc_core.stores.s3_support import (
    _create_s3_client,
    create_glacier_s3_client,
    create_s3_client,
)
from tests.fixtures.acceptance import REPO_ROOT, SRC_ROOT
from tests.fixtures.data import (
    DOCS_COLLECTION_ID,
    DOCS_FILES,
    IMAGE_FIXTURES,
    MIN_FILL_BYTES,
    PHOTOS_2024_FILES,
    SPLIT_COPY_ONE_ID,
    SPLIT_COPY_ONE_LOCATION,
    SPLIT_COPY_TWO_ID,
    SPLIT_COPY_TWO_LOCATION,
    SPLIT_FILE_RELPATH,
    SPLIT_IMAGE_FIXTURES,
    TARGET_BYTES,
    ImageFixture,
    fixture_encrypt_bytes,
    split_fixture_plaintext,
    write_tree,
)
from tests.fixtures.disc_contracts import InspectedIso, inspect_downloaded_iso
from tests.timing_profile import time_block

_APP_START_TIMEOUT = 15.0
_APP_REQUEST_TIMEOUT = 5.0
_SIDECAR_START_TIMEOUT = 15.0
_EXTERNAL_APP_BASE_URL_ENV = "ARC_TEST_EXTERNAL_APP_BASE_URL"
_EXTERNAL_APP_DB_PATH_ENV = "ARC_TEST_EXTERNAL_APP_DB_PATH"
_EXTERNAL_WEBDAV_BASE_URL_ENV = "ARC_TEST_EXTERNAL_WEBDAV_BASE_URL"
_EXTERNAL_APP_RESTART_PATH_ENV = "ARC_TEST_EXTERNAL_APP_RESTART_PATH"
_EXTERNAL_APP_RESET_PATH_ENV = "ARC_TEST_EXTERNAL_APP_RESET_PATH"
_CANONICAL_TEST_ENTRYPOINT_ENV = "ARC_TEST_CANONICAL_ENTRYPOINT"
_DEFAULT_EXTERNAL_APP_BASE_URL = "http://app:8000"
_DEFAULT_EXTERNAL_APP_DB_PATH = "/app/.compose/state.sqlite3"
_DEFAULT_EXTERNAL_WEBDAV_BASE_URL = "http://webdav:8080"
_DEFAULT_EXTERNAL_APP_RESTART_PATH = "/_test/restart"
_DEFAULT_EXTERNAL_APP_RESET_PATH = "/_test/reset"


def _copy_summary_from_payload(copy: Mapping[str, object]) -> CopySummary:
    return CopySummary(
        id=CopyId(str(copy["id"])),
        volume_id=str(copy["volume_id"]),
        label_text=str(copy["label_text"]),
        location=cast(str | None, copy.get("location")),
        created_at=str(copy["created_at"]),
        state=CopyState(str(copy["state"])),
        verification_state=VerificationState(str(copy["verification_state"])),
    )


def _require_canonical_test_entrypoint() -> None:
    if os.environ.get(_CANONICAL_TEST_ENTRYPOINT_ENV) == "1":
        return
    raise pytest.UsageError(
        "Production acceptance runs must use `./test prod`, `./test prod-profile`, "
        "or the canonical `./test` entrypoint; direct `pytest` is unsupported."
    )


@dataclass(slots=True)
class _ExternalAppHandle:
    base_url: str
    reset_path: str
    restart_path: str
    instance_id: str | None = None

    def wait_until_ready(self, *, previous_instance_id: str | None = None) -> str:
        deadline = time.monotonic() + _APP_START_TIMEOUT
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                with httpx.Client(base_url=self.base_url, timeout=0.5) as client:
                    response = client.get("/healthz")
                if response.status_code != 200:
                    time.sleep(0.05)
                    continue
                payload = response.json()
                instance_id = str(payload["instance_id"])
                if previous_instance_id is not None and instance_id == previous_instance_id:
                    time.sleep(0.05)
                    continue
                self.instance_id = instance_id
                return instance_id
            except Exception as exc:  # pragma: no cover - readiness race
                last_error = exc
            time.sleep(0.05)
        raise RuntimeError(
            f"Timed out waiting for compose-managed app at {self.base_url}"
        ) from last_error

    def restart(self) -> None:
        previous_instance_id = self.instance_id
        try:
            with httpx.Client(base_url=self.base_url, timeout=_APP_REQUEST_TIMEOUT) as client:
                response = client.post(self.restart_path)
            if response.status_code != 202:
                response.raise_for_status()
        except httpx.HTTPError:
            # The process can exit before the response fully flushes. The follow-up
            # readiness check below is the authoritative signal that the service
            # recycled through the compose-managed runtime path.
            pass
        self.wait_until_ready(previous_instance_id=previous_instance_id)

    def reset(self) -> None:
        with httpx.Client(base_url=self.base_url, timeout=_APP_REQUEST_TIMEOUT) as client:
            response = client.post(self.reset_path)
        if response.status_code != 204:
            response.raise_for_status()
        self.wait_until_ready()

    def close(self) -> None:
        return None


def _external_app_server() -> _ExternalAppHandle:
    base_url = os.environ.get(_EXTERNAL_APP_BASE_URL_ENV, _DEFAULT_EXTERNAL_APP_BASE_URL).rstrip(
        "/"
    )
    if not base_url:
        raise RuntimeError(f"{_EXTERNAL_APP_BASE_URL_ENV} must not be empty")
    restart_path = os.environ.get(
        _EXTERNAL_APP_RESTART_PATH_ENV, _DEFAULT_EXTERNAL_APP_RESTART_PATH
    )
    reset_path = os.environ.get(_EXTERNAL_APP_RESET_PATH_ENV, _DEFAULT_EXTERNAL_APP_RESET_PATH)
    handle = _ExternalAppHandle(
        base_url=base_url,
        reset_path=reset_path,
        restart_path=restart_path,
    )
    handle.wait_until_ready()
    return handle


def _external_webdav_base_url() -> str:
    base_url = os.environ.get(
        _EXTERNAL_WEBDAV_BASE_URL_ENV, _DEFAULT_EXTERNAL_WEBDAV_BASE_URL
    ).rstrip("/")
    if not base_url:
        raise RuntimeError(f"{_EXTERNAL_WEBDAV_BASE_URL_ENV} must not be empty")
    return base_url


def _wait_for_tusd(base_url: str) -> None:
    deadline = time.monotonic() + _SIDECAR_START_TIMEOUT
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with httpx.Client(timeout=0.5) as client:
                response = client.options(base_url, headers={"Tus-Resumable": "1.0.0"})
            if response.status_code in {200, 204}:
                return
        except Exception as exc:  # pragma: no cover - readiness race
            last_error = exc
        time.sleep(0.05)
    raise RuntimeError(f"Timed out waiting for tusd at {base_url}") from last_error


def _wait_for_webdav(base_url: str) -> None:
    deadline = time.monotonic() + _SIDECAR_START_TIMEOUT
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with httpx.Client(timeout=0.5) as client:
                response = client.request("PROPFIND", base_url, headers={"Depth": "0"})
            if response.status_code == 207:
                return
        except Exception as exc:  # pragma: no cover - readiness race
            last_error = exc
        time.sleep(0.05)
    raise RuntimeError(f"Timed out waiting for WebDAV at {base_url}") from last_error


def _wait_for_s3_bucket() -> None:
    deadline = time.monotonic() + _SIDECAR_START_TIMEOUT
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            config = load_runtime_config()
            create_s3_client(config).head_bucket(Bucket=config.s3_bucket)
            if (
                config.glacier_bucket != config.s3_bucket
                or config.glacier_endpoint_url != config.s3_endpoint_url
            ):
                create_glacier_s3_client(config).head_bucket(Bucket=config.glacier_bucket)
            return
        except Exception as exc:  # pragma: no cover - readiness race
            last_error = exc
        time.sleep(0.05)
    raise RuntimeError("Timed out waiting for Garage S3 bucket readiness") from last_error


def _normalize_lifecycle_configuration(payload: Mapping[str, object]) -> dict[str, object]:
    rules = []
    raw_rules = payload.get("Rules", [])
    if isinstance(raw_rules, list):
        for rule in raw_rules:
            if not isinstance(rule, dict):
                continue
            abort = rule.get("AbortIncompleteMultipartUpload")
            rules.append(
                {
                    "ID": rule.get("ID"),
                    "Status": rule.get("Status"),
                    "Filter": rule.get("Filter", {}),
                    "AbortIncompleteMultipartUpload": {
                        "DaysAfterInitiation": abort.get("DaysAfterInitiation")
                        if isinstance(abort, dict)
                        else None
                    },
                }
            )
    return {"Rules": rules}


class ProductionCollectionsClient:
    def __init__(self, system: ProductionSystem) -> None:
        self._system = system

    def create_or_resume_upload(
        self,
        collection_id: str,
        files: list[dict[str, object]],
        *,
        ingest_source: str | None = None,
    ) -> dict[str, object]:
        body: dict[str, object] = {"collection_id": collection_id, "files": files}
        if ingest_source is not None:
            body["ingest_source"] = ingest_source
        return self._system.request("POST", "/v1/collection-uploads", json_body=body).json()

    def get_upload(self, collection_id: str) -> dict[str, object]:
        return self._system.request("GET", f"/v1/collection-uploads/{collection_id}").json()

    def create_or_resume_file_upload(self, collection_id: str, path: str) -> dict[str, object]:
        return self._system.request(
            "POST",
            f"/v1/collection-uploads/{collection_id}/files/{path}/upload",
        ).json()

    def append_upload_chunk(
        self,
        upload_url: str,
        *,
        offset: int,
        content: bytes,
    ) -> dict[str, object]:
        checksum = base64.b64encode(hashlib.sha256(content).digest()).decode("ascii")
        response = self._system.request(
            "PATCH",
            upload_url,
            headers={
                "Content-Type": "application/offset+octet-stream",
                "Tus-Resumable": "1.0.0",
                "Upload-Offset": str(offset),
                "Upload-Checksum": f"sha256 {checksum}",
            },
            content=content,
        )
        return {
            "offset": int(response.headers["Upload-Offset"]),
            "expires_at": response.headers.get("Upload-Expires"),
        }

    def get(self, collection_id: str) -> CollectionSummary:
        response = self._system.request("GET", f"/v1/collections/{collection_id}")
        payload = response.json()
        return CollectionSummary(
            id=CollectionId(payload["id"]),
            files=payload["files"],
            bytes=payload["bytes"],
            hot_bytes=payload["hot_bytes"],
            archived_bytes=payload["archived_bytes"],
            protection_state=ProtectionState(payload["protection_state"]),
            protected_bytes=payload["protected_bytes"],
        )


class ProductionFetchesClient:
    def __init__(self, system: ProductionSystem) -> None:
        self._system = system

    def get(self, fetch_id: str) -> FetchSummary:
        payload = self._system.request("GET", f"/v1/fetches/{fetch_id}").json()
        return FetchSummary(
            id=FetchId(payload["id"]),
            target=TargetStr(payload["target"]),
            state=FetchState(payload["state"]),
            files=payload["files"],
            bytes=payload["bytes"],
            copies=[
                FetchCopyHint(
                    id=CopyId(copy["id"]),
                    volume_id=copy["volume_id"],
                    location=copy["location"],
                )
                for copy in payload["copies"]
            ],
            entries_total=payload["entries_total"],
            entries_pending=payload["entries_pending"],
            entries_partial=payload["entries_partial"],
            entries_byte_complete=payload["entries_byte_complete"],
            entries_uploaded=payload["entries_uploaded"],
            uploaded_bytes=payload["uploaded_bytes"],
            missing_bytes=payload["missing_bytes"],
            upload_state_expires_at=payload["upload_state_expires_at"],
        )

    def manifest(self, fetch_id: str) -> dict[str, object]:
        return self._system.request("GET", f"/v1/fetches/{fetch_id}/manifest").json()

    def create_or_resume_upload(self, fetch_id: str, entry_id: str) -> dict[str, object]:
        return self._system.request(
            "POST",
            f"/v1/fetches/{fetch_id}/entries/{entry_id}/upload",
        ).json()

    def append_upload_chunk(
        self,
        upload_url: str,
        *,
        offset: int,
        content: bytes,
    ) -> dict[str, object]:
        checksum = base64.b64encode(hashlib.sha256(content).digest()).decode("ascii")
        response = self._system.request(
            "PATCH",
            upload_url,
            headers={
                "Content-Type": "application/offset+octet-stream",
                "Tus-Resumable": "1.0.0",
                "Upload-Offset": str(offset),
                "Upload-Checksum": f"sha256 {checksum}",
            },
            content=content,
        )
        return {
            "offset": int(response.headers["Upload-Offset"]),
            "expires_at": response.headers.get("Upload-Expires"),
        }

    def complete(self, fetch_id: str) -> dict[str, object]:
        return self._system.request("POST", f"/v1/fetches/{fetch_id}/complete").json()


@dataclass(frozen=True, slots=True)
class ProductionStoredFile:
    collection_id: str
    path: str
    hot: bool
    archived: bool


@dataclass(frozen=True, slots=True)
class ProductionFetchCopyView:
    part_count: int | None


@dataclass(frozen=True, slots=True)
class ProductionFetchEntryView:
    sha256: str
    copies: tuple[ProductionFetchCopyView, ...]


@dataclass(frozen=True, slots=True)
class ProductionFetchRecordView:
    entries: dict[str, ProductionFetchEntryView]


@dataclass(frozen=True, slots=True)
class ProductionCandidateView:
    candidate_id: str
    finalized_id: str
    iso_ready: bool
    covered_paths: tuple[tuple[str, str], ...]


class ProductionCandidatesProxy:
    def __init__(self, system: ProductionSystem) -> None:
        self._system = system

    def __iter__(self) -> Iterator[str]:
        with session_scope(make_session_factory(str(self._system.db_path))) as session:
            records = session.scalars(select(PlannedCandidateRecord.candidate_id)).all()
        return iter(records)

    def __contains__(self, key: object) -> bool:
        candidate_id = str(key)
        with session_scope(make_session_factory(str(self._system.db_path))) as session:
            return session.get(PlannedCandidateRecord, candidate_id) is not None

    def __getitem__(self, key: object) -> ProductionCandidateView:
        candidate_id = str(key)
        with session_scope(make_session_factory(str(self._system.db_path))) as session:
            record = session.get(PlannedCandidateRecord, candidate_id)
            if record is None:
                raise KeyError(candidate_id)
            covered = tuple((cp.collection_id, cp.path) for cp in record.covered_paths)
            return ProductionCandidateView(
                candidate_id=record.candidate_id,
                finalized_id=record.finalized_id,
                iso_ready=record.iso_ready,
                covered_paths=covered,
            )


class ProductionPlanningClient:
    def __init__(self, system: ProductionSystem) -> None:
        self._system = system

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
        params: dict[str, object] = {
            "page": page,
            "per_page": per_page,
            "sort": sort,
            "order": order,
        }
        if q is not None:
            params["q"] = q
        if collection is not None:
            params["collection"] = collection
        if iso_ready is not None:
            params["iso_ready"] = str(iso_ready).lower()
        response = self._system.request("GET", "/v1/plan", params=params)
        return cast(dict[str, object], response.json())

    def finalize_image(self, candidate_id: str) -> dict[str, object]:
        return cast(
            dict[str, object],
            self._system.request("POST", f"/v1/plan/candidates/{candidate_id}/finalize").json(),
        )


class ProductionCopiesClient:
    def __init__(self, system: ProductionSystem) -> None:
        self._system = system

    def register(
        self,
        image_id: str,
        location: str,
        *,
        copy_id: str | None = None,
    ) -> CopySummary:
        body: dict[str, object] = {"location": location}
        if copy_id is not None:
            body["copy_id"] = copy_id
        payload = self._system.request(
            "POST",
            f"/v1/images/{image_id}/copies",
            json_body=body,
        ).json()
        return _copy_summary_from_payload(payload["copy"])

    def list_for_image(self, image_id: str) -> list[CopySummary]:
        payload = self._system.request("GET", f"/v1/images/{image_id}/copies").json()
        return [_copy_summary_from_payload(copy) for copy in payload["copies"]]

    def update(
        self,
        image_id: str,
        copy_id: str,
        *,
        location: str | None = None,
        state: str | None = None,
        verification_state: str | None = None,
    ) -> CopySummary:
        body: dict[str, object] = {}
        if location is not None:
            body["location"] = location
        if state is not None:
            body["state"] = state
        if verification_state is not None:
            body["verification_state"] = verification_state
        payload = self._system.request(
            "PATCH",
            f"/v1/images/{image_id}/copies/{copy_id}",
            json_body=body,
        ).json()
        return _copy_summary_from_payload(payload["copy"])


class ProductionStateFetchesProxy:
    def __init__(self, system: ProductionSystem) -> None:
        self._system = system

    def __iter__(self) -> Iterator[str]:
        with session_scope(make_session_factory(str(self._system.db_path))) as session:
            records = session.scalars(
                select(ActivePinRecord.fetch_id).order_by(ActivePinRecord.fetch_order)
            ).all()
        return iter(records)

    def __getitem__(self, fetch_id: str) -> ProductionFetchRecordView:
        with session_scope(make_session_factory(str(self._system.db_path))) as session:
            entry_records = session.scalars(
                select(FetchEntryRecord)
                .where(FetchEntryRecord.fetch_id == fetch_id)
                .order_by(FetchEntryRecord.entry_order)
            ).all()
            if not entry_records:
                raise KeyError(fetch_id)

            entries: dict[str, ProductionFetchEntryView] = {}
            for entry in entry_records:
                copy_records = session.scalars(
                    select(FileCopyRecord)
                    .where(
                        FileCopyRecord.collection_id == entry.collection_id,
                        FileCopyRecord.path == entry.path,
                    )
                    .order_by(
                        FileCopyRecord.part_index,
                        FileCopyRecord.volume_id,
                        FileCopyRecord.copy_id,
                    )
                ).all()
                entries[entry.entry_id] = ProductionFetchEntryView(
                    sha256=entry.sha256,
                    copies=tuple(
                        ProductionFetchCopyView(part_count=copy.part_count) for copy in copy_records
                    ),
                )
        return ProductionFetchRecordView(entries=entries)


class ProductionExactPinsProxy:
    def __init__(self, system: ProductionSystem) -> None:
        self._system = system

    def discard(self, canonical: str) -> None:
        with session_scope(make_session_factory(str(self._system.db_path))) as session:
            record = session.get(ActivePinRecord, canonical)
            if record is not None:
                session.delete(record)


class ProductionStateClient:
    def __init__(self, system: ProductionSystem) -> None:
        self._system = system
        self.exact_pins = ProductionExactPinsProxy(system)
        self.fetches = ProductionStateFetchesProxy(system)
        self.candidates_by_id = ProductionCandidatesProxy(system)

    def collection_files(self, collection_id: str) -> list[ProductionStoredFile]:
        with session_scope(make_session_factory(str(self._system.db_path))) as session:
            records = session.scalars(
                select(CollectionFileRecord).where(
                    CollectionFileRecord.collection_id == collection_id
                )
            ).all()
        return [
            ProductionStoredFile(
                collection_id=record.collection_id,
                path=record.path,
                hot=record.hot,
                archived=record.archived,
            )
            for record in sorted(records, key=lambda item: item.path)
        ]

    def selected_files(
        self,
        raw_target: str,
        *,
        missing_ok: bool = False,
    ) -> list[ProductionStoredFile]:
        target = parse_target(raw_target)
        with session_scope(make_session_factory(str(self._system.db_path))) as session:
            records = session.scalars(select(CollectionFileRecord)).all()
        selected = [
            ProductionStoredFile(
                collection_id=record.collection_id,
                path=record.path,
                hot=record.hot,
                archived=record.archived,
            )
            for record in records
            if (
                f"{record.collection_id}/{record.path}".startswith(target.canonical)
                if target.is_dir
                else f"{record.collection_id}/{record.path}" == target.canonical
            )
        ]
        if not selected and not missing_ok:
            raise AssertionError(f"target not found: {raw_target}")
        return selected

    def file_content(self, collection_id: str, path: str) -> bytes:
        return self._system._file_bytes(str(collection_id), path)

    def is_hot(self, raw_target: str) -> bool:
        selected = self.selected_files(raw_target, missing_ok=True)
        return bool(selected) and all(record.hot for record in selected)


@dataclass(slots=True)
class ProductionSystem:
    workspace: Path
    webdav_url: str
    server: _ExternalAppHandle
    base_url: str
    db_path: Path
    fixture_path: Path
    collections: ProductionCollectionsClient
    fetches: ProductionFetchesClient
    state: ProductionStateClient
    planning: ProductionPlanningClient
    copies: ProductionCopiesClient

    @classmethod
    def create(cls, workspace: Path) -> ProductionSystem:
        with time_block("fixture.acceptance_system.create"):
            db_path = (
                Path(os.environ.get(_EXTERNAL_APP_DB_PATH_ENV, _DEFAULT_EXTERNAL_APP_DB_PATH))
                .expanduser()
                .resolve()
            )
            fixture_path = workspace / "arc_disc_fixture.json"
            _wait_for_s3_bucket()
            _wait_for_tusd(load_runtime_config().tusd_base_url)
            server = _external_app_server()
            webdav_url = _external_webdav_base_url()
            _wait_for_webdav(webdav_url)
            system = cls(
                workspace=workspace,
                webdav_url=webdav_url,
                server=server,
                base_url=server.base_url,
                db_path=db_path,
                fixture_path=fixture_path,
                collections=cast(ProductionCollectionsClient, None),
                fetches=cast(ProductionFetchesClient, None),
                state=cast(ProductionStateClient, None),
                planning=cast(ProductionPlanningClient, None),
                copies=cast(ProductionCopiesClient, None),
            )
            system.collections = ProductionCollectionsClient(system)
            system.fetches = ProductionFetchesClient(system)
            system.state = ProductionStateClient(system)
            system.planning = ProductionPlanningClient(system)
            system.copies = ProductionCopiesClient(system)
            system.reset()
            return system

    def close(self) -> None:
        with time_block("fixture.acceptance_system.close"):
            self.server.close()

    def restart(self) -> None:
        with time_block("fixture.acceptance_system.restart"):
            self.server.restart()
            self.base_url = self.server.base_url

    def reset(self) -> None:
        with time_block("fixture.acceptance_system.reset"):
            self._clear_fixture_path()
            self.server.reset()

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
            with httpx.Client(base_url=self.base_url, timeout=5.0) as client:
                return client.request(
                    method,
                    path,
                    params=params,
                    json=json_body,
                    headers=headers,
                    content=content,
                )

    def _s3_client(self):
        return create_s3_client(load_runtime_config())

    def _glacier_s3_client(self):
        return create_glacier_s3_client(load_runtime_config())

    @staticmethod
    def _collection_key(collection_id: str, path: str) -> str:
        return f"collections/{normalize_collection_id(collection_id)}/{path}"

    @staticmethod
    def _bucket_and_client(storage: str):
        config = load_runtime_config()
        if storage == "hot":
            return config.s3_bucket, create_s3_client(config)
        if storage == "archive":
            return config.glacier_bucket, create_glacier_s3_client(config)
        raise AssertionError(f"unsupported storage bucket kind: {storage}")

    @staticmethod
    def _client_for_credentials(credentials: str):
        config = load_runtime_config()
        if credentials == "hot":
            return _create_s3_client(
                endpoint_url=config.s3_endpoint_url,
                region=config.s3_region,
                access_key_id=config.s3_access_key_id,
                secret_access_key=config.s3_secret_access_key,
                force_path_style=config.s3_force_path_style,
            )
        if credentials == "archive":
            return _create_s3_client(
                endpoint_url=config.glacier_endpoint_url,
                region=config.glacier_region,
                access_key_id=config.glacier_access_key_id,
                secret_access_key=config.glacier_secret_access_key,
                force_path_style=config.glacier_force_path_style,
            )
        raise AssertionError(f"unsupported credential kind: {credentials}")

    def _webdav_request(
        self,
        method: str,
        path: str = "/",
        *,
        headers: Mapping[str, str] | None = None,
        content: bytes | None = None,
    ) -> httpx.Response:
        with time_block(f"webdav {method} {path}"):
            with httpx.Client(base_url=self.webdav_url, timeout=5.0) as client:
                return client.request(method, path, headers=headers, content=content)

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
        with time_block("subprocess arc-disc"):
            if not self.fixture_path.exists():
                self._write_arc_disc_fixture(self._default_arc_disc_fixture())
            return subprocess.run(
                [sys.executable, "-m", "arc_disc.main", *args],
                cwd=REPO_ROOT,
                env=self._subprocess_env(
                    {
                        "ARC_DISC_FIXTURE_PATH": str(self.fixture_path),
                        "ARC_DISC_READER_FACTORY": (
                            "tests.fixtures.arc_disc_fakes:FixtureOpticalReader"
                        ),
                        "ARC_DISC_ISO_VERIFIER_FACTORY": (
                            "tests.fixtures.arc_disc_fakes:FixtureIsoVerifier"
                        ),
                        "ARC_DISC_BURNER_FACTORY": (
                            "tests.fixtures.arc_disc_fakes:FixtureDiscBurner"
                        ),
                        "ARC_DISC_BURNED_MEDIA_VERIFIER_FACTORY": (
                            "tests.fixtures.arc_disc_fakes:FixtureBurnedMediaVerifier"
                        ),
                        "ARC_DISC_BURN_PROMPTS_FACTORY": (
                            "tests.fixtures.arc_disc_fakes:FixtureBurnPrompts"
                        ),
                        "ARC_DISC_STAGING_DIR": str(self.workspace / "arc_disc_staging"),
                    }
                ),
                input=input_text,
                capture_output=True,
                text=True,
                check=False,
            )

    def delete_hot_backing_file(self, target: str) -> None:
        selected = self.state.selected_files(target)
        if len(selected) != 1:
            raise AssertionError(f"expected exactly one file target: {target}")
        record = selected[0]
        self._s3_client().delete_object(
            Bucket=load_runtime_config().s3_bucket,
            Key=self._collection_key(record.collection_id, record.path),
        )

    def has_committed_collection_file(self, collection_id: str, path: str) -> bool:
        client = self._s3_client()
        try:
            client.head_object(
                Bucket=load_runtime_config().s3_bucket,
                Key=self._collection_key(collection_id, path),
            )
            return True
        except client.exceptions.ClientError as exc:  # type: ignore[attr-defined]
            if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404:
                return False
            raise

    def seed_collection_source(
        self, collection_id: str, files: Mapping[str, bytes] | None = None
    ) -> None:
        with time_block("fixture.seed_collection_source"):
            normalized_collection_id = normalize_collection_id(collection_id)
            write_tree(
                self.workspace / "collections-src" / normalized_collection_id,
                files or PHOTOS_2024_FILES,
            )

    def upload_collection_source(
        self, collection_id: str, files: Mapping[str, bytes] | None = None
    ) -> dict[str, object]:
        with time_block("fixture.upload_collection_source"):
            normalized_collection_id = normalize_collection_id(collection_id)
            source_files = files or PHOTOS_2024_FILES
            self.seed_collection_source(normalized_collection_id, source_files)
            root = (self.workspace / "collections-src" / normalized_collection_id).resolve()
            manifest = [
                {
                    "path": path,
                    "bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
                for path, content in sorted(source_files.items())
            ]
            payload = self.collections.create_or_resume_upload(
                normalized_collection_id,
                manifest,
                ingest_source=str(root),
            )
            for file_payload in payload["files"]:
                upload = self.collections.create_or_resume_file_upload(
                    normalized_collection_id,
                    str(file_payload["path"]),
                )
                content = source_files[str(file_payload["path"])]
                self.collections.append_upload_chunk(
                    str(upload["upload_url"]),
                    offset=int(upload["offset"]),
                    content=content,
                )
            return self.request("GET", f"/v1/collections/{normalized_collection_id}").json()

    def _seed_collection_hot(
        self, collection_id: str, files: Mapping[str, bytes], *, ingest_source: str | None = None
    ) -> dict[str, object]:
        normalized_collection_id = normalize_collection_id(collection_id)
        self.seed_collection_source(normalized_collection_id, files)
        source_root = (self.workspace / "collections-src" / normalized_collection_id).resolve()
        with session_scope(make_session_factory(str(self.db_path))) as session:
            if session.get(CollectionRecord, normalized_collection_id) is None:
                session.add(
                    CollectionRecord(
                        id=normalized_collection_id,
                        ingest_source=ingest_source or str(source_root),
                    )
                )
                for path, content in sorted(files.items()):
                    self._s3_client().put_object(
                        Bucket=load_runtime_config().s3_bucket,
                        Key=self._collection_key(normalized_collection_id, path),
                        Body=content,
                    )
                    session.add(
                        CollectionFileRecord(
                            collection_id=normalized_collection_id,
                            path=path,
                            bytes=len(content),
                            sha256=hashlib.sha256(content).hexdigest(),
                            hot=True,
                            archived=False,
                        )
                    )
        return self.request("GET", f"/v1/collections/{normalized_collection_id}").json()

    def _seed_image_copy(self, image_id: str, copy_id: str, location: str) -> None:
        with session_scope(make_session_factory(str(self.db_path))) as session:
            if session.get(ImageCopyRecord, {"image_id": image_id, "copy_id": copy_id}) is not None:
                return
        self.copies.register(image_id, location, copy_id=copy_id)

    def seed_photos_hot(self) -> None:
        with time_block("fixture.seed_photos_hot"):
            if not self._collection_exists("photos-2024"):
                self._seed_collection_hot("photos-2024", PHOTOS_2024_FILES)

    def seed_planner_fixtures(self) -> None:
        with time_block("fixture.seed_planner_fixtures"):
            self.seed_docs_hot()
            self.seed_photos_hot()
            self.seed_image_fixtures(IMAGE_FIXTURES)

    def seed_split_planner_fixtures(self) -> None:
        with time_block("fixture.seed_split_planner_fixtures"):
            self.seed_docs_hot()
            self.seed_image_fixtures(SPLIT_IMAGE_FIXTURES)

    def seed_image_fixtures(self, fixtures: tuple[ImageFixture, ...]) -> None:
        with time_block("fixture.seed_image_fixtures"):
            images_root = self.workspace / "images"
            with session_scope(make_session_factory(str(self.db_path))) as session:
                for fixture in fixtures:
                    image_root = write_tree(images_root / fixture.id, fixture.files)
                    existing = session.get(PlannedCandidateRecord, fixture.id)
                    if existing is not None:
                        continue
                    candidate = PlannedCandidateRecord(
                        candidate_id=fixture.id,
                        finalized_id=fixture.volume_id,
                        filename=fixture.filename,
                        bytes=fixture.bytes,
                        iso_ready=fixture.iso_ready,
                        image_root=str(image_root),
                        target_bytes=TARGET_BYTES,
                        min_fill_bytes=MIN_FILL_BYTES,
                    )
                    session.add(candidate)
                    for collection_id, path in fixture.covered_paths:
                        session.add(
                            CandidateCoveredPathRecord(
                                candidate_id=fixture.id,
                                collection_id=collection_id,
                                path=path,
                            )
                        )

    def seed_finalized_image(self, candidate_id: str, *, force_ready: bool = False) -> None:
        with time_block("fixture.seed_finalized_image"):
            with session_scope(make_session_factory(str(self.db_path))) as session:
                candidate = session.get(PlannedCandidateRecord, candidate_id)
                assert candidate is not None, f"candidate not found: {candidate_id}"
                existing = session.get(FinalizedImageRecord, candidate.finalized_id)
                if existing is not None:
                    return
                session.add(
                    FinalizedImageRecord(
                        image_id=candidate.finalized_id,
                        candidate_id=candidate.candidate_id,
                        filename=candidate.filename,
                        bytes=candidate.bytes,
                        image_root=candidate.image_root,
                        target_bytes=candidate.target_bytes,
                    )
                )
                for cp in candidate.covered_paths:
                    session.add(
                        FinalizedImageCoveredPathRecord(
                            image_id=candidate.finalized_id,
                            collection_id=cp.collection_id,
                            path=cp.path,
                        )
                    )
                enqueue_glacier_upload_job(
                    session,
                    image_id=candidate.finalized_id,
                    next_attempt_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                )

    def wait_for_image_glacier_state(
        self,
        image_id: str,
        state: str,
        *,
        timeout: float = 10.0,
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
        timeout: float = 20.0,
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
        response = self.request("GET", "/_test/webhooks")
        assert response.status_code == 200, response.text
        payload = response.json()
        deliveries = payload.get("deliveries", [])
        assert isinstance(deliveries, list), payload
        return [cast(dict[str, object], item) for item in deliveries if isinstance(item, dict)]

    def list_webhook_attempts(self) -> list[dict[str, object]]:
        response = self.request("GET", "/_test/webhooks")
        assert response.status_code == 200, response.text
        payload = response.json()
        attempts = payload.get("attempts", [])
        assert isinstance(attempts, list), payload
        return [cast(dict[str, object], item) for item in attempts if isinstance(item, dict)]

    def configure_webhook_failure(
        self,
        event: str,
        *,
        status_code: int = 503,
        remaining: int = 1,
        delay_seconds: float = 0.0,
        mode: str = "status",
    ) -> None:
        response = self.request(
            "POST",
            "/_test/webhooks/behaviors",
            json_body={
                "event": event,
                "status_code": status_code,
                "remaining": remaining,
                "delay_seconds": delay_seconds,
                "mode": mode,
            },
        )
        assert response.status_code == 201, response.text

    def wait_for_webhook_event(
        self,
        event: str,
        *,
        delivery: int = 1,
        timeout: float = 20.0,
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
        timeout: float = 20.0,
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

    def seed_nested_photos_hot(self) -> None:
        with time_block("fixture.seed_nested_photos_hot"):
            if not self._collection_exists("photos/2024"):
                self._seed_collection_hot("photos/2024", PHOTOS_2024_FILES)

    def seed_parent_photos_hot(self) -> None:
        with time_block("fixture.seed_parent_photos_hot"):
            if not self._collection_exists("photos"):
                self._seed_collection_hot("photos", PHOTOS_2024_FILES)

    def seed_docs_hot(self) -> None:
        with time_block("fixture.seed_docs_hot"):
            if not self._collection_exists("docs"):
                self._seed_collection_hot("docs", DOCS_FILES)

    def seed_docs_archive(self) -> None:
        with time_block("fixture.seed_docs_archive"):
            files = self.state.selected_files(
                f"{DOCS_COLLECTION_ID}/tax/2022/invoice-123.pdf", missing_ok=True
            )
            if files and files[0].archived:
                return
            self.seed_docs_hot()
            self.seed_image_fixtures((IMAGE_FIXTURES[0],))
            self.seed_finalized_image(IMAGE_FIXTURES[0].id)
            self._seed_image_copy(
                IMAGE_FIXTURES[0].volume_id,
                "20260420T040001Z-1",
                "vault-a/shelf-01",
            )
            with session_scope(make_session_factory(str(self.db_path))) as session:
                record = session.get(
                    CollectionFileRecord,
                    {"collection_id": DOCS_COLLECTION_ID, "path": "tax/2022/invoice-123.pdf"},
                )
                assert record is not None
                record.hot = False

    def seed_docs_archive_with_split_invoice(self) -> None:
        with time_block("fixture.seed_docs_archive_with_split_invoice"):
            files = self.state.selected_files(
                f"{DOCS_COLLECTION_ID}/{SPLIT_FILE_RELPATH}", missing_ok=True
            )
            if files and files[0].archived:
                return
            self.seed_docs_hot()
            self.seed_image_fixtures(SPLIT_IMAGE_FIXTURES)
            for fixture, copy_id, location in zip(
                SPLIT_IMAGE_FIXTURES,
                (SPLIT_COPY_ONE_ID, SPLIT_COPY_TWO_ID),
                (SPLIT_COPY_ONE_LOCATION, SPLIT_COPY_TWO_LOCATION),
                strict=True,
            ):
                self.seed_finalized_image(fixture.id)
                self._seed_image_copy(fixture.volume_id, copy_id, location)
            with session_scope(make_session_factory(str(self.db_path))) as session:
                record = session.get(
                    CollectionFileRecord,
                    {"collection_id": DOCS_COLLECTION_ID, "path": SPLIT_FILE_RELPATH},
                )
                assert record is not None
                record.hot = False

    def seed_search_fixtures(self) -> None:
        with time_block("fixture.seed_search_fixtures"):
            self.seed_docs_archive()
            self.seed_photos_hot()

    def seed_pin(self, target: str) -> None:
        response = self.request("POST", "/v1/pin", json_body={"target": target})
        assert response.status_code == 200, response.text

    def recovery_upload_absent(self, fetch_id: str) -> bool:
        response = self._s3_client().list_objects_v2(
            Bucket=load_runtime_config().s3_bucket,
            Prefix=f".arc/uploads/recovery/{fetch_id}/",
        )
        return response.get("KeyCount", 0) == 0

    def list_read_only_browsing_paths(self) -> set[str]:
        response = self._webdav_request("PROPFIND", "/", headers={"Depth": "infinity"})
        assert response.status_code == 207, response.text
        root_path = urlsplit(self.webdav_url).path.rstrip("/")
        paths: set[str] = set()
        for href in ElementTree.fromstring(response.text).iterfind(".//{DAV:}href"):
            raw_href = href.text or ""
            path = unquote(urlsplit(raw_href).path)
            if root_path and path.startswith(root_path):
                relative = path[len(root_path) :].lstrip("/")
            else:
                relative = path.lstrip("/")
            if relative:
                paths.add(relative)
        return paths

    def write_through_read_only_browsing_surface(self, path: str) -> httpx.Response:
        return self._webdav_request("PUT", f"/{path.lstrip('/')}", content=b"forbidden")

    def storage_lifecycle_configuration(self, *, storage: str = "hot") -> dict[str, object]:
        bucket, client = self._bucket_and_client(storage)
        payload = client.get_bucket_lifecycle_configuration(Bucket=bucket)
        return _normalize_lifecycle_configuration(payload)

    def bucket_contains_object(self, *, storage: str, key: str) -> bool:
        bucket, client = self._bucket_and_client(storage)
        try:
            client.head_object(Bucket=bucket, Key=key)
            return True
        except client.exceptions.ClientError as exc:  # type: ignore[attr-defined]
            if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404:
                return False
            raise

    def bucket_contains_prefix(self, *, storage: str, prefix: str) -> bool:
        bucket, client = self._bucket_and_client(storage)
        response = client.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1)
        return response.get("KeyCount", 0) > 0

    def bucket_write_is_rejected(
        self,
        *,
        credentials: str,
        storage: str,
        key: str,
    ) -> bool:
        bucket, _client = self._bucket_and_client(storage)
        client = self._client_for_credentials(credentials)
        try:
            client.put_object(Bucket=bucket, Key=key, Body=b"forbidden")
        except client.exceptions.ClientError as exc:  # type: ignore[attr-defined]
            status = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))
            code = str(exc.response.get("Error", {}).get("Code", "")).strip()
            return status in {401, 403} or code in {"AccessDenied", "Forbidden"}
        return False

    def bucket_read_is_rejected(
        self,
        *,
        credentials: str,
        storage: str,
        key: str,
    ) -> bool:
        bucket, _client = self._bucket_and_client(storage)
        client = self._client_for_credentials(credentials)
        try:
            client.head_object(Bucket=bucket, Key=key)
        except client.exceptions.ClientError as exc:  # type: ignore[attr-defined]
            status = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))
            code = str(exc.response.get("Error", {}).get("Code", "")).strip()
            return status in {401, 403} or code in {"AccessDenied", "Forbidden"}
        return False

    def bucket_list_is_rejected(
        self,
        *,
        credentials: str,
        storage: str,
        prefix: str,
    ) -> bool:
        bucket, _client = self._bucket_and_client(storage)
        client = self._client_for_credentials(credentials)
        try:
            client.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1)
        except client.exceptions.ClientError as exc:  # type: ignore[attr-defined]
            status = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))
            code = str(exc.response.get("Error", {}).get("Code", "")).strip()
            return status in {401, 403} or code in {"AccessDenied", "Forbidden"}
        return False

    def upload_required_entries(self, fetch_id: str) -> None:
        with time_block("fixture.upload_required_entries"):
            manifest = self.fetches.manifest(fetch_id)
            with session_scope(make_session_factory(str(self.db_path))) as db_session:
                entry_records = db_session.scalars(
                    select(FetchEntryRecord).where(FetchEntryRecord.fetch_id == fetch_id)
                ).all()
                collection_id_by_path = {r.path: r.collection_id for r in entry_records}
            for entry in manifest["entries"]:
                upload = self.fetches.create_or_resume_upload(fetch_id, entry["id"])
                entry_collection_id = collection_id_by_path[str(entry["path"])]
                for part in entry["parts"]:
                    payload = fixture_encrypt_bytes(
                        self._file_part_bytes(
                            entry_collection_id,
                            str(entry["path"]),
                            int(part["index"]),
                            len(entry["parts"]),
                        )
                    )
                    result = self.fetches.append_upload_chunk(
                        upload["upload_url"],
                        offset=int(upload["offset"]),
                        content=payload,
                    )
                    upload["offset"] = result["offset"]

    def upload_partial_entry(self, fetch_id: str, entry_id: str) -> int:
        content = self._fetch_entry_file_bytes(fetch_id, entry_id)
        full_payload = fixture_encrypt_bytes(content)
        partial_payload = full_payload[: max(1, len(full_payload) // 2)]
        session_info = self.fetches.create_or_resume_upload(fetch_id, entry_id)
        result = self.fetches.append_upload_chunk(
            str(session_info["upload_url"]),
            offset=int(session_info["offset"]),
            content=partial_payload,
        )
        return int(result["offset"])

    def _fetch_entry_file_bytes(self, fetch_id: str, entry_id: str) -> bytes:
        with session_scope(make_session_factory(str(self.db_path))) as session:
            entry_records = session.scalars(
                select(FetchEntryRecord).where(
                    FetchEntryRecord.fetch_id == fetch_id,
                    FetchEntryRecord.entry_id == entry_id,
                )
            ).all()
            assert len(entry_records) == 1, f"entry not found: {entry_id} in {fetch_id}"
            collection_id = entry_records[0].collection_id
            path = entry_records[0].path
        return self._file_bytes(collection_id, path)

    def configure_arc_disc_fixture(
        self,
        *,
        fetch_id: str = "fx-1",
        fail_path: str | None = None,
        corrupt_path: str | None = None,
        fail_copy_ids: set[str] | None = None,
        corrupt_copy_ids: set[str] | None = None,
    ) -> None:
        payload = self._load_arc_disc_fixture()
        manifest = self.fetches.manifest(fetch_id)
        with session_scope(make_session_factory(str(self.db_path))) as session:
            entry_records = session.scalars(
                select(FetchEntryRecord).where(FetchEntryRecord.fetch_id == fetch_id)
            ).all()
            collection_id_by_path = {r.path: r.collection_id for r in entry_records}
        payload_by_disc_path: dict[str, str] = {}
        fail_disc_paths: list[str] = []
        fail_copy_ids = fail_copy_ids or set()
        corrupt_copy_ids = corrupt_copy_ids or set()

        with time_block("fixture.configure_arc_disc_fixture"):
            for entry in manifest["entries"]:
                entry_path = str(entry["path"])
                parts = entry["parts"]
                plaintext_parts = split_fixture_plaintext(
                    self._file_bytes(collection_id_by_path[entry_path], entry_path),
                    len(parts),
                )
                for part in parts:
                    part_index = int(part["index"])
                    recovery_payload = fixture_encrypt_bytes(plaintext_parts[part_index])
                    for copy in part["copies"]:
                        copy_id = str(copy["copy"])
                        disc_path = str(copy["disc_path"])
                        encoded = recovery_payload
                        if entry_path == corrupt_path or copy_id in corrupt_copy_ids:
                            encoded = recovery_payload + b"corrupted-by-fixture\n"
                        payload_by_disc_path[disc_path] = base64.b64encode(encoded).decode("ascii")
                        if entry_path == fail_path or copy_id in fail_copy_ids:
                            fail_disc_paths.append(disc_path)

            payload["reader"] = {
                "payload_by_disc_path": payload_by_disc_path,
                "fail_disc_paths": fail_disc_paths,
            }
            self._write_arc_disc_fixture(payload)

    @staticmethod
    def _default_arc_disc_fixture() -> dict[str, object]:
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

    def _load_arc_disc_fixture(self) -> dict[str, object]:
        if not self.fixture_path.exists():
            return self._default_arc_disc_fixture()
        return cast(dict[str, object], json.loads(self.fixture_path.read_text(encoding="utf-8")))

    def _write_arc_disc_fixture(self, payload: dict[str, object]) -> None:
        self.fixture_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def confirm_arc_disc_burn_copy(self, copy_id: str, *, location: str) -> None:
        payload = self._load_arc_disc_fixture()
        burn = cast(dict[str, object], payload["burn"])
        confirmed = set(cast(list[str], burn.get("confirmed_copy_ids", [])))
        confirmed.add(copy_id)
        burn["confirmed_copy_ids"] = sorted(confirmed)
        locations = dict(cast(dict[str, str], burn.get("location_by_copy_id", {})))
        locations[copy_id] = location
        burn["location_by_copy_id"] = locations
        labels = dict(cast(dict[str, str], burn.get("label_text_by_copy_id", {})))
        labels[copy_id] = copy_id
        burn["label_text_by_copy_id"] = labels
        self._write_arc_disc_fixture(payload)

    def set_arc_disc_burn_copy_available(self, copy_id: str, *, available: bool) -> None:
        payload = self._load_arc_disc_fixture()
        burn = cast(dict[str, object], payload["burn"])
        available_copy_ids = set(cast(list[str], burn.get("available_copy_ids", [])))
        if available:
            available_copy_ids.add(copy_id)
        else:
            available_copy_ids.discard(copy_id)
        burn["available_copy_ids"] = sorted(available_copy_ids)
        self._write_arc_disc_fixture(payload)

    def fail_arc_disc_burn_copy(self, copy_id: str) -> None:
        payload = self._load_arc_disc_fixture()
        burn = cast(dict[str, object], payload["burn"])
        failures = set(cast(list[str], burn.get("fail_copy_ids", [])))
        failures.add(copy_id)
        burn["fail_copy_ids"] = sorted(failures)
        self._write_arc_disc_fixture(payload)

    def fail_arc_disc_burn_copy_verification(self, copy_id: str) -> None:
        payload = self._load_arc_disc_fixture()
        burn = cast(dict[str, object], payload["burn"])
        failures = set(cast(list[str], burn.get("verify_fail_copy_ids", [])))
        failures.add(copy_id)
        burn["verify_fail_copy_ids"] = sorted(failures)
        self._write_arc_disc_fixture(payload)

    def clear_arc_disc_burn_failures(self) -> None:
        payload = self._load_arc_disc_fixture()
        burn = cast(dict[str, object], payload["burn"])
        burn["fail_copy_ids"] = []
        burn["verify_fail_copy_ids"] = []
        burn["blank_media_blocked_copy_ids"] = []
        self._write_arc_disc_fixture(payload)

    def corrupt_arc_disc_staged_iso(self, image_id: str) -> None:
        image = self.request("GET", f"/v1/images/{image_id}").json()
        staging_path = self.workspace / "arc_disc_staging" / image_id / str(image["filename"])
        if not staging_path.is_file():
            raise AssertionError(f"staged ISO not found: {staging_path}")
        staging_path.write_bytes(staging_path.read_bytes() + b"corrupted-by-fixture\n")

    def arc_disc_staged_iso_exists(self, image_id: str) -> bool:
        image = self.request("GET", f"/v1/images/{image_id}").json()
        staging_path = self.workspace / "arc_disc_staging" / image_id / str(image["filename"])
        return staging_path.is_file()

    def pins_list(self) -> list[str]:
        return [item["target"] for item in self.request("GET", "/v1/pins").json()["pins"]]

    def collection_source_root(self, collection_id: str) -> Path:
        return (
            self.workspace / "collections-src" / normalize_collection_id(collection_id)
        ).resolve()

    def inspect_downloaded_iso(self, *, image_id: str, iso_bytes: bytes) -> InspectedIso:
        return inspect_downloaded_iso(
            image_id=image_id,
            iso_bytes=iso_bytes,
            workspace=self.workspace,
        )

    def expire_collection_upload(self, collection_id: str) -> None:
        normalized_collection_id = normalize_collection_id(collection_id)
        with session_scope(make_session_factory(str(self.db_path))) as session:
            records = session.scalars(
                select(CollectionUploadFileRecord).where(
                    CollectionUploadFileRecord.collection_id == normalized_collection_id
                )
            ).all()
            assert records, f"collection upload not found: {normalized_collection_id}"
            for record in records:
                record.upload_expires_at = "2000-01-01T00:00:00Z"

    def expire_fetch_upload(self, fetch_id: str, entry_id: str) -> None:
        with session_scope(make_session_factory(str(self.db_path))) as session:
            record = session.get(
                FetchEntryRecord,
                {
                    "fetch_id": fetch_id,
                    "entry_id": entry_id,
                },
            )
            assert record is not None, f"fetch entry not found: {fetch_id}/{entry_id}"
            record.upload_expires_at = "2000-01-01T00:00:00Z"

    def wait_for_collection_upload_cleanup(self, collection_id: str, timeout: float = 5.0) -> None:
        normalized_collection_id = normalize_collection_id(collection_id)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with session_scope(make_session_factory(str(self.db_path))) as session:
                remaining = session.scalars(
                    select(CollectionUploadFileRecord).where(
                        CollectionUploadFileRecord.collection_id == normalized_collection_id
                    )
                ).all()
            if not remaining:
                return
            time.sleep(0.05)
        raise AssertionError(f"timed out waiting for collection upload cleanup: {collection_id}")

    def wait_for_fetch_upload_cleanup(
        self,
        fetch_id: str,
        entry_id: str,
        timeout: float = 5.0,
    ) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with session_scope(make_session_factory(str(self.db_path))) as session:
                record = session.get(
                    FetchEntryRecord,
                    {
                        "fetch_id": fetch_id,
                        "entry_id": entry_id,
                    },
                )
                assert record is not None, f"fetch entry not found: {fetch_id}/{entry_id}"
                if (
                    record.tus_url is None
                    and record.uploaded_bytes == 0
                    and record.upload_expires_at is None
                ):
                    return
            time.sleep(0.05)
        raise AssertionError(f"timed out waiting for fetch upload cleanup: {fetch_id}/{entry_id}")

    def _clear_fixture_path(self) -> None:
        if self.fixture_path.exists():
            self.fixture_path.unlink()

    def _subprocess_env(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        env = os.environ.copy()
        pythonpath_parts = [str(ROOT) for ROOT in (SRC_ROOT, REPO_ROOT)]
        existing = env.get("PYTHONPATH")
        if existing:
            pythonpath_parts.append(existing)
        env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
        env["ARC_BASE_URL"] = self.base_url
        env["ARC_DB_PATH"] = str(self.db_path)
        if extra:
            env.update(extra)
        return env

    def _selected_file_bytes(self, collection_id: str, path: str) -> int:
        with session_scope(make_session_factory(str(self.db_path))) as session:
            record = session.get(
                CollectionFileRecord,
                {
                    "collection_id": collection_id,
                    "path": path,
                },
            )
            assert record is not None
            return record.bytes

    def _file_bytes(self, collection_id: str, path: str) -> bytes:
        response = self._s3_client().get_object(
            Bucket=load_runtime_config().s3_bucket,
            Key=self._collection_key(collection_id, path),
        )
        return response["Body"].read()

    def _file_part_bytes(
        self,
        collection_id: str,
        path: str,
        part_index: int,
        part_count: int,
    ) -> bytes:
        return split_fixture_plaintext(
            self._file_bytes(collection_id, path),
            part_count,
        )[part_index]

    def _collection_exists(self, collection_id: str) -> bool:
        response = self.request("GET", f"/v1/collections/{collection_id}")
        if response.status_code == 200:
            return True
        if response.status_code == 404:
            return False
        raise AssertionError(response.text)

    def __getattr__(self, name: str) -> object:
        raise NotImplementedError(
            f"production acceptance suite does not provide fixture-backed state for {name}"
        )


@pytest.fixture
def acceptance_system(tmp_path: Path) -> Iterator[ProductionSystem]:
    _require_canonical_test_entrypoint()
    workspace = (REPO_ROOT / ".tmp" / "acceptance" / tmp_path.name).resolve()
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    try:
        system = ProductionSystem.create(workspace)
    except RuntimeError as exc:
        pytest.skip(str(exc))
    try:
        yield system
    finally:
        system.close()
        shutil.rmtree(workspace, ignore_errors=True)
