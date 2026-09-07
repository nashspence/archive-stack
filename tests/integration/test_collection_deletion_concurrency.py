from __future__ import annotations

import hashlib
import os
import threading
from collections.abc import Iterator, Sequence
from dataclasses import replace
from typing import cast

import pytest
from pydantic import JsonValue
from riverhog_core.app_permissions import ApplicationPrincipal
from riverhog_core.archive_store_registry import ArchiveStoreBinding, ArchiveStoreRegistry
from riverhog_core.catalog_base import Base
from riverhog_core.catalog_db import (
    STATE_VERSION_TABLE,
    create_catalog_engine,
    initialize_db,
    make_session_factory,
    session_scope,
)
from riverhog_core.catalog_models import (
    CollectionArchiveCopyRecord,
    CollectionArchiveFileObjectRecord,
    CollectionArchiveObjectRecord,
    CollectionDeletionRecord,
    CollectionFileRecord,
    CollectionMetadataPublicationRecord,
    CollectionRecord,
    CollectionTagRecord,
    CollectionUploadRecord,
    RetrievalJobRecord,
    RetrievalPlanObjectRecord,
    TagRecord,
)
from riverhog_core.catalog_workflow_models import CollectionDerivationRecord
from riverhog_core.ports.archive_store import (
    ArchiveObjectIdentity,
    ArchiveStore,
    CollectionArchiveIdentity,
    MutableManifestReceipt,
)
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.archive_copy_retirements import (
    SqlAlchemyArchiveCopyRetirementService,
)
from riverhog_core.services.archive_maintenance import SqlAlchemyArchiveMaintenanceService
from riverhog_core.services.collection_deletions import SqlAlchemyCollectionDeletionService
from riverhog_core.services.collection_uploads import SqlAlchemyCollectionUploadService
from riverhog_core.services.collection_workflows import SqlAlchemyCollectionWorkflowService
from riverhog_core.services.retrieval import SqlAlchemyRetrievalService
from riverhog_protocol.collection_workflows import (
    DERIVATION_EVIDENCE_PATH,
    PRODUCER_EVIDENCE_PATH,
    ArtifactDisposition,
    ArtifactDispositionOutput,
    ArtifactDispositionSetIdentity,
    CollectionArtifactIdentity,
    CollectionDerivation,
    CollectionProcessingOutcomeIdentity,
    CollectionRootIdentity,
    OperationIdentity,
    RecipeIdentity,
    canonical_json_bytes,
    canonical_json_sha256,
    derivation_evidence_page_path,
)
from riverhog_protocol.errors import Conflict, NotFound
from riverhog_protocol.paths import tag_set_identity
from sqlalchemy import select, text

from tests.unit.archive_object_fixtures import MemoryArchiveStore, archive_store_binding

pytestmark = pytest.mark.integration

COLLECTION_ID = 1
SECOND_COLLECTION_ID = 3
FILE_PATH = "document.txt"
CONTENT = b"archived document"
SECOND_FILE_PATH = "second.txt"
SECOND_CONTENT = b"second archived document"
DELETER = ApplicationPrincipal(
    app="riverhog-client",
    key_id="client-key",
    access=frozenset(),
)
WORKFLOW_PRINCIPAL = ApplicationPrincipal(
    app="stove0",
    key_id="controller",
    access=frozenset(),
)
WORK_ID = "8" * 64
EXECUTION_ID = "9" * 64
OPERATION = OperationIdentity("fixture.transform/v1", "7" * 64)


def _workflow_artifact(root: CollectionRootIdentity) -> CollectionArtifactIdentity:
    return CollectionArtifactIdentity(
        collection=root,
        path=FILE_PATH,
        bytes=len(CONTENT),
        sha256=hashlib.sha256(CONTENT).hexdigest(),
    )


class BlockingArchiveStore:
    def __init__(self) -> None:
        self.delete_started = threading.Event()
        self.allow_delete = threading.Event()
        self.metadata_started = threading.Event()
        self.allow_metadata = threading.Event()
        self.deleted: list[tuple[str, ...]] = []
        self.published_metadata: list[bytes] = []

    def read_mode(self) -> str:
        return "immediate"

    def delete_collection_archive(
        self,
        *,
        collection_id: int,
        objects: Sequence[ArchiveObjectIdentity],
    ) -> None:
        assert collection_id == COLLECTION_ID
        self.delete_started.set()
        if not self.allow_delete.wait(10):
            raise RuntimeError("timed out waiting to finish archive deletion")
        self.deleted.append(tuple(current.object_id for current in objects))

    def publish_collection_metadata(
        self,
        *,
        collection_id: int,
        archive_storage_prefix: str,
        manifest: bytes,
        passphrase_id: str,
    ) -> MutableManifestReceipt:
        assert collection_id == COLLECTION_ID
        assert passphrase_id == "fixture-archive-key-v1"
        self.metadata_started.set()
        if not self.allow_metadata.wait(10):
            raise RuntimeError("timed out waiting to finish metadata publication")
        self.published_metadata.append(manifest)
        return MutableManifestReceipt(
            object_path=f"{archive_storage_prefix}/metadata.json.age",
            revision="metadata-version",
            stored_bytes=len(manifest),
            stored_sha256=hashlib.sha256(manifest).hexdigest(),
            published_at="2026-07-18T00:00:00.000000Z",
        )

    def verify_collection_archive(
        self,
        *,
        collection_id: int,
        archive: CollectionArchiveIdentity,
    ) -> None:
        assert collection_id == COLLECTION_ID
        assert archive.objects


class RetirementArchiveStore:
    def __init__(self) -> None:
        self.deleted_collections: list[int] = []

    def read_mode(self) -> str:
        return "immediate"

    def delete_collection_archive(
        self,
        *,
        collection_id: int,
        objects: Sequence[ArchiveObjectIdentity],
    ) -> None:
        assert objects
        self.deleted_collections.append(collection_id)

    def publish_collection_metadata(
        self,
        *,
        collection_id: int,
        archive_storage_prefix: str,
        manifest: bytes,
        passphrase_id: str,
    ) -> MutableManifestReceipt:
        raise AssertionError("retirement path does not publish mutable metadata")

    def verify_collection_archive(
        self,
        *,
        collection_id: int,
        archive: CollectionArchiveIdentity,
    ) -> None:
        raise AssertionError("retirement path does not verify archives")


def _archive_store_binding(store: BlockingArchiveStore) -> ArchiveStoreBinding:
    return replace(
        archive_store_binding(MemoryArchiveStore()),
        store=cast(ArchiveStore, store),
    )


@pytest.fixture
def database_url() -> Iterator[str]:
    value = os.getenv("RIVERHOG_TEST_POSTGRES_URL", "").strip()
    if not value:
        pytest.skip("RIVERHOG_TEST_POSTGRES_URL is required")
    engine = create_catalog_engine(value)
    Base.metadata.drop_all(engine)
    with engine.begin() as connection:
        connection.execute(text(f'DROP TABLE IF EXISTS "{STATE_VERSION_TABLE}"'))
    engine.dispose()
    initialize_db(value)
    try:
        yield value
    finally:
        engine = create_catalog_engine(value)
        Base.metadata.drop_all(engine)
        with engine.begin() as connection:
            connection.execute(text(f'DROP TABLE IF EXISTS "{STATE_VERSION_TABLE}"'))
        engine.dispose()


