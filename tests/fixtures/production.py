from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import httpx
import pytest
from sqlalchemy import select

from arc_api.app import create_app
from arc_core.catalog_models import (
    ActivePinRecord,
    CandidateCoveredPathRecord,
    CollectionFileRecord,
    CollectionRecord,
    FetchEntryRecord,
    FileCopyRecord,
    FinalizedImageCoveredPathRecord,
    FinalizedImageRecord,
    PlannedCandidateRecord,
)
from arc_core.domain.enums import FetchState
from arc_core.domain.models import CollectionSummary, CopySummary, FetchCopyHint, FetchSummary
from arc_core.domain.selectors import parse_target
from arc_core.domain.types import CollectionId, CopyId, FetchId, TargetStr
from arc_core.fs_paths import normalize_collection_id
from arc_core.sqlite_db import make_session_factory, session_scope
from tests.fixtures.acceptance import REPO_ROOT, SRC_ROOT, _LiveServerHandle, _reserve_local_port
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


class ProductionCollectionsClient:
    def __init__(self, system: ProductionSystem) -> None:
        self._system = system

    def get(self, collection_id: str) -> CollectionSummary:
        response = self._system.request("GET", f"/v1/collections/{collection_id}")
        payload = response.json()
        return CollectionSummary(
            id=CollectionId(payload["id"]),
            files=payload["files"],
            bytes=payload["bytes"],
            hot_bytes=payload["hot_bytes"],
            archived_bytes=payload["archived_bytes"],
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
            covered = tuple(
                (cp.collection_id, cp.path) for cp in record.covered_paths
            )
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

    def register(self, image_id: str, copy_id: str, location: str) -> CopySummary:
        payload = self._system.request(
            "POST",
            f"/v1/images/{image_id}/copies",
            json_body={"id": copy_id, "location": location},
        ).json()
        copy = payload["copy"]
        return CopySummary(
            id=CopyId(str(copy["id"])),
            volume_id=str(copy["volume_id"]),
            location=str(copy["location"]),
            created_at=str(copy["created_at"]),
        )


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
    server: _LiveServerHandle
    base_url: str
    fixture_path: Path
    previous_arc_staging_root: str | None
    previous_arc_db_path: str | None
    collections: ProductionCollectionsClient
    fetches: ProductionFetchesClient
    state: ProductionStateClient
    planning: ProductionPlanningClient
    copies: ProductionCopiesClient

    @property
    def db_path(self) -> Path:
        return (self.workspace / ".arc" / "state.sqlite3").resolve()

    @classmethod
    def create(cls, workspace: Path) -> ProductionSystem:
        previous_arc_staging_root = os.environ.get("ARC_STAGING_ROOT")
        previous_arc_db_path = os.environ.get("ARC_DB_PATH")
        os.environ["ARC_STAGING_ROOT"] = str((workspace / "staging").resolve())
        os.environ["ARC_DB_PATH"] = str((workspace / ".arc" / "state.sqlite3").resolve())
        app = create_app()
        fixture_path = workspace / "arc_disc_fixture.json"
        with _reserve_local_port() as reserved:
            server = _LiveServerHandle(app, host="127.0.0.1", port=reserved.port)
        server.start()
        system = cls(
            workspace=workspace,
            server=server,
            base_url=server.base_url,
            fixture_path=fixture_path,
            previous_arc_staging_root=previous_arc_staging_root,
            previous_arc_db_path=previous_arc_db_path,
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
        return system

    def close(self) -> None:
        self.server.close()
        self._restore_environment()

    def restart(self) -> None:
        self.server.close()
        app = create_app()
        with _reserve_local_port() as reserved:
            server = _LiveServerHandle(app, host="127.0.0.1", port=reserved.port)
        server.start()
        self.server = server
        self.base_url = server.base_url

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
        with httpx.Client(base_url=self.base_url, timeout=5.0) as client:
            return client.request(
                method,
                path,
                params=params,
                json=json_body,
                headers=headers,
                content=content,
            )

    def run_arc(self, *args: str) -> subprocess.CompletedProcess[str]:
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
            self.configure_arc_disc_fixture()
        return subprocess.run(
            [sys.executable, "-m", "arc_disc.main", *args],
            cwd=REPO_ROOT,
            env=self._subprocess_env(
                {
                    "ARC_DISC_FIXTURE_PATH": str(self.fixture_path),
                    "ARC_DISC_READER_FACTORY": "tests.fixtures.arc_disc_fakes:FixtureOpticalReader",
                }
            ),
            input=input_text,
            capture_output=True,
            text=True,
            check=False,
        )

    def seed_staged_collection(
        self, collection_id: str, files: Mapping[str, bytes] | None = None
    ) -> None:
        normalized_collection_id = normalize_collection_id(collection_id)
        write_tree(
            self.workspace / "staging" / normalized_collection_id, files or PHOTOS_2024_FILES
        )

    def seed_collection_closed(
        self, collection_id: str, files: Mapping[str, bytes]
    ) -> None:
        normalized_collection_id = normalize_collection_id(collection_id)
        self.seed_staged_collection(normalized_collection_id, files)
        response = self.request(
            "POST",
            "/v1/collections/close",
            json_body={"path": f"/staging/{normalized_collection_id}"},
        )
        assert response.status_code == 200, response.text

    def seed_photos_hot(self) -> None:
        self.seed_collection_closed("photos-2024", PHOTOS_2024_FILES)

    def seed_planner_fixtures(self) -> None:
        self.seed_docs_hot()
        self.seed_photos_hot()
        self.seed_image_fixtures(IMAGE_FIXTURES)

    def seed_split_planner_fixtures(self) -> None:
        self.seed_docs_hot()
        self.seed_image_fixtures(SPLIT_IMAGE_FIXTURES)

    def seed_image_fixtures(self, fixtures: tuple[ImageFixture, ...]) -> None:
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

    def seed_nested_photos_hot(self) -> None:
        self.seed_collection_closed("photos/2024", PHOTOS_2024_FILES)

    def seed_parent_photos_hot(self) -> None:
        self.seed_collection_closed("photos", PHOTOS_2024_FILES)

    def seed_docs_hot(self) -> None:
        if not self._collection_exists("docs"):
            self.seed_collection_closed("docs", DOCS_FILES)

    def seed_docs_archive(self) -> None:
        files = self.state.selected_files(
            f"{DOCS_COLLECTION_ID}/tax/2022/invoice-123.pdf", missing_ok=True
        )
        if files and files[0].archived:
            return
        self.seed_docs_hot()
        self.seed_image_fixtures((IMAGE_FIXTURES[0],))
        resp = self.request("POST", f"/v1/plan/candidates/{IMAGE_FIXTURES[0].id}/finalize")
        assert resp.status_code == 200, resp.text
        image_id = resp.json()["id"]
        resp = self.request(
            "POST", f"/v1/images/{image_id}/copies",
            json_body={"id": "copy-docs-1", "location": "vault-a/shelf-01"},
        )
        assert resp.status_code == 200, resp.text
        with session_scope(make_session_factory(str(self.db_path))) as session:
            record = session.get(
                CollectionFileRecord,
                {"collection_id": DOCS_COLLECTION_ID, "path": "tax/2022/invoice-123.pdf"},
            )
            assert record is not None
            record.hot = False

    def seed_docs_archive_with_split_invoice(self) -> None:
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
        ):
            resp = self.request("POST", f"/v1/plan/candidates/{fixture.id}/finalize")
            assert resp.status_code == 200, resp.text
            image_id = resp.json()["id"]
            resp = self.request(
                "POST", f"/v1/images/{image_id}/copies",
                json_body={"id": copy_id, "location": location},
            )
            assert resp.status_code == 200, resp.text
        with session_scope(make_session_factory(str(self.db_path))) as session:
            record = session.get(
                CollectionFileRecord,
                {"collection_id": DOCS_COLLECTION_ID, "path": SPLIT_FILE_RELPATH},
            )
            assert record is not None
            record.hot = False

    def seed_search_fixtures(self) -> None:
        self.seed_docs_archive()
        self.seed_photos_hot()

    def seed_pin(self, target: str) -> None:
        response = self.request("POST", "/v1/pin", json_body={"target": target})
        assert response.status_code == 200, response.text

    def upload_buffer_absent(self, fetch_id: str) -> bool:
        buffer_dir = self.workspace / "staging" / ".arc_uploads" / fetch_id
        return not buffer_dir.exists()

    def upload_required_entries(self, fetch_id: str) -> None:
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

        for entry in manifest["entries"]:
            entry_path = str(entry["path"])
            parts = entry["parts"]
            plaintext_parts = split_fixture_plaintext(
                self._file_bytes(collection_id_by_path[entry_path], entry_path),
                len(parts),
            )
            for part in parts:
                part_index = int(part["index"])
                payload = fixture_encrypt_bytes(plaintext_parts[part_index])
                for copy in part["copies"]:
                    copy_id = str(copy["copy"])
                    disc_path = str(copy["disc_path"])
                    encoded = payload
                    if entry_path == corrupt_path or copy_id in corrupt_copy_ids:
                        encoded = payload + b"corrupted-by-fixture\n"
                    payload_by_disc_path[disc_path] = base64.b64encode(encoded).decode("ascii")
                    if entry_path == fail_path or copy_id in fail_copy_ids:
                        fail_disc_paths.append(disc_path)

        self.fixture_path.write_text(
            json.dumps(
                {
                    "reader": {
                        "payload_by_disc_path": payload_by_disc_path,
                        "fail_disc_paths": fail_disc_paths,
                    }
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    def pins_list(self) -> list[str]:
        return [item["target"] for item in self.request("GET", "/v1/pins").json()["pins"]]

    def _subprocess_env(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        env = os.environ.copy()
        pythonpath_parts = [str(ROOT) for ROOT in (SRC_ROOT, REPO_ROOT)]
        existing = env.get("PYTHONPATH")
        if existing:
            pythonpath_parts.append(existing)
        env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
        env["ARC_BASE_URL"] = self.base_url
        env["ARC_STAGING_ROOT"] = str((self.workspace / "staging").resolve())
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
        with session_scope(make_session_factory(str(self.db_path))) as session:
            collection = session.get(CollectionRecord, collection_id)
            assert collection is not None, f"collection not found: {collection_id}"
            source_staging_path = collection.source_staging_path
        # source_staging_path is like "/staging/docs"; staging root is workspace/staging
        rel_parts = source_staging_path.lstrip("/").split("/")
        return self.workspace.joinpath(*rel_parts).joinpath(path).read_bytes()

    def _file_part_bytes(self, collection_id: str, path: str, part_index: int, part_count: int) -> bytes:
        return split_fixture_plaintext(
            self._file_bytes(collection_id, path),
            part_count,
        )[part_index]

    def _restore_environment(self) -> None:
        if self.previous_arc_staging_root is None:
            os.environ.pop("ARC_STAGING_ROOT", None)
        else:
            os.environ["ARC_STAGING_ROOT"] = self.previous_arc_staging_root

        if self.previous_arc_db_path is None:
            os.environ.pop("ARC_DB_PATH", None)
        else:
            os.environ["ARC_DB_PATH"] = self.previous_arc_db_path

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
    system = ProductionSystem.create(tmp_path)
    try:
        yield system
    finally:
        system.close()
