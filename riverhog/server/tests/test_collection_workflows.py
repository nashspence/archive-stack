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
)
from riverhog_protocol.errors import Conflict
from riverhog_protocol.paths import tag_set_identity
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
        creation_identity_sha256=f"{collection_id:064x}",
        creation_custody_mode="producer-retained",
        content_identity=str(collection_id) * 64,
        tag_set_identity=tag_set_identity((tag,)),
        encryption_format="age-v1-scrypt",
        passphrase_id="fixture-archive-key-v1",
        provenance_mode="omitted",
        provenance_identity=None,
        inventory_identity="f" * 64,
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
            revision=None,
            age_state_json=None,
            archive_parts_json=None,
            plan_sha256=None,
            index_sha256=None,
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


def _artifact(root: CollectionRootIdentity) -> CollectionArtifactIdentity:
    return CollectionArtifactIdentity(
        collection=root,
        path="camera/input.mov",
        bytes=4,
        sha256="8" * 64,
    )


def _create_claim(
    service: SqlAlchemyCollectionWorkflowService,
    *,
    work_id: str,
    work_document: dict[str, object],
    root: CollectionRootIdentity,
    artifact: CollectionArtifactIdentity | None = None,
    purpose: str = "collection-work/v1",
) -> dict[str, object]:
    claim = service.create_or_resume_claim(
        work_id=work_id,
        work_document=work_document,
        work_document_sha256=hashlib.sha256(canonical_json_bytes(work_document)).hexdigest(),
        purpose=purpose,
        principal=_principal(),
    )
    claim_id = str(claim["id"])
    fence = int(claim["fence"])
    service.append_claim_inputs(
        claim_id,
        fence=fence,
        start_ordinal=0,
        inputs=(root,),
        principal=_principal(),
    )
    inputs = service.seal_claim_inputs(claim_id, fence=fence, principal=_principal())
    service.append_claim_artifacts(
        claim_id,
        fence=fence,
        start_ordinal=0,
        artifacts=(artifact or _artifact(root),),
        principal=_principal(),
    )
    artifacts = service.seal_claim_artifacts(claim_id, fence=fence, principal=_principal())
    service.append_claim_output_tags(
        claim_id,
        fence=fence,
        start_ordinal=0,
        tags=("archive-camera",),
        principal=_principal(),
    )
    output_tags = service.seal_claim_output_tags(claim_id, fence=fence, principal=_principal())
    result = service.get_claim(claim_id, principal=_principal())
    result["inputs"] = inputs
    result["artifacts"] = artifacts
    result["output_tags"] = output_tags
    return result


def _seal_plan(
    service: SqlAlchemyCollectionWorkflowService,
    claim_id: str,
    *,
    execution_id: str = EXECUTION_ID,
    controller_evidence: dict[str, object] = CONTROLLER_EVIDENCE,
    controller_evidence_sha256: str = CONTROLLER_EVIDENCE_SHA256,
    operation_id: str = "archive-video/v1",
    retirement_policy: str = "retain",
) -> dict[str, object]:
    return service.seal_claim_plan(
        claim_id,
        fence=1,
        execution_id=execution_id,
        controller_evidence=controller_evidence,
        controller_evidence_sha256=controller_evidence_sha256,
        operation_id=operation_id,
        operation_sha256="c" * 64,
        retirement_policy=retirement_policy,
        retirement_grace_seconds=0,
        principal=_principal(),
    )


def _seal_dispositions(
    service: SqlAlchemyCollectionWorkflowService,
    claim_id: str,
    *,
    root: CollectionRootIdentity,
    input_path: str,
    output_path: str,
) -> ArtifactDispositionSetIdentity:
    service.record_dispositions(
        claim_id,
        fence=1,
        dispositions=(
            ArtifactDisposition(
                input_collection_id=root.collection_id,
                input_archive_root_sha256=root.archive_root_sha256,
                input_path=input_path,
                status="transformed",
            ),
        ),
        principal=_principal(),
    )
    service.record_disposition_outputs(
        claim_id,
        fence=1,
        outputs=(
            ArtifactDispositionOutput(
                input_collection_id=root.collection_id,
                input_archive_root_sha256=root.archive_root_sha256,
                input_path=input_path,
                output_path=output_path,
            ),
        ),
        principal=_principal(),
    )
    sealed = service.seal_disposition_set(
        claim_id,
        fence=1,
        principal=_principal(),
    )
    while sealed["state"] == "sealing":
        assert service.process_due_disposition_sets(limit=1) == 1
        sealed = service.get_disposition_set(claim_id, principal=_principal())
    assert sealed["state"] == "sealed"
    return ArtifactDispositionSetIdentity.from_mapping(cast(dict[str, object], sealed["identity"]))