def _seed(database_url: str) -> None:
    factory = make_session_factory(database_url)
    with session_scope(factory) as session:
        session.add(
            TagRecord(
                id="docs",
                created_by_app="fixture",
                created_at="2026-01-01T00:00:00.000000Z",
                collection_count=1,
            )
        )
        session.add(
            CollectionRecord(
                id=COLLECTION_ID,
                creation_idempotency_key="fixture-docs",
                creation_identity_sha256="e" * 64,
                creation_custody_mode="producer-retained",
                content_identity="0" * 64,
                tag_set_identity=tag_set_identity(["docs"]),
                encryption_format="age-v1-scrypt",
                passphrase_id="fixture-archive-key-v1",
                inventory_identity="0" * 64,
                metadata_revision=1,
                metadata_updated_at="2026-01-01T00:00:00.000000Z",
                created_at="2026-01-01T00:00:00.000000Z",
            )
        )
        session.add(
            CollectionTagRecord(
                collection_id=COLLECTION_ID,
                tag_id="docs",
                assigned_by_app="fixture",
                assigned_at="2026-01-01T00:00:00.000000Z",
            )
        )
        session.add(
            CollectionFileRecord(
                collection_id=COLLECTION_ID,
                path=FILE_PATH,
                bytes=len(CONTENT),
                sha256=hashlib.sha256(CONTENT).hexdigest(),
            )
        )
        copy = CollectionArchiveCopyRecord(
            collection_id=COLLECTION_ID,
            store="deep",
            state="uploaded",
            archive_storage_prefix="archives/opaque-docs",
            last_uploaded_at="2026-07-18T00:00:00.000000Z",
            last_verified_at="2026-07-18T00:00:00.000000Z",
        )
        session.add(copy)
        for order, (object_id, kind, relative_path, stored_bytes) in enumerate(
            (
                ("segment-000000000000", "segment", "volumes/segment-000000000000.bin.age", 100),
                ("manifest", "manifest", "manifest.json.age", 20),
                ("recovery-descriptor", "recovery-descriptor", "recovery.json", 15),
            )
        ):
            copy.objects.append(
                CollectionArchiveObjectRecord(
                    collection_id=COLLECTION_ID,
                    store="deep",
                    object_id=object_id,
                    object_order=order,
                    kind=kind,
                    object_path=f"archives/opaque-docs/{relative_path}",
                    plaintext_bytes=stored_bytes - 1,
                    stored_bytes=stored_bytes,
                    sha256=chr(ord("a") + order) * 64,
                    stored_sha256="def0"[order] * 64,
                    uploaded_at="2026-07-18T00:00:00.000000Z",
                    verified_at="2026-07-18T00:00:00.000000Z",
                )
            )
        session.add(
            CollectionArchiveFileObjectRecord(
                collection_id=COLLECTION_ID,
                store="deep",
                path=FILE_PATH,
                sequence=0,
                object_id="segment-000000000000",
                file_offset=0,
                bytes=len(CONTENT),
            )
        )


def _seed_second_input(database_url: str) -> CollectionRootIdentity:
    with session_scope(make_session_factory(database_url)) as session:
        tag = session.get_one(TagRecord, "docs")
        tag.collection_count += 1
        session.add(
            CollectionRecord(
                id=SECOND_COLLECTION_ID,
                creation_idempotency_key="fixture-second",
                creation_identity_sha256="d" * 64,
                creation_custody_mode="producer-retained",
                content_identity="1" * 64,
                tag_set_identity=tag_set_identity(["docs"]),
                encryption_format="age-v1-scrypt",
                passphrase_id="fixture-archive-key-v1",
                inventory_identity="1" * 64,
                metadata_revision=1,
                metadata_updated_at="2026-01-01T00:00:00.000000Z",
                created_at="2026-01-01T00:00:00.000000Z",
            )
        )
        session.add(
            CollectionTagRecord(
                collection_id=SECOND_COLLECTION_ID,
                tag_id="docs",
                assigned_by_app="fixture",
                assigned_at="2026-01-01T00:00:00.000000Z",
            )
        )
        session.add(
            CollectionFileRecord(
                collection_id=SECOND_COLLECTION_ID,
                path=SECOND_FILE_PATH,
                bytes=len(SECOND_CONTENT),
                sha256=hashlib.sha256(SECOND_CONTENT).hexdigest(),
            )
        )
        copy = CollectionArchiveCopyRecord(
            collection_id=SECOND_COLLECTION_ID,
            store="deep",
            state="uploaded",
            archive_storage_prefix="archives/opaque-second",
            last_uploaded_at="2026-07-18T00:00:00.000000Z",
            last_verified_at="2026-07-18T00:00:00.000000Z",
        )
        session.add(copy)
        for order, (object_id, kind, relative_path) in enumerate(
            (
                ("segment-000000000000", "segment", "volumes/segment.bin.age"),
                ("manifest", "manifest", "manifest.json.age"),
                ("recovery-descriptor", "recovery-descriptor", "recovery.json"),
            )
        ):
            copy.objects.append(
                CollectionArchiveObjectRecord(
                    collection_id=SECOND_COLLECTION_ID,
                    store="deep",
                    object_id=object_id,
                    object_order=order,
                    kind=kind,
                    object_path=f"archives/opaque-second/{relative_path}",
                    plaintext_bytes=9,
                    stored_bytes=10,
                    sha256=("f" if object_id == "manifest" else "e") * 64,
                    stored_sha256=("9" if object_id == "manifest" else "8") * 64,
                    uploaded_at="2026-07-18T00:00:00.000000Z",
                    verified_at="2026-07-18T00:00:00.000000Z",
                )
            )
        session.add(
            CollectionArchiveFileObjectRecord(
                collection_id=SECOND_COLLECTION_ID,
                store="deep",
                path=SECOND_FILE_PATH,
                sequence=0,
                object_id="segment-000000000000",
                file_offset=0,
                bytes=len(SECOND_CONTENT),
            )
        )
    return CollectionRootIdentity(SECOND_COLLECTION_ID, "f" * 64, "1" * 64)


def _services(
    database_url: str,
) -> tuple[
    SqlAlchemyCollectionDeletionService,
    SqlAlchemyRetrievalService,
    BlockingArchiveStore,
]:
    config = RuntimeConfig(database_url=database_url)
    deep = replace(config.archive_store("archive"), name="deep")
    config = replace(
        config,
        archive_stores={"deep": deep},
        archive_write_store="deep",
        archive_read_order=("deep",),
    )
    store = BlockingArchiveStore()
    stores = ArchiveStoreRegistry({"deep": _archive_store_binding(store)})
    return (
        SqlAlchemyCollectionDeletionService(
            config,
            stores,
            None,
        ),
        SqlAlchemyRetrievalService(
            config,
            stores,
            None,
        ),
        store,
    )


def _workflow_claim(
    service: SqlAlchemyCollectionWorkflowService,
    *,
    work_id: str = WORK_ID,
) -> tuple[CollectionRootIdentity, dict[str, object]]:
    root = CollectionRootIdentity(COLLECTION_ID, "b" * 64, "0" * 64)
    work = {"format": "fixture-processing-work/v1", "work_id": work_id, "inputs": [root.as_dict()]}
    claim = service.create_or_resume_claim(
        work_id=work_id,
        work_document=work,
        work_document_sha256=canonical_json_sha256(work),
        principal=WORKFLOW_PRINCIPAL,
    )
    _stage_claim_inputs(service, claim, (root,))
    return root, claim


def _stage_claim_inputs(
    service: SqlAlchemyCollectionWorkflowService,
    claim: dict[str, object],
    inputs: Sequence[CollectionRootIdentity],
) -> None:
    claim_id = str(claim["id"])
    fence = int(cast(int, claim["fence"]))
    service.append_claim_inputs(
        claim_id,
        fence=fence,
        start_ordinal=0,
        inputs=tuple(inputs),
        principal=WORKFLOW_PRINCIPAL,
    )
    service.seal_claim_inputs(
        claim_id,
        fence=fence,
        principal=WORKFLOW_PRINCIPAL,
    )


