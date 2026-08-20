from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, cast

import pytest
from pytest import FixtureRequest
from riverhog_core.app_permissions import ApplicationPrincipal
from riverhog_core.catalog_base import Base
from riverhog_core.catalog_db import SessionFactory
from riverhog_core.catalog_models import (
    CollectionArchiveCopyRecord,
    CollectionArchiveObjectRecord,
    CollectionFileRecord,
    CollectionRecord,
    CollectionTagRecord,
    CollectionUploadRecord,
    TagRecord,
)
from riverhog_core.catalog_workflow_models import CollectionProcessingClaimRecord
from riverhog_core.services.collection_workflows import (
    SqlAlchemyCollectionWorkflowService,
    processing_claim_blockers,
)
from riverhog_protocol.collection_workflows import (
    DERIVATION_EVIDENCE_PATH,
    ArtifactDisposition,
    CollectionDerivation,
    CollectionRootIdentity,
    OperationIdentity,
    RecipeIdentity,
    canonical_json_bytes,
)
from riverhog_protocol.errors import Conflict
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

NOW = "2026-08-15T00:00:00Z"
WORK_ID = "b" * 64
EXECUTION_ID = "d" * 64
CONTROLLER_EVIDENCE = {
    "format": "stove0-controller-evidence/v1",
    "execution_envelope": {"execution_envelope_sha256": EXECUTION_ID},
}
CONTROLLER_EVIDENCE_SHA256 = hashlib.sha256(canonical_json_bytes(CONTROLLER_EVIDENCE)).hexdigest()


def _session_factory(tmp_path: Path, request: FixtureRequest) -> SessionFactory:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'state.sqlite3'}")
    request.addfinalizer(engine.dispose)
    Base.metadata.create_all(engine)
    return cast(SessionFactory, sessionmaker(engine, expire_on_commit=False))


def _collection(
    session: Any,
    collection_id: int,
    *,
    creator: str,
    tag: str,
    root: str,
    idempotency_key: str | None = None,
) -> None:
    collection = CollectionRecord(
        id=collection_id,
        creation_idempotency_key=idempotency_key or f"collection-{collection_id}",
        content_etag=str(collection_id) * 64,
        provenance_mode="omitted",
        provenance_etag=None,
        record_etag="f" * 64,
        metadata_revision=1,
        metadata_updated_at=NOW,
        ingest_source="fixture",
        created_by_app=creator,
        created_by_key_id=None,
        created_at=NOW,
    )
    session.add(collection)
    session.flush()
    session.add(
        CollectionTagRecord(
            collection_id=collection_id,
            tag_id=tag,
            assigned_by_app=creator,
            assigned_by_key_id=None,
            assigned_at=NOW,
        )
    )
    if collection_id == 1:
        session.add(
            CollectionFileRecord(
                collection_id=collection_id,
                path="camera/input.mov",
                bytes=4,
                sha256="8" * 64,
            )
        )
    session.add(
        CollectionArchiveCopyRecord(
            collection_id=collection_id,
            store="hot",
            state="uploaded",
            archive_storage_prefix=f"collections/{collection_id}",
            backend="s3",
            storage_class="STANDARD",
            last_uploaded_at=NOW,
            last_verified_at=NOW,
            failure=None,
        )
    )
    session.flush()
    session.add(
        CollectionArchiveObjectRecord(
            collection_id=collection_id,
            store="hot",
            object_id="manifest",
            object_order=1,
            kind="manifest",
            object_path=f"collections/{collection_id}/manifest.json.age",
            plaintext_bytes=1,
            stored_bytes=2,
            sha256=root,
            stored_sha256="9" * 64,
            version_id=None,
            age_state_json=None,
            part_receipts_json=None,
            plan_sha256=None,
            index_sha256=None,
            backend="s3",
            storage_class="STANDARD",
            uploaded_at=NOW,
            verified_at=NOW,
        )
    )


def _principal() -> ApplicationPrincipal:
    return ApplicationPrincipal(app="stove0", key_id="stove0-key", access=frozenset())


