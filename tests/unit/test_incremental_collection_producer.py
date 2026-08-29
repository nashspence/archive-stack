from __future__ import annotations

import hashlib
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest
from riverhog_api_client import (
    IncrementalCollectionProducer,
    ProducerArtifactIdentity,
    ProducerFile,
)
from riverhog_core.app_permissions import (
    ALL_RESOURCES,
    COLLECTIONS_CREATE,
    ApplicationAccess,
    ApplicationPrincipal,
)
from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import initialize_db, make_session_factory, session_scope
from riverhog_core.catalog_models import TagRecord
from riverhog_core.collection_plan import CollectionVolumePolicy
from riverhog_core.proofs import ProofStamper
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.collection_uploads import SqlAlchemyCollectionUploadService
from riverhog_protocol import (
    CollectionUploadArtifactCustodyReceiptDocument,
    CollectionUploadCustodyObjectDocument,
    CollectionUploadUnitWorkDocument,
    CollectionUploadVolumeSetDocument,
)
from riverhog_protocol.collection_workflows import DERIVATION_EVIDENCE_PATH
from riverhog_protocol.paths import tag_set_identity

from tests.fixtures.crypto import FixtureProofStamper
from tests.unit.archive_object_fixtures import MemoryArchiveStore, archive_store_binding
from tests.unit.db_helpers import sqlite_url


class _CustodyApi:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, object]] = {}
        self.completed: dict[str, object] | None = None
        self.heartbeats = 0

    def spawn(self) -> _CustodyApi:
        return self

    def close(self) -> None:
        pass

    def create_or_resume_collection_upload_session(
        self,
        _idempotency_key: str,
        _tags: Sequence[str],
        **kwargs: object,
    ) -> dict[str, object]:
        assert kwargs["custody_mode"] == "custody-transfer"
        return {
            "collection_id": 42,
            "state": "open",
            "registration_constraints": {
                "pack_member_bytes": 1024,
                "raw_part_plaintext_bytes": 65536,
            },
        }

    def heartbeat_collection_upload_session(self, _collection_id: int) -> dict[str, object]:
        self.heartbeats += 1
        return {"state": "open"}

    def list_collection_upload_session_files(
        self,
        _collection_id: int,
        **_kwargs: object,
    ) -> dict[str, object]:
        return {"files": list(self.rows.values())}

    @contextmanager
    def stream_collection_upload_session_files(
        self,
        collection_id: int,
    ) -> Iterator[Iterator[dict[str, object]]]:
        payload = self.list_collection_upload_session_files(collection_id)
        yield iter(payload["files"])  # type: ignore[arg-type]

    def register_collection_upload_session_files(
        self,
        _collection_id: int,
        files: Sequence[Mapping[str, object]],
        **_kwargs: object,
    ) -> dict[str, object]:
        for supplied in files:
            row = dict(supplied)
            path = str(row["path"])
            receipt = CollectionUploadArtifactCustodyReceiptDocument.seal(
                collection_id=42,
                path=path,
                bytes=int(row["bytes"]),
                sha256=str(row["sha256"]),
                archive_objects=(
                    CollectionUploadCustodyObjectDocument(
                        volume_id=f"pack-{len(self.rows):012d}",
                        sealed_receipt_sha256="f" * 64,
                    ),
                ),
            )
            row["custody_receipt"] = receipt.model_dump(mode="json")
            self.rows[path] = row
        return {"files": list(self.rows.values()), "volumes": []}

    def list_collection_upload_session_volumes(
        self,
        collection_id: int,
    ) -> CollectionUploadVolumeSetDocument:
        return CollectionUploadVolumeSetDocument(collection_id=collection_id, volumes=())

    def put_collection_upload_session_provenance_journal(
        self, *_args: object, **_kwargs: object
    ) -> None:
        raise AssertionError("omitted-provenance fixture must not publish journals")

    def complete_collection_upload_session(
        self,
        _collection_id: int,
        *,
        content_identity: str,
        **_kwargs: object,
    ) -> dict[str, object]:
        self.completed = {
            "state": "finalized",
            "content_identity": content_identity,
            "archive_root_sha256": "e" * 64,
            "collection": {
                "id": 42,
                "content_identity": content_identity,
                "archive_root_sha256": "e" * 64,
            },
        }
        return dict(self.completed)