def _issue_capability(
    service: SqlAlchemyCollectionWorkflowService,
    claim_id: str,
    *,
    audience: str,
    actions: Sequence[str],
    artifacts: Sequence[CollectionArtifactIdentity],
) -> dict[str, object]:
    capability = service.issue_capability(
        claim_id,
        fence=1,
        audience=audience,
        actions=tuple(actions),
        ttl_seconds=600,
        principal=WORKFLOW_PRINCIPAL,
    )
    capability_id = str(capability["id"])
    service.append_capability_artifacts(
        claim_id,
        capability_id,
        fence=1,
        start_ordinal=0,
        artifacts=tuple(sorted(artifacts)),
        principal=WORKFLOW_PRINCIPAL,
    )
    service.seal_capability_artifacts(
        claim_id,
        capability_id,
        fence=1,
        principal=WORKFLOW_PRINCIPAL,
    )
    return capability


def _seal_workflow_claim(
    service: SqlAlchemyCollectionWorkflowService,
    claim_id: str,
    *,
    retirement_policy: str = "retain",
    execution_id: str = EXECUTION_ID,
    input_artifacts: Sequence[CollectionArtifactIdentity] | None = None,
) -> dict[str, object]:
    root = CollectionRootIdentity(COLLECTION_ID, "b" * 64, "0" * 64)
    artifacts = tuple(sorted(input_artifacts or (_workflow_artifact(root),)))
    service.append_claim_artifacts(
        claim_id,
        fence=1,
        start_ordinal=0,
        artifacts=artifacts,
        principal=WORKFLOW_PRINCIPAL,
    )
    service.seal_claim_artifacts(claim_id, fence=1, principal=WORKFLOW_PRINCIPAL)
    controller_evidence: dict[str, JsonValue] = {
        "format": "stove0-controller-evidence/v1",
        "execution_envelope": {"execution_envelope_sha256": execution_id},
    }
    return service.seal_claim_plan(
        claim_id,
        fence=1,
        execution_id=execution_id,
        controller_evidence=controller_evidence,
        controller_evidence_sha256=canonical_json_sha256(controller_evidence),
        operation_id=OPERATION.id,
        operation_sha256=OPERATION.sha256,
        retirement_policy=retirement_policy,
        retirement_grace_seconds=0,
        principal=WORKFLOW_PRINCIPAL,
    )


def _upload_service(database_url: str) -> SqlAlchemyCollectionUploadService:
    base = RuntimeConfig(database_url=database_url, archive_scrypt_work_factor=1)
    deep = replace(base.archive_store("archive"), name="deep")
    config = replace(
        base,
        archive_stores={"deep": deep},
        archive_write_store="deep",
        archive_read_order=("deep",),
    )
    return SqlAlchemyCollectionUploadService(
        config,
        ArchiveStoreRegistry({"deep": archive_store_binding(MemoryArchiveStore())}),
    )


def _seal_disposition_set(
    service: SqlAlchemyCollectionWorkflowService,
    claim_id: str,
    dispositions: Sequence[ArtifactDisposition],
    outputs: Sequence[ArtifactDispositionOutput],
) -> ArtifactDispositionSetIdentity:
    service.record_dispositions(
        claim_id,
        fence=1,
        dispositions=tuple(dispositions),
        principal=WORKFLOW_PRINCIPAL,
    )
    service.record_disposition_outputs(
        claim_id,
        fence=1,
        outputs=tuple(outputs),
        principal=WORKFLOW_PRINCIPAL,
    )
    state = service.seal_disposition_set(
        claim_id,
        fence=1,
        principal=WORKFLOW_PRINCIPAL,
    )
    while state["state"] != "sealed":
        assert state["state"] == "sealing"
        assert service.process_due_disposition_sets() == 1
        state = service.get_disposition_set(claim_id, principal=WORKFLOW_PRINCIPAL)
    return ArtifactDispositionSetIdentity.from_mapping(cast(dict[str, object], state["identity"]))


def _settle_outcomes(
    service: SqlAlchemyCollectionWorkflowService,
    claim_id: str,
) -> dict[str, object]:
    settled = service.settle_claim_outcomes(
        claim_id,
        fence=1,
        retirement_policy="retain",
        retirement_grace_seconds=0,
        principal=WORKFLOW_PRINCIPAL,
    )
    while settled["state"] == "active":
        assert service.process_due_outcome_sets() == 1
        settled = service.settle_claim_outcomes(
            claim_id,
            fence=1,
            retirement_policy="retain",
            retirement_grace_seconds=0,
            principal=WORKFLOW_PRINCIPAL,
        )
    return settled


def _derivation_evidence_records(
    service: SqlAlchemyCollectionWorkflowService,
    claim_id: str,
    authority: ArtifactDispositionSetIdentity,
    *,
    collection_id: int,
) -> tuple[tuple[CollectionFileRecord, ...], int]:
    pages = (
        (
            "dispositions",
            service.list_dispositions(
                claim_id,
                authority_sha256=authority.sha256,
                start_ordinal=0,
                principal=WORKFLOW_PRINCIPAL,
            ),
        ),
        (
            "output-edges",
            service.list_disposition_outputs(
                claim_id,
                authority_sha256=authority.sha256,
                start_ordinal=0,
                principal=WORKFLOW_PRINCIPAL,
            ),
        ),
    )
    records: list[CollectionFileRecord] = []
    total_bytes = 0
    for kind, page in pages:
        content = canonical_json_bytes(page)
        total_bytes += len(content)
        records.append(
            CollectionFileRecord(
                collection_id=collection_id,
                path=derivation_evidence_page_path(
                    "dispositions" if kind == "dispositions" else "output-edges",
                    0,
                ),
                bytes=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
            )
        )
    return tuple(records), total_bytes