def _setup(factory: SessionFactory) -> CollectionRootIdentity:
    with factory() as session, session.begin():
        session.add_all(
            [
                TagRecord(
                    id="intake-camera",
                    created_by_app="test",
                    created_by_key_id=None,
                    created_at=NOW,
                ),
                TagRecord(
                    id="archive-camera",
                    created_by_app="test",
                    created_by_key_id=None,
                    created_at=NOW,
                ),
            ]
        )
        _collection(session, 1, creator="ftp", tag="intake-camera", root="a" * 64)
    return CollectionRootIdentity(1, "a" * 64, "1" * 64)


def _work_document(root: CollectionRootIdentity) -> dict[str, object]:
    return {
        "format": "stove0-work/v1",
        "work_id": WORK_ID,
        "inputs": [root.as_dict()],
    }


def test_claim_plan_capabilities_settlement_and_deletion_blocker(
    tmp_path: Path,
    request: FixtureRequest,
) -> None:
    factory = _session_factory(tmp_path, request)
    root = _setup(factory)
    service = SqlAlchemyCollectionWorkflowService(
        cast(Any, object()),
        session_factory=factory,
    )
    work = _work_document(root)
    work_sha256 = hashlib.sha256(canonical_json_bytes(work)).hexdigest()
    claim = service.create_or_resume_claim(
        work_id=WORK_ID,
        work_document=work,
        work_document_sha256=work_sha256,
        inputs=(root,),
        principal=_principal(),
    )
    claim_id = str(claim["id"])

    observer_capability = service.issue_capability(
        claim_id,
        fence=1,
        audience="fixture.observer/v1",
        actions=("read-inputs",),
        ttl_seconds=600,
        principal=_principal(),
    )
    observer = service.authenticate_capability(str(observer_capability["token"]))
    assert observer_capability["audience"] == "fixture.observer/v1"
    assert observer is not None
    assert observer.app == f"claim:{claim_id}"
    assert observer.key_id == "stove0-key"
    with pytest.raises(Conflict, match="sealed execution plan"):
        service.issue_capability(
            claim_id,
            fence=1,
            audience="fixture.target/v1",
            actions=("write-output",),
            ttl_seconds=600,
            principal=_principal(),
        )

    sealed = service.seal_claim_plan(
        claim_id,
        fence=1,
        execution_id=EXECUTION_ID,
        controller_evidence=CONTROLLER_EVIDENCE,
        controller_evidence_sha256=CONTROLLER_EVIDENCE_SHA256,
        operation_id="archive-video/v1",
        operation_sha256="c" * 64,
        output_tags=("archive-camera",),
        retirement_policy="retire-after-verified-output",
        retirement_grace_seconds=0,
        principal=_principal(),
    )
    assert sealed["plan"]["execution_id"] == EXECUTION_ID  # type: ignore[index]
    assert service.authenticate_capability(str(observer_capability["token"])) is None

    first = service.issue_capability(
        claim_id,
        fence=1,
        audience="fixture.target/v1",
        actions=("read-inputs", "write-output"),
        ttl_seconds=600,
        principal=_principal(),
    )
    second = service.issue_capability(
        claim_id,
        fence=1,
        audience="fixture.target/v1",
        actions=("read-inputs", "write-output"),
        ttl_seconds=600,
        principal=_principal(),
    )
    first_principal = service.authenticate_capability(str(first["token"]))
    second_principal = service.authenticate_capability(str(second["token"]))
    assert first_principal is not None and second_principal is not None
    assert first_principal.app == second_principal.app == f"transform:{EXECUTION_ID}"
    assert first_principal.key_id == second_principal.key_id == "stove0-key"

    derivation = CollectionDerivation(
        execution_id=EXECUTION_ID,
        claim_id=claim_id,
        fence=1,
        recipe=RecipeIdentity("camera/v1", 1, "b" * 64),
        operation=OperationIdentity("archive-video/v1", "c" * 64),
        inputs=(root,),
        output_tags=("archive-camera",),
        execution_envelope_sha256=EXECUTION_ID,
        execution_sha256="e" * 64,
        controller_evidence=CONTROLLER_EVIDENCE,
        controller_evidence_sha256=CONTROLLER_EVIDENCE_SHA256,
        dispositions=(
            ArtifactDisposition(
                input_collection_id=1,
                input_manifest_sha256="a" * 64,
                input_path="camera/input.mov",
                status="transformed",
                outputs=("video/output.mkv",),
            ),
        ),
    )
    with factory() as session, session.begin():
        _collection(
            session,
            2,
            creator=f"transform:{EXECUTION_ID}",
            tag="archive-camera",
            root="6" * 64,
            idempotency_key=EXECUTION_ID,
        )
        session.add_all(
            [
                CollectionFileRecord(
                    collection_id=2,
                    path="video/output.mkv",
                    bytes=4,
                    sha256="7" * 64,
                ),
                CollectionFileRecord(
                    collection_id=2,
                    path=DERIVATION_EVIDENCE_PATH,
                    bytes=len(derivation.to_json_bytes()),
                    sha256=derivation.sha256,
                ),
            ]
        )

    with factory() as session, session.begin():
        stored = session.get(CollectionProcessingClaimRecord, claim_id)
        assert stored is not None
        stored.expires_at = NOW

    settled = service.settle_claim(
        claim_id,
        fence=1,
        output_collection_id=2,
        derivation=derivation.as_dict(),
        principal=_principal(),
    )
    assert settled["state"] == "settled"
    replayed = service.settle_claim(
        claim_id,
        fence=1,
        output_collection_id=2,
        derivation=derivation.as_dict(),
        principal=_principal(),
    )
    assert replayed["state"] == "settled"
    changed_derivation = derivation.as_dict()
    changed_derivation["execution_sha256"] = "f" * 64
    with pytest.raises(Conflict, match="different derivation evidence"):
        service.settle_claim(
            claim_id,
            fence=1,
            output_collection_id=2,
            derivation=changed_derivation,
            principal=_principal(),
        )
    retiring = service.begin_retirement(
        claim_id,
        fence=1,
        principal=_principal(),
    )
    assert retiring["state"] == "retiring"
    replayed_retiring = service.settle_claim(
        claim_id,
        fence=1,
        output_collection_id=2,
        derivation=derivation.as_dict(),
        principal=_principal(),
    )
    assert replayed_retiring["state"] == "retiring"
    assert service.authenticate_capability(str(first["token"])) is None
    with factory() as session:
        blockers = processing_claim_blockers(session, 1)
    assert blockers and claim_id in blockers[0]