def _producer(api: _CustodyApi) -> IncrementalCollectionProducer:
    return IncrementalCollectionProducer(
        api,  # type: ignore[arg-type]
        producer_app="fixture-target",
        adapter_id="fixture-target/v1",
        adapter_version="1.0.0",
        ingest_source="transform:fixture",
        tags=("derived",),
        source_event_id="fixture-execution",
        idempotency_key="fixture-execution",
    )


def test_incremental_producer_resumes_without_rereading_custodied_local_bytes(
    tmp_path: Path,
) -> None:
    api = _CustodyApi()
    source = tmp_path / "artifact.bin"
    source.write_bytes(b"completed artifact")
    first = _producer(api)
    try:
        receipts = first.append_inputs((ProducerFile(source, "z/artifact.bin"),))
        assert "z/artifact.bin" in {item.artifact.path for item in receipts}
        assert "z/artifact.bin" in first.custody_receipts
    finally:
        first.stop()

    resumed_input = ProducerFile(source, "z/artifact.bin")
    source.unlink()
    resumed = _producer(api)
    try:
        with pytest.raises(ValueError, match="differs from its expected identity"):
            resumed.append_inputs(
                (resumed_input,),
                expected_identities={
                    "z/artifact.bin": ProducerArtifactIdentity(
                        "z/artifact.bin",
                        len(b"completed artifact"),
                        "0" * 64,
                    )
                },
            )
        assert resumed.append_inputs((resumed_input,)) == ()
        produced = resumed.finish(
            terminal_evidence={DERIVATION_EVIDENCE_PATH: b'{"format":"fixture/v1"}'},
        )
    finally:
        resumed.stop()

    assert produced.collection_id == 42
    assert produced.archive_root_sha256 == "e" * 64
    assert DERIVATION_EVIDENCE_PATH in api.rows
    assert api.completed is not None
    registered = tuple(api.rows)
    assert registered == (
        "riverhog/producer-evidence.json",
        "z/artifact.bin",
        DERIVATION_EVIDENCE_PATH,
    )
    expected = api.rows["z/artifact.bin"]
    assert expected["sha256"] == hashlib.sha256(b"completed artifact").hexdigest()


def test_incremental_producer_keeps_local_custody_when_receipt_identity_is_wrong(
    tmp_path: Path,
) -> None:
    class _WrongReceiptApi(_CustodyApi):
        def register_collection_upload_session_files(
            self,
            collection_id: int,
            files: Sequence[Mapping[str, object]],
            **kwargs: object,
        ) -> dict[str, object]:
            result = super().register_collection_upload_session_files(
                collection_id,
                files,
                **kwargs,
            )
            row = self.rows.get("z/artifact.bin")
            if row is not None:
                row["custody_receipt"] = CollectionUploadArtifactCustodyReceiptDocument.seal(
                    collection_id=collection_id,
                    path="z/other.bin",
                    bytes=int(row["bytes"]),
                    sha256=str(row["sha256"]),
                    archive_objects=(
                        CollectionUploadCustodyObjectDocument(
                            volume_id="pack-000000000000",
                            sealed_receipt_sha256="f" * 64,
                        ),
                    ),
                ).model_dump(mode="json")
            return result

    api = _WrongReceiptApi()
    source = tmp_path / "artifact.bin"
    source.write_bytes(b"completed artifact")
    producer = _producer(api)
    try:
        with pytest.raises(ValueError, match="upload file identity"):
            producer.append_inputs((ProducerFile(source, "z/artifact.bin"),))
        assert "z/artifact.bin" in producer._sources  # noqa: SLF001
        assert "z/artifact.bin" not in producer.custody_receipts
    finally:
        producer.stop()