def _seed_derived_output(
    database_url: str,
    *,
    claim_id: str,
    root: CollectionRootIdentity,
    output_collection_id: int = 2,
    execution_id: str = EXECUTION_ID,
    output_path: str = "derived/document.txt",
) -> CollectionDerivation:
    controller_evidence: dict[str, JsonValue] = {
        "format": "stove0-controller-evidence/v1",
        "execution_envelope": {"execution_envelope_sha256": execution_id},
    }
    service = SqlAlchemyCollectionWorkflowService(RuntimeConfig(database_url=database_url))
    disposition = ArtifactDisposition(
        input_collection_id=COLLECTION_ID,
        input_archive_root_sha256=root.archive_root_sha256,
        input_path=FILE_PATH,
        status="transformed",
    )
    output = ArtifactDispositionOutput(
        input_collection_id=COLLECTION_ID,
        input_archive_root_sha256=root.archive_root_sha256,
        input_path=FILE_PATH,
        output_path=output_path,
    )
    disposition_set = _seal_disposition_set(service, claim_id, (disposition,), (output,))
    evidence_records, evidence_bytes = _derivation_evidence_records(
        service,
        claim_id,
        disposition_set,
        collection_id=output_collection_id,
    )
    claim = service.get_claim(claim_id, principal=WORKFLOW_PRINCIPAL)
    plan = cast(dict[str, object], claim["plan"])
    derivation = CollectionDerivation(
        execution_id=execution_id,
        claim_id=claim_id,
        fence=1,
        recipe=RecipeIdentity("fixture.recipe/v1", 1, "6" * 64),
        operation=OPERATION,
        input_set_sha256=str(cast(dict[str, object], plan["inputs"])["sha256"]),
        artifact_set_sha256=str(cast(dict[str, object], plan["artifacts"])["sha256"]),
        execution_envelope_sha256=execution_id,
        execution_sha256="5" * 64,
        controller_evidence=controller_evidence,
        controller_evidence_sha256=canonical_json_sha256(controller_evidence),
        disposition_set=disposition_set,
    )
    with session_scope(make_session_factory(database_url)) as session:
        tag = session.get_one(TagRecord, "docs")
        tag.collection_count += 1
        session.add(
            CollectionRecord(
                id=output_collection_id,
                creation_idempotency_key=execution_id,
                creation_identity_sha256=("c" if output_collection_id == 2 else "b") * 64,
                creation_custody_mode="producer-retained",
                content_identity=("4" if output_collection_id == 2 else "5") * 64,
                tag_set_identity=tag_set_identity(["docs"]),
                encryption_format="age-v1-scrypt",
                passphrase_id="fixture-archive-key-v1",
                inventory_identity=("3" if output_collection_id == 2 else "4") * 64,
                metadata_revision=1,
                metadata_updated_at="2026-01-01T00:00:00.000000Z",
                ingest_source=f"transform:{execution_id}",
                created_by_app=f"transform:{execution_id}",
                created_by_key_id=f"transform:{execution_id}",
                created_at="2026-01-01T00:00:00.000000Z",
                file_count=5,
                file_bytes=len(CONTENT) + len(derivation.to_json_bytes()) + 2 + evidence_bytes,
            )
        )
        session.add(
            CollectionTagRecord(
                collection_id=output_collection_id,
                tag_id="docs",
                assigned_by_app=f"transform:{execution_id}",
                assigned_at="2026-01-01T00:00:00.000000Z",
            )
        )
        session.add_all(
            (
                CollectionFileRecord(
                    collection_id=output_collection_id,
                    path=output_path,
                    bytes=len(CONTENT),
                    sha256="2" * 64,
                ),
                CollectionFileRecord(
                    collection_id=output_collection_id,
                    path=DERIVATION_EVIDENCE_PATH,
                    bytes=len(derivation.to_json_bytes()),
                    sha256=derivation.sha256,
                ),
                CollectionFileRecord(
                    collection_id=output_collection_id,
                    path=PRODUCER_EVIDENCE_PATH,
                    bytes=2,
                    sha256=hashlib.sha256(b"{}").hexdigest(),
                ),
                *evidence_records,
            )
        )
        session.add(
            CollectionArchiveCopyRecord(
                collection_id=output_collection_id,
                store="deep",
                state="uploaded",
                archive_storage_prefix=f"archives/derived-{output_collection_id}",
                last_uploaded_at="2026-01-01T00:00:00.000000Z",
                last_verified_at="2026-01-01T00:00:00.000000Z",
            )
        )
        session.add(
            CollectionArchiveObjectRecord(
                collection_id=output_collection_id,
                store="deep",
                object_id="manifest",
                object_order=0,
                kind="manifest",
                object_path=f"archives/derived-{output_collection_id}/manifest.json.age",
                plaintext_bytes=1,
                stored_bytes=2,
                sha256="1" * 64,
                stored_sha256="0" * 64,
                uploaded_at="2026-01-01T00:00:00.000000Z",
                verified_at="2026-01-01T00:00:00.000000Z",
            )
        )
    return derivation


def _seed_multi_input_derived_output(
    database_url: str,
    *,
    claim_id: str,
    roots: tuple[CollectionRootIdentity, CollectionRootIdentity],
) -> CollectionDerivation:
    controller_evidence: dict[str, JsonValue] = {
        "format": "stove0-controller-evidence/v1",
        "execution_envelope": {"execution_envelope_sha256": EXECUTION_ID},
    }
    service = SqlAlchemyCollectionWorkflowService(RuntimeConfig(database_url=database_url))
    dispositions = (
        ArtifactDisposition(
            input_collection_id=COLLECTION_ID,
            input_archive_root_sha256=roots[0].archive_root_sha256,
            input_path=FILE_PATH,
            status="transformed",
        ),
        ArtifactDisposition(
            input_collection_id=SECOND_COLLECTION_ID,
            input_archive_root_sha256=roots[1].archive_root_sha256,
            input_path=SECOND_FILE_PATH,
            status="transformed",
        ),
    )
    outputs = (
        ArtifactDispositionOutput(
            input_collection_id=COLLECTION_ID,
            input_archive_root_sha256=roots[0].archive_root_sha256,
            input_path=FILE_PATH,
            output_path="derived/document.txt",
        ),
        ArtifactDispositionOutput(
            input_collection_id=SECOND_COLLECTION_ID,
            input_archive_root_sha256=roots[1].archive_root_sha256,
            input_path=SECOND_FILE_PATH,
            output_path="derived/second.txt",
        ),
    )
    disposition_set = _seal_disposition_set(service, claim_id, dispositions, outputs)
    evidence_records, evidence_bytes = _derivation_evidence_records(
        service,
        claim_id,
        disposition_set,
        collection_id=2,
    )
    claim = service.get_claim(claim_id, principal=WORKFLOW_PRINCIPAL)
    plan = cast(dict[str, object], claim["plan"])
    derivation = CollectionDerivation(
        execution_id=EXECUTION_ID,
        claim_id=claim_id,
        fence=1,
        recipe=RecipeIdentity("fixture.multi-input/v1", 1, "6" * 64),
        operation=OPERATION,
        input_set_sha256=str(cast(dict[str, object], plan["inputs"])["sha256"]),
        artifact_set_sha256=str(cast(dict[str, object], plan["artifacts"])["sha256"]),
        execution_envelope_sha256=EXECUTION_ID,
        execution_sha256="5" * 64,
        controller_evidence=controller_evidence,
        controller_evidence_sha256=canonical_json_sha256(controller_evidence),
        disposition_set=disposition_set,
    )
    with session_scope(make_session_factory(database_url)) as session:
        tag = session.get_one(TagRecord, "docs")
        tag.collection_count += 1
        session.add(
            CollectionRecord(
                id=2,
                creation_idempotency_key=EXECUTION_ID,
                creation_identity_sha256="c" * 64,
                creation_custody_mode="producer-retained",
                content_identity="4" * 64,
                tag_set_identity=tag_set_identity(["docs"]),
                encryption_format="age-v1-scrypt",
                passphrase_id="fixture-archive-key-v1",
                inventory_identity="3" * 64,
                metadata_revision=1,
                metadata_updated_at="2026-01-01T00:00:00.000000Z",
                ingest_source=f"transform:{EXECUTION_ID}",
                created_by_app=f"transform:{EXECUTION_ID}",
                created_by_key_id=f"transform:{EXECUTION_ID}",
                created_at="2026-01-01T00:00:00.000000Z",
                file_count=6,
                file_bytes=(
                    len(CONTENT)
                    + len(SECOND_CONTENT)
                    + len(derivation.to_json_bytes())
                    + 2
                    + evidence_bytes
                ),
            )
        )
        session.add(
            CollectionTagRecord(
                collection_id=2,
                tag_id="docs",
                assigned_by_app=f"transform:{EXECUTION_ID}",
                assigned_at="2026-01-01T00:00:00.000000Z",
            )
        )
        session.add_all(
            (
                CollectionFileRecord(
                    collection_id=2,
                    path="derived/document.txt",
                    bytes=len(CONTENT),
                    sha256="2" * 64,
                ),
                CollectionFileRecord(
                    collection_id=2,
                    path="derived/second.txt",
                    bytes=len(SECOND_CONTENT),
                    sha256="7" * 64,
                ),
                CollectionFileRecord(
                    collection_id=2,
                    path=DERIVATION_EVIDENCE_PATH,
                    bytes=len(derivation.to_json_bytes()),
                    sha256=derivation.sha256,
                ),
                CollectionFileRecord(
                    collection_id=2,
                    path=PRODUCER_EVIDENCE_PATH,
                    bytes=2,
                    sha256=hashlib.sha256(b"{}").hexdigest(),
                ),
                *evidence_records,
            )
        )
        copy = CollectionArchiveCopyRecord(
            collection_id=2,
            store="deep",
            state="uploaded",
            archive_storage_prefix="archives/derived-multi",
            last_uploaded_at="2026-01-01T00:00:00.000000Z",
            last_verified_at="2026-01-01T00:00:00.000000Z",
        )
        session.add(copy)
        copy.objects.append(
            CollectionArchiveObjectRecord(
                collection_id=2,
                store="deep",
                object_id="manifest",
                object_order=0,
                kind="manifest",
                object_path="archives/derived-multi/manifest.json.age",
                plaintext_bytes=1,
                stored_bytes=2,
                sha256="1" * 64,
                stored_sha256="0" * 64,
                uploaded_at="2026-01-01T00:00:00.000000Z",
                verified_at="2026-01-01T00:00:00.000000Z",
            )
        )
    return derivation