def test_claim_fails_closed_on_changed_root_or_reused_work_document(
    tmp_path: Path,
    request: FixtureRequest,
) -> None:
    factory = _session_factory(tmp_path, request)
    root = _setup(factory)
    service = SqlAlchemyCollectionWorkflowService(
        cast(Any, object()),
        session_factory=factory,
    )
    work = _work_document(root)
    digest = hashlib.sha256(canonical_json_bytes(work)).hexdigest()
    service.create_or_resume_claim(
        work_id=WORK_ID,
        work_document=work,
        work_document_sha256=digest,
        inputs=(root,),
        principal=_principal(),
    )
    changed = {**work, "extra": True}
    with pytest.raises(Conflict, match="another request"):
        service.create_or_resume_claim(
            work_id=WORK_ID,
            work_document=changed,
            work_document_sha256=hashlib.sha256(canonical_json_bytes(changed)).hexdigest(),
            inputs=(root,),
            principal=_principal(),
        )
    with pytest.raises(Conflict, match="root differs"):
        service.create_or_resume_claim(
            work_id="f" * 64,
            work_document={"format": "test"},
            work_document_sha256=hashlib.sha256(b'{"format":"test"}').hexdigest(),
            inputs=(CollectionRootIdentity(1, "9" * 64, "1" * 64),),
            principal=_principal(),
        )