def _issue_capability(
    service: SqlAlchemyCollectionWorkflowService,
    claim_id: str,
    root: CollectionRootIdentity,
    *,
    audience: str,
    actions: tuple[str, ...],
) -> dict[str, object]:
    capability = service.issue_capability(
        claim_id,
        fence=1,
        audience=audience,
        actions=actions,
        ttl_seconds=600,
        principal=_principal(),
    )
    service.append_capability_artifacts(
        claim_id,
        str(capability["id"]),
        fence=1,
        start_ordinal=0,
        artifacts=(_artifact(root),),
        principal=_principal(),
    )
    service.seal_capability_artifacts(
        claim_id,
        str(capability["id"]),
        fence=1,
        principal=_principal(),
    )
    return capability


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
    claim = _create_claim(
        service,
        work_id=WORK_ID,
        work_document=work,
        root=root,
    )
    claim_id = str(claim["id"])

    observer_capability = _issue_capability(
        service,
        claim_id,
        root,
        audience="fixture.observer/v1",
        actions=("read-inputs",),
    )
    observer = service.authenticate_capability(str(observer_capability["token"]))
    assert observer_capability["audience"] == "fixture.observer/v1"
    assert observer is not None
    assert observer.app == f"claim:{claim_id}"
    assert observer.key_id == "stove0-key"
    assert observer.has_artifact_scope is True
    assert observer.artifact_scope_capability_id == observer_capability["id"]
    with pytest.raises(Conflict, match="sealed execution plan"):
        service.issue_capability(
            claim_id,
            fence=1,
            audience="fixture.target/v1",
            actions=("read-inputs", "write-output"),
            ttl_seconds=600,
            principal=_principal(),
        )

    sealed = _seal_plan(
        service,
        claim_id,
        retirement_policy="retire-after-verified-output",
    )
    assert sealed["plan"]["execution_id"] == EXECUTION_ID  # type: ignore[index]
    assert service.authenticate_capability(str(observer_capability["token"])) is None

    first = _issue_capability(
        service,
        claim_id,
        root,
        audience="fixture.target/v1",
        actions=("read-inputs", "write-output"),
    )
    second = _issue_capability(
        service,
        claim_id,
        root,
        audience="fixture.target/v1",
        actions=("read-inputs", "write-output"),
    )
    first_principal = service.authenticate_capability(str(first["token"]))
    second_principal = service.authenticate_capability(str(second["token"]))
    assert first_principal is not None and second_principal is not None
    assert first_principal.app == second_principal.app == f"transform:{EXECUTION_ID}"
    assert first_principal.key_id == second_principal.key_id == "stove0-key"

    disposition_set = _seal_dispositions(
        service,
        claim_id,
        root=root,
        input_path="camera/input.mov",
        output_path="video/output.mkv",
    )
    derivation = CollectionDerivation(
        execution_id=EXECUTION_ID,
        claim_id=claim_id,
        fence=1,
        recipe=RecipeIdentity("camera/v1", 1, "b" * 64),
        operation=OperationIdentity("archive-video/v1", "c" * 64),
        input_set_sha256=cast(str, claim["inputs"]["authority"]["sha256"]),  # type: ignore[index]
        artifact_set_sha256=cast(str, claim["artifacts"]["authority"]["sha256"]),  # type: ignore[index]
        output_tag_set_sha256=cast(
            str,
            claim["output_tags"]["authority"]["sha256"],  # type: ignore[index]
        ),
        execution_envelope_sha256=EXECUTION_ID,
        execution_sha256="e" * 64,
        controller_evidence=CONTROLLER_EVIDENCE,
        controller_evidence_sha256=CONTROLLER_EVIDENCE_SHA256,
        disposition_set=disposition_set,
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
                CollectionFileRecord(
                    collection_id=2,
                    path=PRODUCER_EVIDENCE_PATH,
                    bytes=1,
                    sha256="5" * 64,
                ),
            ]
        )
        output_record = session.get(CollectionRecord, 2)
        assert output_record is not None
        output_record.file_count = 3
        output_record.file_bytes = 4 + len(derivation.to_json_bytes()) + 1

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
    with factory() as session, session.begin():
        stored = session.get(CollectionProcessingClaimRecord, claim_id)
        assert stored is not None
        stored.retirement_grace_seconds = 10 * 365 * 24 * 60 * 60
    waiting = service.begin_retirement(
        claim_id,
        fence=1,
        principal=_principal(),
    )
    assert waiting["state"] == "settled"
    with factory() as session, session.begin():
        stored = session.get(CollectionProcessingClaimRecord, claim_id)
        assert stored is not None
        stored.retirement_grace_seconds = 0
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
    _create_claim(
        service,
        work_id=WORK_ID,
        work_document=work,
        root=root,
    )
    changed = {**work, "extra": True}
    with pytest.raises(Conflict, match="another request"):
        service.create_or_resume_claim(
            work_id=WORK_ID,
            work_document=changed,
            work_document_sha256=hashlib.sha256(canonical_json_bytes(changed)).hexdigest(),
            principal=_principal(),
        )
    with pytest.raises(Conflict, match="root differs"):
        _create_claim(
            service,
            work_id="f" * 64,
            work_document={"format": "test"},
            root=CollectionRootIdentity(1, "9" * 64, "1" * 64),
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
    claim = _create_claim(
        service,
        work_id=WORK_ID,
        work_document=work,
        root=root,
    )
    claim_id = str(claim["id"])
    _seal_plan(service, claim_id)
    capability = _issue_capability(
        service,
        claim_id,
        root,
        audience="fixture.target/v1",
        actions=("read-inputs", "write-output"),
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
    claim = _create_claim(
        service,
        work_id=WORK_ID,
        work_document=work,
        root=root,
    )
    claim_id = str(claim["id"])
    _seal_plan(service, claim_id)
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
    claim = _create_claim(
        service,
        work_id=WORK_ID,
        work_document=work,
        root=root,
    )
    claim_id = str(claim["id"])
    _seal_plan(service, claim_id)
    with factory() as session, session.begin():
        session.add(
            CollectionUploadRecord(
                collection_id=3,
                idempotency_key=EXECUTION_ID,
                creation_identity_sha256="a" * 64,
                tag_set_identity=tag_set_identity(("archive-camera",)),
                ingest_source=f"transform:{EXECUTION_ID}",
                encryption_format="age-v1-scrypt",
                passphrase_id="fixture-archive-key-v1",
                provenance_mode="omitted",
                provenance_omission_reason="fixture",
                provenance_identity=None,
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
    capability = _issue_capability(
        service,
        claim_id,
        root,
        audience="fixture.target/v1",
        actions=("read-inputs", "write-output"),
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
    claim = _create_claim(
        service,
        work_id=WORK_ID,
        work_document=work,
        root=root,
    )
    claim_id = str(claim["id"])
    capability = _issue_capability(
        service,
        claim_id,
        root,
        audience="fixture.observer/v1",
        actions=("read-inputs",),
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
    claim = _create_claim(
        service,
        work_id=WORK_ID,
        work_document=work,
        root=root,
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
    claim = _create_claim(
        service,
        work_id=WORK_ID,
        work_document=work,
        root=root,
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


def test_multiple_processing_outcomes_retain_outputs_and_authorize_retirement(
    tmp_path: Path,
    request: FixtureRequest,
) -> None:
    factory = _session_factory(tmp_path, request)
    root = _setup(factory)
    with factory() as session, session.begin():
        session.add(
            CollectionFileRecord(
                collection_id=1,
                path="camera/sidecar.json",
                bytes=2,
                sha256="9" * 64,
            )
        )
    service = SqlAlchemyCollectionWorkflowService(
        cast(Any, object()),
        session_factory=factory,
    )

    parent_work = {"format": "fixture-multi-output-work/v1", "inputs": [root.as_dict()]}
    parent_work_id = hashlib.sha256(canonical_json_bytes(parent_work)).hexdigest()
    parent = _create_claim(
        service,
        work_id=parent_work_id,
        work_document=parent_work,
        root=root,
        purpose="fixture-multi-output/v1",
    )
    parent_id = str(parent["id"])

    def settle_output(
        *,
        outcome_id: str,
        execution_id: str,
        source_path: str,
        source_bytes: int,
        source_sha256: str,
        output_collection_id: int,
        output_path: str,
    ) -> None:
        work = {
            "format": "fixture-output-work/v1",
            "outcome": outcome_id,
            "inputs": [root.as_dict()],
        }
        work_id = hashlib.sha256(canonical_json_bytes(work)).hexdigest()
        claim = _create_claim(
            service,
            work_id=work_id,
            work_document=work,
            root=root,
            artifact=CollectionArtifactIdentity(
                collection=root,
                path=source_path,
                bytes=source_bytes,
                sha256=source_sha256,
            ),
            purpose="fixture-output/v1",
        )
        claim_id = str(claim["id"])
        controller_evidence = {
            "format": "fixture-controller-evidence/v1",
            "execution_envelope": {"execution_envelope_sha256": execution_id},
        }
        controller_evidence_sha256 = hashlib.sha256(
            canonical_json_bytes(controller_evidence)
        ).hexdigest()
        _seal_plan(
            service,
            claim_id,
            execution_id=execution_id,
            controller_evidence=controller_evidence,
            controller_evidence_sha256=controller_evidence_sha256,
            operation_id="fixture.copy/v1",
        )
        disposition_set = _seal_dispositions(
            service,
            claim_id,
            root=root,
            input_path=source_path,
            output_path=output_path,
        )
        derivation = CollectionDerivation(
            execution_id=execution_id,
            claim_id=claim_id,
            fence=1,
            recipe=RecipeIdentity("fixture.branch/v1", 1, "b" * 64),
            operation=OperationIdentity("fixture.copy/v1", "c" * 64),
            input_set_sha256=cast(str, claim["inputs"]["authority"]["sha256"]),  # type: ignore[index]
            artifact_set_sha256=cast(
                str,
                claim["artifacts"]["authority"]["sha256"],  # type: ignore[index]
            ),
            output_tag_set_sha256=cast(
                str,
                claim["output_tags"]["authority"]["sha256"],  # type: ignore[index]
            ),
            execution_envelope_sha256=execution_id,
            execution_sha256="e" * 64,
            controller_evidence=controller_evidence,
            controller_evidence_sha256=controller_evidence_sha256,
            disposition_set=disposition_set,
        )
        with factory() as session, session.begin():
            _collection(
                session,
                output_collection_id,
                creator=f"transform:{execution_id}",
                tag="archive-camera",
                root=str(output_collection_id) * 64,
                idempotency_key=execution_id,
            )
            session.add_all(
                [
                    CollectionFileRecord(
                        collection_id=output_collection_id,
                        path=output_path,
                        bytes=source_bytes,
                        sha256=source_sha256,
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
                        bytes=1,
                        sha256="5" * 64,
                    ),
                ]
            )
            output_record = session.get(CollectionRecord, output_collection_id)
            assert output_record is not None
            output_record.file_count = 3
            output_record.file_bytes = source_bytes + len(derivation.to_json_bytes()) + 1
        service.settle_claim(
            claim_id,
            fence=1,
            output_collection_id=output_collection_id,
            derivation=derivation.as_dict(),
            outcome_claim_id=parent_id,
            outcome_fence=1,
            outcome_id=outcome_id,
            principal=_principal(),
        )

    settle_output(
        outcome_id="video-copy",
        execution_id="1" * 64,
        source_path="camera/input.mov",
        source_bytes=4,
        source_sha256="8" * 64,
        output_collection_id=2,
        output_path="video/output.mkv",
    )
    settle_output(
        outcome_id="sidecar-copy",
        execution_id="2" * 64,
        source_path="camera/sidecar.json",
        source_bytes=2,
        source_sha256="9" * 64,
        output_collection_id=3,
        output_path="metadata/sidecar.json",
    )
    with factory() as session:
        assert processing_claim_blockers(session, 2)
        assert processing_claim_blockers(session, 3)

    settled = service.settle_claim_outcomes(
        parent_id,
        fence=1,
        retirement_policy="retire-after-verified-output",
        retirement_grace_seconds=0,
        principal=_principal(),
    )
    while settled["state"] == "active":
        assert service.process_due_outcome_sets(limit=1) == 1
        settled = service.settle_claim_outcomes(
            parent_id,
            fence=1,
            retirement_policy="retire-after-verified-output",
            retirement_grace_seconds=0,
            principal=_principal(),
        )
    assert settled["state"] == "settled"
    assert settled["outcome_settlement"] is not None
    outcome_authority = cast(dict[str, object], settled["outcomes"])["authority"]
    assert isinstance(outcome_authority, dict)
    page = service.list_claim_outcomes(
        parent_id,
        authority_sha256=cast(str, outcome_authority["sha256"]),
        start_ordinal=0,
        principal=_principal(),
    )
    outcomes = tuple(
        CollectionProcessingOutcomeIdentity.from_mapping(item)
        for item in cast(list[dict[str, object]], page["outcomes"])
    )
    assert [item.outcome_id for item in outcomes] == [
        "sidecar-copy",
        "video-copy",
    ]
    assert (
        service.begin_retirement(parent_id, fence=1, principal=_principal())["state"] == "retiring"
    )