def _seed_b2_copy(database_url: str) -> None:
    with session_scope(make_session_factory(database_url)) as session:
        deep = session.get(CollectionArchiveCopyRecord, (COLLECTION_ID, "deep"))
        assert deep is not None
        b2 = CollectionArchiveCopyRecord(
            collection_id=COLLECTION_ID,
            store="b2",
            state="uploaded",
            archive_storage_prefix="archives/b2-opaque-docs",
            last_uploaded_at=deep.last_uploaded_at,
            last_verified_at=deep.last_verified_at,
        )
        session.add(b2)
        for current in sorted(deep.objects, key=lambda item: item.object_order):
            copied = CollectionArchiveObjectRecord(
                collection_id=COLLECTION_ID,
                store="b2",
                object_id=current.object_id,
                object_order=current.object_order,
                kind=current.kind,
                object_path=current.object_path.replace(
                    "archives/opaque-docs",
                    "archives/b2-opaque-docs",
                ),
                plaintext_bytes=current.plaintext_bytes,
                stored_bytes=current.stored_bytes,
                sha256=current.sha256,
                stored_sha256=current.stored_sha256,
                uploaded_at=current.uploaded_at,
                verified_at=current.verified_at,
            )
            b2.objects.append(copied)
            for placement in current.placements:
                copied.placements.append(
                    CollectionArchiveFileObjectRecord(
                        collection_id=COLLECTION_ID,
                        store="b2",
                        path=placement.path,
                        sequence=placement.sequence,
                        object_id=current.object_id,
                        file_offset=placement.file_offset,
                        bytes=placement.bytes,
                        member=placement.member,
                    )
                )


def _create_retrieval(service: SqlAlchemyRetrievalService) -> dict[str, object]:
    files = [(COLLECTION_ID, FILE_PATH)]
    plan = service.plan(files)
    return service.create(
        app="local",
        plan_id=str(plan["id"]),
        plan_etag=str(plan["etag"]),
    )


def _drain_deletions(service: SqlAlchemyCollectionDeletionService) -> int:
    progressed = 0
    while current := service.process_due(limit=1):
        progressed += current
    return progressed


def test_active_retrieval_blocks_collection_deletion(database_url: str) -> None:
    _seed(database_url)
    deletion, retrieval, _store = _services(database_url)
    job = _create_retrieval(retrieval)

    blocked = deletion.plan(COLLECTION_ID)

    assert blocked["status"] == "blocked"
    assert blocked["challenge"] is None
    assert blocked["blockers"] == [f"retrieval job is active: {job['id']}"]
    retrieval.acknowledge(app="local", job_id=str(job["id"]))
    assert deletion.plan(COLLECTION_ID)["status"] == "ready"


def test_deletion_marker_rejects_retrieval_started_during_remote_delete(
    database_url: str,
) -> None:
    _seed(database_url)
    deletion, retrieval, store = _services(database_url)
    plan = deletion.plan(COLLECTION_ID)
    challenge = str(plan["challenge"])
    failures: list[BaseException] = []

    def delete_collection() -> None:
        try:
            deletion.delete(COLLECTION_ID, challenge=challenge, initiator=DELETER)
            deletion.process_due(limit=1)
        except BaseException as exc:  # pragma: no cover - asserted by the parent thread
            failures.append(exc)

    thread = threading.Thread(target=delete_collection)
    thread.start()
    assert store.delete_started.wait(10)
    try:
        with pytest.raises(NotFound, match="collection not found"):
            _create_retrieval(retrieval)
    finally:
        store.allow_delete.set()
        thread.join(10)

    assert not thread.is_alive()
    assert failures == []
    assert _drain_deletions(deletion) == 6
    assert store.deleted == [
        ("segment-000000000000",),
        ("manifest",),
        ("recovery-descriptor",),
    ]
    factory = make_session_factory(database_url)
    with session_scope(factory) as session:
        assert session.get(CollectionRecord, COLLECTION_ID) is None
        assert session.get(CollectionDeletionRecord, COLLECTION_ID) is None
        assert session.scalar(select(RetrievalJobRecord)) is None


def test_deletion_marker_rejects_processing_claim_started_during_remote_delete(
    database_url: str,
) -> None:
    _seed(database_url)
    deletion, _retrieval, store = _services(database_url)
    claim_service = SqlAlchemyCollectionWorkflowService(RuntimeConfig(database_url=database_url))
    root = CollectionRootIdentity(COLLECTION_ID, "b" * 64, "0" * 64)
    work = {"format": "stove0-work/v1", "inputs": [root.as_dict()]}
    work_id = canonical_json_sha256(work)
    challenge = str(deletion.plan(COLLECTION_ID)["challenge"])
    failures: list[BaseException] = []

    def delete_collection() -> None:
        try:
            deletion.delete(COLLECTION_ID, challenge=challenge, initiator=DELETER)
            deletion.process_due(limit=1)
        except BaseException as exc:  # pragma: no cover - asserted by parent thread
            failures.append(exc)

    thread = threading.Thread(target=delete_collection)
    thread.start()
    assert store.delete_started.wait(10)
    try:
        with pytest.raises(NotFound, match="finalized collection not found"):
            claim = claim_service.create_or_resume_claim(
                work_id=work_id,
                work_document=work,
                work_document_sha256=work_id,
                principal=ApplicationPrincipal(
                    app="stove0",
                    key_id="controller",
                    access=frozenset(),
                ),
            )
            _stage_claim_inputs(claim_service, claim, (root,))
    finally:
        store.allow_delete.set()
        thread.join(10)

    assert not thread.is_alive()
    assert failures == []