def test_fenced_restart_advances_generation_revokes_capabilities_and_clears_plan(
    tmp_path: Path,
    request: FixtureRequest,
) -> None:
    factory = _session_factory(tmp_path, request)
    root = _setup(factory)
    service = SqlAlchemyCollectionWorkflowService(
        cast(Any, object()),
        session_factory=factory,
    )
    work = _work_document(root)
    claim = service.create_or_resume_claim(
        work_id=WORK_ID,
        work_document=work,
        work_document_sha256=hashlib.sha256(canonical_json_bytes(work)).hexdigest(),
        inputs=(root,),
        principal=_principal(),
    )
    claim_id = str(claim["id"])
    service.seal_claim_plan(
        claim_id,
        fence=1,
        execution_id=EXECUTION_ID,
        controller_evidence=CONTROLLER_EVIDENCE,
        controller_evidence_sha256=CONTROLLER_EVIDENCE_SHA256,
        operation_id="archive-video/v1",
        operation_sha256="c" * 64,
        output_tags=("archive-camera",),
        retirement_policy="retain",
        retirement_grace_seconds=0,
        principal=_principal(),
    )
    capability = service.issue_capability(
        claim_id,
        fence=1,
        audience="fixture.target/v1",
        actions=("read-inputs", "write-output"),
        ttl_seconds=600,
        principal=_principal(),
    )
    assert service.authenticate_capability(str(capability["token"])) is not None

    restarted = service.restart_claim(
        claim_id,
        fence=1,
        lease_seconds=600,
        principal=_principal(),
    )

    assert restarted["state"] == "active"
    assert restarted["fence"] == 2
    assert restarted["plan"] is None
    assert service.authenticate_capability(str(capability["token"])) is None
    with pytest.raises(Conflict, match="fence is stale"):
        service.restart_claim(
            claim_id,
            fence=1,
            lease_seconds=600,
            principal=_principal(),
        )


def test_fenced_restart_refuses_an_existing_execution_output(
    tmp_path: Path,
    request: FixtureRequest,
) -> None:
    factory = _session_factory(tmp_path, request)
    root = _setup(factory)
    service = SqlAlchemyCollectionWorkflowService(
        cast(Any, object()),
        session_factory=factory,
    )
    work = _work_document(root)
    claim = service.create_or_resume_claim(
        work_id=WORK_ID,
        work_document=work,
        work_document_sha256=hashlib.sha256(canonical_json_bytes(work)).hexdigest(),
        inputs=(root,),
        principal=_principal(),
    )
    claim_id = str(claim["id"])
    service.seal_claim_plan(
        claim_id,
        fence=1,
        execution_id=EXECUTION_ID,
        controller_evidence=CONTROLLER_EVIDENCE,
        controller_evidence_sha256=CONTROLLER_EVIDENCE_SHA256,
        operation_id="archive-video/v1",
        operation_sha256="c" * 64,
        output_tags=("archive-camera",),
        retirement_policy="retain",
        retirement_grace_seconds=0,
        principal=_principal(),
    )
    with factory() as session, session.begin():
        _collection(
            session,
            2,
            creator=f"transform:{EXECUTION_ID}",
            tag="archive-camera",
            root="6" * 64,
            idempotency_key=EXECUTION_ID,
        )

    with pytest.raises(Conflict, match="owns an output collection or upload"):
        service.restart_claim(
            claim_id,
            fence=1,
            lease_seconds=600,
            principal=_principal(),
        )

    with factory() as session, session.begin():
        stored = session.get(CollectionProcessingClaimRecord, claim_id)
        assert stored is not None
        stored.expires_at = NOW
    with factory() as session:
        blockers = processing_claim_blockers(session, 1)
    assert blockers and claim_id in blockers[0]
    with pytest.raises(Conflict, match="owns an output collection or upload"):
        service.create_or_resume_claim(
            work_id=WORK_ID,
            work_document=work,
            work_document_sha256=hashlib.sha256(canonical_json_bytes(work)).hexdigest(),
            inputs=(root,),
            principal=_principal(),
        )