def test_incremental_producer_inserts_evidence_when_one_append_crosses_its_path(
    tmp_path: Path,
) -> None:
    api = _CustodyApi()
    before = tmp_path / "before.bin"
    after = tmp_path / "after.bin"
    before.write_bytes(b"before evidence")
    after.write_bytes(b"after evidence")
    producer = _producer(api)
    try:
        producer.append_inputs(
            (
                ProducerFile(before, "a.txt"),
                ProducerFile(after, "z.txt"),
            )
        )
        producer.finish(
            terminal_evidence={DERIVATION_EVIDENCE_PATH: b'{"format":"fixture/v1"}'},
        )
    finally:
        producer.stop()

    assert tuple(api.rows) == (
        "a.txt",
        "riverhog/producer-evidence.json",
        "z.txt",
        DERIVATION_EVIDENCE_PATH,
    )


class _ServiceApi:
    def __init__(
        self,
        service: SqlAlchemyCollectionUploadService,
        principal: ApplicationPrincipal,
    ) -> None:
        self.service = service
        self.principal = principal
        self.volume_list_calls = 0

    def spawn(self) -> _ServiceApi:
        return self

    def close(self) -> None:
        pass

    def create_or_resume_collection_upload_session(
        self,
        idempotency_key: str,
        tags: Sequence[str],
        **kwargs: object,
    ) -> dict[str, object]:
        return self.service.create_or_resume(
            idempotency_key=idempotency_key,
            initial_tag=(tags[0] if tags else None),
            tag_set_identity_sha256=tag_set_identity(sorted(tags)),
            ingest_source=str(kwargs["ingest_source"]),
            archive_store=None,
            initiator=self.principal,
            event_context=None,
            provenance_mode=str(kwargs["provenance_mode"]),
            provenance_omission_reason=str(kwargs["provenance_omission_reason"]),
            custody_mode=str(kwargs["custody_mode"]),
        )

    def heartbeat_collection_upload_session(self, collection_id: int) -> dict[str, object]:
        return self.service.heartbeat(collection_id)

    def list_collection_upload_session_files(
        self,
        collection_id: int,
        **_kwargs: object,
    ) -> dict[str, object]:
        payload = self.service.list_files(collection_id, page=1, per_page=100)
        raw_files = payload["files"]
        assert isinstance(raw_files, list)
        files = []
        for raw in raw_files:
            assert isinstance(raw, Mapping)
            row = dict(raw)
            receipt = row.get("custody_receipt")
            if isinstance(receipt, CollectionUploadArtifactCustodyReceiptDocument):
                row["custody_receipt"] = receipt.model_dump(mode="json")
            files.append(row)
        return {**payload, "files": files}

    @contextmanager
    def stream_collection_upload_session_files(
        self,
        collection_id: int,
    ) -> Iterator[Iterator[dict[str, object]]]:
        payload = self.list_collection_upload_session_files(collection_id)
        yield iter(payload["files"])  # type: ignore[arg-type]

    def register_collection_upload_session_files(
        self,
        collection_id: int,
        files: Sequence[Mapping[str, object]],
        **_kwargs: object,
    ) -> dict[str, object]:
        return self.service.register_files(collection_id, files)

    def list_collection_upload_session_volumes(
        self,
        collection_id: int,
    ) -> CollectionUploadVolumeSetDocument:
        self.volume_list_calls += 1
        return CollectionUploadVolumeSetDocument.model_validate(
            self.service.list_volumes(collection_id)
        )

    def get_collection_upload_session_unit(
        self,
        collection_id: int,
        volume_id: str,
        unit: int,
    ) -> CollectionUploadUnitWorkDocument:
        return CollectionUploadUnitWorkDocument.model_validate(
            self.service.get_unit(collection_id, volume_id, unit)
        )

    def put_collection_upload_session_unit(
        self,
        collection_id: int,
        volume_id: str,
        unit: int,
        *,
        plan_sha256: str,
        content: bytes,
    ) -> CollectionUploadUnitWorkDocument:
        return CollectionUploadUnitWorkDocument.model_validate(
            self.service.upload_unit(
                collection_id,
                volume_id,
                unit,
                plan_sha256=plan_sha256,
                content=content,
            )
        )

    def complete_collection_upload_session(
        self,
        collection_id: int,
        *,
        files_total: int,
        content_identity: str,
        provenance_identity: str | None,
    ) -> dict[str, object]:
        return self.service.complete(
            collection_id,
            files_total=files_total,
            content_identity=content_identity,
            provenance_identity=provenance_identity,
        )

    def get_collection_upload_session(self, collection_id: int) -> dict[str, object]:
        self.service.process_due_finalizations(limit=1)
        return self.service.get(collection_id)