def test_postgres_claim_acquisition_renewal_restart_and_capability_revocation_converge(
    database_url: str,
) -> None:
    _seed(database_url)
    services = (
        SqlAlchemyCollectionWorkflowService(RuntimeConfig(database_url=database_url)),
        SqlAlchemyCollectionWorkflowService(RuntimeConfig(database_url=database_url)),
    )
    root = CollectionRootIdentity(COLLECTION_ID, "b" * 64, "0" * 64)
    work = {"format": "stove0-work/v1", "inputs": [root.as_dict()]}
    barrier = threading.Barrier(2)
    acquired: list[dict[str, object]] = []
    failures: list[BaseException] = []

    def acquire(service: SqlAlchemyCollectionWorkflowService) -> None:
        try:
            barrier.wait(5)
            acquired.append(
                service.create_or_resume_claim(
                    work_id=WORK_ID,
                    work_document=work,
                    work_document_sha256=canonical_json_sha256(work),
                    principal=WORKFLOW_PRINCIPAL,
                )
            )
        except BaseException as exc:  # pragma: no cover - asserted by parent thread
            failures.append(exc)

    threads = [threading.Thread(target=acquire, args=(service,)) for service in services]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert failures == []
    assert len(acquired) == 2
    assert acquired[0]["id"] == acquired[1]["id"]
    claim_id = str(acquired[0]["id"])
    _stage_claim_inputs(services[0], acquired[0], (root,))

    renewed = [
        service.renew_claim(
            claim_id,
            fence=1,
            lease_seconds=600,
            principal=WORKFLOW_PRINCIPAL,
        )
        for service in services
    ]
    assert {cast(int, item["fence"]) for item in renewed} == {1}
    capability = _issue_capability(
        services[0],
        claim_id,
        audience="fixture.observer/v1",
        actions=("read-inputs",),
        artifacts=(_workflow_artifact(root),),
    )
    assert services[1].authenticate_capability(str(capability["token"])) is not None

    barrier = threading.Barrier(2)
    restarted: list[dict[str, object]] = []
    stale: list[Conflict] = []

    def restart(service: SqlAlchemyCollectionWorkflowService) -> None:
        try:
            barrier.wait(5)
            restarted.append(
                service.restart_claim(
                    claim_id,
                    fence=1,
                    lease_seconds=600,
                    principal=WORKFLOW_PRINCIPAL,
                )
            )
        except Conflict as exc:
            stale.append(exc)

    threads = [threading.Thread(target=restart, args=(service,)) for service in services]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert len(restarted) == 1 and restarted[0]["fence"] == 2
    assert len(stale) == 1
    assert services[0].authenticate_capability(str(capability["token"])) is None


def test_postgres_exact_output_intent_creation_resumes_one_upload(
    database_url: str,
) -> None:
    _seed(database_url)
    workflows = SqlAlchemyCollectionWorkflowService(RuntimeConfig(database_url=database_url))
    root, claim = _workflow_claim(workflows)
    claim_id = str(claim["id"])
    _seal_workflow_claim(workflows, claim_id)
    capability = _issue_capability(
        workflows,
        claim_id,
        audience="fixture.target/v1",
        actions=("read-inputs", "write-output"),
        artifacts=(_workflow_artifact(root),),
    )
    transform = workflows.authenticate_capability(str(capability["token"]))
    assert transform is not None
    services = (_upload_service(database_url), _upload_service(database_url))
    barrier = threading.Barrier(2)
    uploads: list[dict[str, object]] = []
    failures: list[BaseException] = []

    def create(service: SqlAlchemyCollectionUploadService) -> None:
        try:
            barrier.wait(5)
            uploads.append(
                service.create_or_resume(
                    idempotency_key=EXECUTION_ID,
                    initial_tag=None,
                    tag_set_identity_sha256=tag_set_identity(()),
                    ingest_source=f"transform:{EXECUTION_ID}",
                    archive_store=None,
                    initiator=transform,
                    event_context=None,
                    provenance_mode="omitted",
                    provenance_omission_reason="PostgreSQL concurrency fixture.",
                )
            )
        except BaseException as exc:  # pragma: no cover - asserted by parent thread
            failures.append(exc)

    threads = [threading.Thread(target=create, args=(service,)) for service in services]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert failures == []
    assert len(uploads) == 2
    assert uploads[0]["collection_id"] == uploads[1]["collection_id"]
    assert uploads[0]["state"] == uploads[1]["state"] == "open"
    with session_scope(make_session_factory(database_url)) as session:
        rows = list(session.scalars(select(CollectionUploadRecord)))
        assert len(rows) == 1
        assert rows[0].idempotency_key == EXECUTION_ID
        assert rows[0].initiated_by_app == f"transform:{EXECUTION_ID}"


def test_postgres_settlement_replay_converges_on_one_derivation_record(
    database_url: str,
) -> None:
    _seed(database_url)
    services = (
        SqlAlchemyCollectionWorkflowService(RuntimeConfig(database_url=database_url)),
        SqlAlchemyCollectionWorkflowService(RuntimeConfig(database_url=database_url)),
    )
    root, claim = _workflow_claim(services[0])
    claim_id = str(claim["id"])
    _seal_workflow_claim(services[0], claim_id)
    derivation = _seed_derived_output(database_url, claim_id=claim_id, root=root)
    barrier = threading.Barrier(2)
    settlements: list[dict[str, object]] = []
    failures: list[BaseException] = []

    def settle(service: SqlAlchemyCollectionWorkflowService) -> None:
        try:
            barrier.wait(5)
            settlements.append(
                service.settle_claim(
                    claim_id,
                    fence=1,
                    output_collection_id=2,
                    derivation=derivation.as_dict(),
                    principal=WORKFLOW_PRINCIPAL,
                )
            )
        except BaseException as exc:  # pragma: no cover - asserted by parent thread
            failures.append(exc)

    threads = [threading.Thread(target=settle, args=(service,)) for service in services]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert failures == []
    assert len(settlements) == 2
    assert settlements[0]["state"] == settlements[1]["state"] == "settled"
    with session_scope(make_session_factory(database_url)) as session:
        derivations = list(session.scalars(select(CollectionDerivationRecord)))
        assert len(derivations) == 1
        assert derivations[0].document_sha256 == derivation.sha256