def test_expired_execution_upload_remains_a_deletion_blocker(
    tmp_path: Path,
    request: FixtureRequest,
) -> None:
    factory = _session_factory(tmp_path, request)
    root = _setup(factory)
    service = SqlAlchemyCollectionWorkflowService(
        cast(Any, object()),
        session_factory=factory,
    )
    work = _work_document(root)
    claim = service.create_or_resume_claim(
        work_id=WORK_ID,
        work_document=work,
        work_document_sha256=hashlib.sha256(canonical_json_bytes(work)).hexdigest(),
        inputs=(root,),
        principal=_principal(),
    )
    claim_id = str(claim["id"])
    service.seal_claim_plan(
        claim_id,
        fence=1,
        execution_id=EXECUTION_ID,
        controller_evidence=CONTROLLER_EVIDENCE,
        controller_evidence_sha256=CONTROLLER_EVIDENCE_SHA256,
        operation_id="archive-video/v1",
        operation_sha256="c" * 64,
        output_tags=("archive-camera",),
        retirement_policy="retain",
        retirement_grace_seconds=0,
        principal=_principal(),
    )
    with factory() as session, session.begin():
        session.add(
            CollectionUploadRecord(
                collection_id=3,
                idempotency_key=EXECUTION_ID,
                ingest_source=f"transform:{EXECUTION_ID}",
                provenance_mode="omitted",
                provenance_omission_reason="fixture",
                provenance_etag=None,
                initiated_by_app=f"transform:{EXECUTION_ID}",
                initiated_by_key_id=f"transform:{EXECUTION_ID}",
                event_context_json=None,
                state="open",
                archive_store="hot",
                opened_at=NOW,
                last_activity_at=NOW,
                closed_at=None,
                archive_phase="planning",
                archive_phase_updated_at=NOW,
                archive_attempt_count=0,
                archive_next_attempt_at=None,
                archive_last_attempt_at=None,
                archive_failure=None,
                archive_storage_prefix="collections/3",
                collection_manifest_bytes_b64=None,
                collection_manifest_proof_bytes_b64=None,
                planner_checkpoint_json="{}",
            )
        )
        stored = session.get(CollectionProcessingClaimRecord, claim_id)
        assert stored is not None
        stored.expires_at = NOW

    with factory() as session:
        blockers = processing_claim_blockers(session, 1)
    assert blockers and claim_id in blockers[0]
    with pytest.raises(Conflict, match="owns an output collection or upload"):
        service.create_or_resume_claim(
            work_id=WORK_ID,
            work_document=work,
            work_document_sha256=hashlib.sha256(canonical_json_bytes(work)).hexdigest(),
            inputs=(root,),
            principal=_principal(),
        )
    with pytest.raises(Conflict, match="owns an output collection or upload"):
        service.abandon_claim(
            claim_id,
            fence=1,
            reason="failed: output upload requires reconciliation",
            principal=_principal(),
        )

    renewed = service.renew_claim(
        claim_id,
        fence=1,
        lease_seconds=600,
        principal=_principal(),
    )
    assert renewed["fence"] == 1
    capability = service.issue_capability(
        claim_id,
        fence=1,
        audience="fixture.target/v1",
        actions=("read-inputs", "write-output"),
        ttl_seconds=600,
        principal=_principal(),
    )
    assert service.authenticate_capability(str(capability["token"])) is not None