def _bounded_service_api(
    tmp_path: Path,
    *,
    proof_stamper: ProofStamper,
) -> tuple[_ServiceApi, MemoryArchiveStore]:
    database_url = sqlite_url(tmp_path / "catalog.sqlite3")
    config = RuntimeConfig(database_url=database_url, archive_scrypt_work_factor=1)
    initialize_db(database_url)
    with session_scope(make_session_factory(database_url)) as session:
        session.add(
            TagRecord(
                id="derived",
                created_by_app="fixture",
                created_at="2026-08-25T00:00:00.000000Z",
            )
        )
    store = MemoryArchiveStore()
    binding = replace(archive_store_binding(store), store=store)
    service = SqlAlchemyCollectionUploadService(
        config,
        ArchiveStoreRegistry({"archive": binding}),
        proof_stamper=proof_stamper,
        policy=CollectionVolumePolicy(
            pack_source_bytes=1024,
            pack_files=4,
            pack_member_bytes=1024,
            pack_part_plaintext_bytes=5 * 1024 * 1024,
            raw_volume_plaintext_bytes=5 * 1024 * 1024,
            raw_part_plaintext_bytes=5 * 1024 * 1024,
        ),
    )
    principal = ApplicationPrincipal(
        app="fixture-target",
        key_id="fixture-key",
        access=frozenset({ApplicationAccess(COLLECTIONS_CREATE, ALL_RESOURCES)}),
    )
    return _ServiceApi(service, principal), store


def test_many_artifact_publication_retains_only_the_unsealed_pack_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RIVERHOG_UPLOAD_FILE_CONCURRENCY", "1")
    api, store = _bounded_service_api(tmp_path, proof_stamper=FixtureProofStamper())
    producer = IncrementalCollectionProducer(
        api,  # type: ignore[arg-type]
        producer_app="fixture-target",
        adapter_id="fixture-target/v1",
        adapter_version="1.0.0",
        ingest_source="transform:fixture",
        tags=("derived",),
        source_event_id="many-artifact-execution",
        idempotency_key="many-artifact-execution",
    )
    local: dict[str, Path] = {}
    high_water = 0
    try:
        for index in range(25):
            path = f"z/output-{index:04d}.bin"
            source = tmp_path / f"output-{index:04d}.bin"
            source.write_bytes(f"artifact-{index}".encode())
            local[path] = source
            high_water = max(high_water, sum(item.exists() for item in local.values()))
            receipts = producer.append_inputs((ProducerFile(source, path),))
            for receipt in receipts:
                owned = local.get(receipt.artifact.path)
                if owned is not None:
                    owned.unlink()
            high_water = max(high_water, sum(item.exists() for item in local.values()))
        result = producer.finish(
            terminal_evidence={DERIVATION_EVIDENCE_PATH: b'{"format":"fixture/v1"}'},
            poll_seconds=0.01,
            timeout_seconds=10,
        )
        for path, _custody in producer.custody_receipts.items():
            owned = local.get(path)
            if owned is not None and owned.exists():
                owned.unlink()
    finally:
        producer.stop()

    assert result.receipt["state"] == "finalized"
    assert high_water <= 5  # four unsealed members plus the newly completed artifact
    assert not any(path.exists() for path in local.values())
    assert len([path for path in store.objects if "/volumes/" in path]) >= 7
    assert api.volume_list_calls < len(local)