def test_postgres_concurrent_outcome_attachments_are_complete_and_exact(
    database_url: str,
) -> None:
    _seed(database_url)
    services = (
        SqlAlchemyCollectionWorkflowService(RuntimeConfig(database_url=database_url)),
        SqlAlchemyCollectionWorkflowService(RuntimeConfig(database_url=database_url)),
    )
    root = CollectionRootIdentity(COLLECTION_ID, "b" * 64, "0" * 64)
    parent_document = {
        "format": "fixture-multi-output-work/v1",
        "inputs": [root.as_dict()],
    }
    parent = services[0].create_or_resume_claim(
        work_id="a" * 64,
        work_document=parent_document,
        work_document_sha256=canonical_json_sha256(parent_document),
        principal=WORKFLOW_PRINCIPAL,
    )
    _stage_claim_inputs(services[0], parent, (root,))
    parent_id = str(parent["id"])
    children: list[tuple[str, int, CollectionDerivation, str]] = []
    for work_id, execution_id, output_id, outcome_id in (
        ("c" * 64, "1" * 64, 2, "first-output"),
        ("d" * 64, "2" * 64, 3, "second-output"),
    ):
        _, child = _workflow_claim(services[0], work_id=work_id)
        child_id = str(child["id"])
        _seal_workflow_claim(services[0], child_id, execution_id=execution_id)
        derivation = _seed_derived_output(
            database_url,
            claim_id=child_id,
            root=root,
            output_collection_id=output_id,
            execution_id=execution_id,
            output_path=f"derived/{outcome_id}.txt",
        )
        children.append((child_id, output_id, derivation, outcome_id))

    barrier = threading.Barrier(2)
    settlements: list[dict[str, object]] = []
    failures: list[BaseException] = []

    def attach(
        service: SqlAlchemyCollectionWorkflowService,
        child: tuple[str, int, CollectionDerivation, str],
    ) -> None:
        child_id, output_id, derivation, outcome_id = child
        try:
            barrier.wait(5)
            settlements.append(
                service.settle_claim(
                    child_id,
                    fence=1,
                    output_collection_id=output_id,
                    derivation=derivation.as_dict(),
                    outcome_claim_id=parent_id,
                    outcome_fence=1,
                    outcome_id=outcome_id,
                    principal=WORKFLOW_PRINCIPAL,
                )
            )
        except BaseException as exc:  # pragma: no cover - asserted by parent thread
            failures.append(exc)

    threads = [
        threading.Thread(target=attach, args=(service, child))
        for service, child in zip(services, children, strict=True)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert failures == []
    assert len(settlements) == 2
    settled = _settle_outcomes(services[1], parent_id)
    authority = cast(dict[str, object], cast(dict[str, object], settled["outcomes"])["authority"])
    page = services[0].list_claim_outcomes(
        parent_id,
        authority_sha256=str(authority["sha256"]),
        start_ordinal=0,
        principal=WORKFLOW_PRINCIPAL,
    )
    outcomes = tuple(
        CollectionProcessingOutcomeIdentity.from_mapping(item)
        for item in cast(list[dict[str, object]], page["outcomes"])
    )
    assert [item.outcome_id for item in outcomes] == ["first-output", "second-output"]
    assert settled["state"] == "settled"


def test_postgres_last_outcome_attachment_and_claim_closure_converge(
    database_url: str,
) -> None:
    _seed(database_url)
    services = (
        SqlAlchemyCollectionWorkflowService(RuntimeConfig(database_url=database_url)),
        SqlAlchemyCollectionWorkflowService(RuntimeConfig(database_url=database_url)),
    )
    root = CollectionRootIdentity(COLLECTION_ID, "b" * 64, "0" * 64)
    parent_document = {
        "format": "fixture-multi-output-work/v1",
        "inputs": [root.as_dict()],
    }
    parent = services[0].create_or_resume_claim(
        work_id="a" * 64,
        work_document=parent_document,
        work_document_sha256=canonical_json_sha256(parent_document),
        principal=WORKFLOW_PRINCIPAL,
    )
    _stage_claim_inputs(services[0], parent, (root,))
    parent_id = str(parent["id"])
    children: list[tuple[str, int, CollectionDerivation, str]] = []
    for work_id, execution_id, output_id, outcome_id in (
        ("c" * 64, "1" * 64, 2, "first-output"),
        ("d" * 64, "2" * 64, 3, "second-output"),
    ):
        _, child = _workflow_claim(services[0], work_id=work_id)
        child_id = str(child["id"])
        _seal_workflow_claim(services[0], child_id, execution_id=execution_id)
        children.append(
            (
                child_id,
                output_id,
                _seed_derived_output(
                    database_url,
                    claim_id=child_id,
                    root=root,
                    output_collection_id=output_id,
                    execution_id=execution_id,
                    output_path=f"derived/{outcome_id}.txt",
                ),
                outcome_id,
            )
        )

    first_id, first_output, first_derivation, first_outcome = children[0]
    services[0].settle_claim(
        first_id,
        fence=1,
        output_collection_id=first_output,
        derivation=first_derivation.as_dict(),
        outcome_claim_id=parent_id,
        outcome_fence=1,
        outcome_id=first_outcome,
        principal=WORKFLOW_PRINCIPAL,
    )
    expected = tuple(
        sorted(
            CollectionProcessingOutcomeIdentity(
                outcome_id=outcome_id,
                source_claim_id=child_id,
                output_collection=CollectionRootIdentity(
                    collection_id=output_id,
                    archive_root_sha256="1" * 64,
                    content_identity=("4" if output_id == 2 else "5") * 64,
                ),
                derivation_sha256=derivation.sha256,
            )
            for child_id, output_id, derivation, outcome_id in children
        )
    )
    barrier = threading.Barrier(2)
    attachment_failures: list[BaseException] = []
    closure_results: list[dict[str, object]] = []
    closure_conflicts: list[Conflict] = []

    def attach_last() -> None:
        child_id, output_id, derivation, outcome_id = children[1]
        try:
            barrier.wait(5)
            services[0].settle_claim(
                child_id,
                fence=1,
                output_collection_id=output_id,
                derivation=derivation.as_dict(),
                outcome_claim_id=parent_id,
                outcome_fence=1,
                outcome_id=outcome_id,
                principal=WORKFLOW_PRINCIPAL,
            )
        except BaseException as exc:  # pragma: no cover - asserted by parent thread
            attachment_failures.append(exc)

    def close_outcomes() -> None:
        try:
            barrier.wait(5)
            closure_results.append(
                services[1].settle_claim_outcomes(
                    parent_id,
                    fence=1,
                    retirement_policy="retain",
                    retirement_grace_seconds=0,
                    principal=WORKFLOW_PRINCIPAL,
                )
            )
        except Conflict as exc:
            closure_conflicts.append(exc)

    threads = [threading.Thread(target=attach_last), threading.Thread(target=close_outcomes)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert len(closure_results) + len(closure_conflicts) == 1
    assert len(attachment_failures) <= 1
    if attachment_failures:
        assert isinstance(attachment_failures[0], Conflict)
    settled = _settle_outcomes(services[1], parent_id)
    authority = cast(dict[str, object], cast(dict[str, object], settled["outcomes"])["authority"])
    page = services[0].list_claim_outcomes(
        parent_id,
        authority_sha256=str(authority["sha256"]),
        start_ordinal=0,
        principal=WORKFLOW_PRINCIPAL,
    )
    assert settled["state"] == "settled"
    actual = tuple(
        CollectionProcessingOutcomeIdentity.from_mapping(item)
        for item in cast(list[dict[str, object]], page["outcomes"])
    )
    if attachment_failures:
        assert actual == expected[:1]
    else:
        assert actual == expected


def test_postgres_multi_input_retirement_resumes_after_first_source_deletion(
    database_url: str,
) -> None:
    _seed(database_url)
    first_root = CollectionRootIdentity(COLLECTION_ID, "b" * 64, "0" * 64)
    second_root = _seed_second_input(database_url)
    roots = (first_root, second_root)
    workflows = SqlAlchemyCollectionWorkflowService(RuntimeConfig(database_url=database_url))
    work = {"format": "stove0-work/v1", "inputs": [root.as_dict() for root in roots]}
    claim = workflows.create_or_resume_claim(
        work_id=WORK_ID,
        work_document=work,
        work_document_sha256=canonical_json_sha256(work),
        principal=WORKFLOW_PRINCIPAL,
    )
    _stage_claim_inputs(workflows, claim, roots)
    claim_id = str(claim["id"])
    _seal_workflow_claim(
        workflows,
        claim_id,
        retirement_policy="retire-after-verified-output",
        input_artifacts=(
            _workflow_artifact(first_root),
            CollectionArtifactIdentity(
                collection=second_root,
                path=SECOND_FILE_PATH,
                bytes=len(SECOND_CONTENT),
                sha256=hashlib.sha256(SECOND_CONTENT).hexdigest(),
            ),
        ),
    )
    derivation = _seed_multi_input_derived_output(
        database_url,
        claim_id=claim_id,
        roots=roots,
    )
    settled = workflows.settle_claim(
        claim_id,
        fence=1,
        output_collection_id=2,
        derivation=derivation.as_dict(),
        principal=WORKFLOW_PRINCIPAL,
    )
    assert settled["state"] == "settled"
    assert (
        workflows.begin_retirement(
            claim_id,
            fence=1,
            principal=WORKFLOW_PRINCIPAL,
        )["state"]
        == "retiring"
    )

    base = RuntimeConfig(database_url=database_url)
    deep = replace(base.archive_store("archive"), name="deep")
    config = replace(
        base,
        archive_stores={"deep": deep},
        archive_write_store="deep",
        archive_read_order=("deep",),
    )
    store = RetirementArchiveStore()
    registry = ArchiveStoreRegistry(
        {
            "deep": replace(
                archive_store_binding(MemoryArchiveStore()),
                store=cast(ArchiveStore, store),
            )
        }
    )
    deletions = SqlAlchemyCollectionDeletionService(config, registry, None)
    first_plan = deletions.plan(
        COLLECTION_ID,
        principal=WORKFLOW_PRINCIPAL,
        retirement_claim_id=claim_id,
    )
    assert first_plan["status"] == "ready"
    assert (
        deletions.delete(
            COLLECTION_ID,
            challenge=str(first_plan["challenge"]),
            initiator=WORKFLOW_PRINCIPAL,
            retirement_claim_id=claim_id,
        )["status"]
        == "deleting"
    )
    assert _drain_deletions(deletions) > int(first_plan["archive_object_count"])

    # Reconstruct both authorities at the exact crash boundary where the first
    # immutable input is gone but the second remains under the same claim.
    restarted_workflows = SqlAlchemyCollectionWorkflowService(
        RuntimeConfig(database_url=database_url)
    )
    restarted_deletions = SqlAlchemyCollectionDeletionService(config, registry, None)
    resumed = restarted_workflows.get_claim(claim_id, principal=WORKFLOW_PRINCIPAL)
    assert resumed["state"] == "retiring"
    with pytest.raises(Conflict, match="still has live input collections"):
        restarted_workflows.release_claim(
            claim_id,
            fence=1,
            principal=WORKFLOW_PRINCIPAL,
        )
    second_plan = restarted_deletions.plan(
        SECOND_COLLECTION_ID,
        principal=WORKFLOW_PRINCIPAL,
        retirement_claim_id=claim_id,
    )
    assert second_plan["status"] == "ready"
    assert (
        restarted_deletions.delete(
            SECOND_COLLECTION_ID,
            challenge=str(second_plan["challenge"]),
            initiator=WORKFLOW_PRINCIPAL,
            retirement_claim_id=claim_id,
        )["status"]
        == "deleting"
    )
    assert _drain_deletions(restarted_deletions) > int(second_plan["archive_object_count"])
    released = restarted_workflows.release_claim(
        claim_id,
        fence=1,
        principal=WORKFLOW_PRINCIPAL,
    )
    assert released["state"] == "released"
    assert store.deleted_collections == [
        *([COLLECTION_ID] * int(first_plan["archive_object_count"])),
        *([SECOND_COLLECTION_ID] * int(second_plan["archive_object_count"])),
    ]
    with session_scope(make_session_factory(database_url)) as session:
        assert session.get(CollectionRecord, COLLECTION_ID) is None
        assert session.get(CollectionRecord, SECOND_COLLECTION_ID) is None
        assert session.get(CollectionRecord, 2) is not None


def test_metadata_publication_and_deletion_cannot_cross_collection_archive_operations(
    database_url: str,
) -> None:
    _seed(database_url)
    deletion, _retrieval, store = _services(database_url)
    factory = make_session_factory(database_url)
    with session_scope(factory) as session:
        session.add(
            CollectionMetadataPublicationRecord(
                collection_id=COLLECTION_ID,
                store="deep",
                desired_revision=1,
                state="pending",
                attempt_count=0,
                next_attempt_at="2026-01-01T00:00:00.000000Z",
            )
        )
    publisher = SqlAlchemyArchiveMaintenanceService(
        RuntimeConfig(database_url=database_url),
        ArchiveStoreRegistry({"deep": _archive_store_binding(store)}),
    )
    deletion_plan = deletion.plan(COLLECTION_ID)
    failures: list[BaseException] = []

    def publish_metadata() -> None:
        try:
            assert publisher.process_due_metadata_publications(limit=1) == 1
        except BaseException as exc:  # pragma: no cover - asserted by the parent thread
            failures.append(exc)

    thread = threading.Thread(target=publish_metadata)
    thread.start()
    assert store.metadata_started.wait(10)
    try:
        with pytest.raises(Conflict, match="plan changed"):
            deletion.delete(
                COLLECTION_ID,
                challenge=str(deletion_plan["challenge"]),
                initiator=DELETER,
            )
    finally:
        store.allow_metadata.set()
        thread.join(10)

    assert not thread.is_alive()
    assert failures == []
    assert len(store.published_metadata) == 1
    assert deletion.plan(COLLECTION_ID)["status"] == "ready"


def test_deletion_marker_prevents_a_due_metadata_publication_claim(
    database_url: str,
) -> None:
    _seed(database_url)
    deletion, _retrieval, store = _services(database_url)
    with session_scope(make_session_factory(database_url)) as session:
        session.add(
            CollectionMetadataPublicationRecord(
                collection_id=COLLECTION_ID,
                store="deep",
                desired_revision=1,
                state="pending",
                attempt_count=0,
                next_attempt_at="2026-01-01T00:00:00.000000Z",
            )
        )
    publisher = SqlAlchemyArchiveMaintenanceService(
        RuntimeConfig(database_url=database_url),
        ArchiveStoreRegistry({"deep": _archive_store_binding(store)}),
    )
    challenge = str(deletion.plan(COLLECTION_ID)["challenge"])
    failures: list[BaseException] = []

    def delete_collection() -> None:
        try:
            deletion.delete(COLLECTION_ID, challenge=challenge, initiator=DELETER)
            deletion.process_due(limit=1)
        except BaseException as exc:  # pragma: no cover - asserted by the parent thread
            failures.append(exc)

    thread = threading.Thread(target=delete_collection)
    thread.start()
    assert store.delete_started.wait(10)
    try:
        assert publisher.process_due_metadata_publications(limit=1) == 0
        assert store.published_metadata == []
    finally:
        store.allow_delete.set()
        thread.join(10)

    assert not thread.is_alive()
    assert failures == []


def test_retirement_marker_forces_retrieval_to_replan_onto_a_retained_copy(
    database_url: str,
) -> None:
    _seed(database_url)
    _seed_b2_copy(database_url)
    base = RuntimeConfig(database_url=database_url)
    archive = base.archive_store("archive")
    b2_config = replace(
        archive,
        name="b2",
    )
    config = replace(
        base,
        archive_stores={"deep": replace(archive, name="deep"), "b2": b2_config},
        archive_write_store="deep",
        archive_read_order=("deep", "b2"),
    )
    deep = BlockingArchiveStore()
    b2 = BlockingArchiveStore()
    b2.allow_delete.set()
    stores = ArchiveStoreRegistry(
        {
            "deep": _archive_store_binding(deep),
            "b2": _archive_store_binding(b2),
        }
    )
    retirement = SqlAlchemyArchiveCopyRetirementService(config, stores)
    retrieval = SqlAlchemyRetrievalService(
        config,
        stores,
        None,
    )
    files = [(COLLECTION_ID, FILE_PATH)]
    challenge = str(retirement.plan(COLLECTION_ID, store="deep")["challenge"])
    failures: list[BaseException] = []

    def retire_copy() -> None:
        try:
            retirement.retire(COLLECTION_ID, store="deep", challenge=challenge)
        except BaseException as exc:  # pragma: no cover - asserted by the parent thread
            failures.append(exc)

    thread = threading.Thread(target=retire_copy)
    thread.start()
    assert deep.delete_started.wait(10)
    try:
        current_plan = retrieval.plan(files)
        with session_scope(make_session_factory(database_url)) as session:
            current_stores = set(
                session.scalars(
                    select(RetrievalPlanObjectRecord.source_store).where(
                        RetrievalPlanObjectRecord.plan_id == str(current_plan["id"])
                    )
                )
            )
        assert current_stores == {"b2"}
        current_job = retrieval.create(
            app="local",
            plan_id=str(current_plan["id"]),
            plan_etag=str(current_plan["etag"]),
        )
        assert current_job["state"] == "ready"
    finally:
        deep.allow_delete.set()
        thread.join(10)

    assert not thread.is_alive()
    assert failures == []