def test_fenced_abandonment_revokes_capabilities_and_unblocks_deletion(
    tmp_path: Path,
    request: FixtureRequest,
) -> None:
    factory = _session_factory(tmp_path, request)
    root = _setup(factory)
    service = SqlAlchemyCollectionWorkflowService(
        cast(Any, object()),
        session_factory=factory,
    )
    work = _work_document(root)
    claim = service.create_or_resume_claim(
        work_id=WORK_ID,
        work_document=work,
        work_document_sha256=hashlib.sha256(canonical_json_bytes(work)).hexdigest(),
        inputs=(root,),
        principal=_principal(),
    )
    claim_id = str(claim["id"])
    capability = service.issue_capability(
        claim_id,
        fence=1,
        audience="fixture.observer/v1",
        actions=("read-inputs",),
        ttl_seconds=600,
        principal=_principal(),
    )
    assert service.authenticate_capability(str(capability["token"])) is not None
    with factory() as session:
        assert processing_claim_blockers(session, 1)

    abandoned = service.abandon_claim(
        claim_id,
        fence=1,
        reason="canceled: operator requested cancellation",
        principal=_principal(),
    )
    repeated = service.abandon_claim(
        claim_id,
        fence=1,
        reason="canceled: operator requested cancellation",
        principal=_principal(),
    )

    assert abandoned["state"] == "abandoned"
    assert isinstance(abandoned["abandoned_at"], str)
    assert abandoned["abandonment_reason"] == "canceled: operator requested cancellation"
    assert repeated == abandoned
    assert service.authenticate_capability(str(capability["token"])) is None
    with factory() as session:
        assert processing_claim_blockers(session, 1) == []
    with pytest.raises(Conflict, match="fence is stale"):
        service.abandon_claim(
            claim_id,
            fence=2,
            reason="canceled: operator requested cancellation",
            principal=_principal(),
        )
    with pytest.raises(Conflict, match="another reason"):
        service.abandon_claim(
            claim_id,
            fence=1,
            reason="failed: a different terminal outcome",
            principal=_principal(),
        )


def test_expired_claim_abandonment_is_fenced_against_a_restarted_generation(
    tmp_path: Path,
    request: FixtureRequest,
) -> None:
    factory = _session_factory(tmp_path, request)
    root = _setup(factory)
    service = SqlAlchemyCollectionWorkflowService(
        cast(Any, object()),
        session_factory=factory,
    )
    work = _work_document(root)
    digest = hashlib.sha256(canonical_json_bytes(work)).hexdigest()
    claim = service.create_or_resume_claim(
        work_id=WORK_ID,
        work_document=work,
        work_document_sha256=digest,
        inputs=(root,),
        principal=_principal(),
    )
    claim_id = str(claim["id"])
    with factory() as session, session.begin():
        stored = session.get(CollectionProcessingClaimRecord, claim_id)
        assert stored is not None
        stored.expires_at = NOW

    restarted = service.create_or_resume_claim(
        work_id=WORK_ID,
        work_document=work,
        work_document_sha256=digest,
        inputs=(root,),
        principal=_principal(),
    )
    assert restarted["fence"] == 2
    with pytest.raises(Conflict, match="fence is stale"):
        service.abandon_claim(
            claim_id,
            fence=1,
            reason="canceled: stale worker",
            principal=_principal(),
        )


def test_expired_current_generation_can_reconcile_terminal_abandonment(
    tmp_path: Path,
    request: FixtureRequest,
) -> None:
    factory = _session_factory(tmp_path, request)
    root = _setup(factory)
    service = SqlAlchemyCollectionWorkflowService(
        cast(Any, object()),
        session_factory=factory,
    )
    work = _work_document(root)
    claim = service.create_or_resume_claim(
        work_id=WORK_ID,
        work_document=work,
        work_document_sha256=hashlib.sha256(canonical_json_bytes(work)).hexdigest(),
        inputs=(root,),
        principal=_principal(),
    )
    claim_id = str(claim["id"])
    with factory() as session, session.begin():
        stored = session.get(CollectionProcessingClaimRecord, claim_id)
        assert stored is not None
        stored.expires_at = NOW
    with factory() as session:
        assert processing_claim_blockers(session, 1) == []
    with pytest.raises(Conflict, match="not renewable"):
        service.renew_claim(
            claim_id,
            fence=1,
            lease_seconds=600,
            principal=_principal(),
        )

    abandoned = service.abandon_claim(
        claim_id,
        fence=1,
        reason="canceled: controller reconciliation after lease expiry",
        principal=_principal(),
    )
    assert abandoned["state"] == "abandoned"
