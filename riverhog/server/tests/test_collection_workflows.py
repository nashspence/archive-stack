from __future__ import annotations

from typing import Any, cast

from riverhog_core.app_permissions import ApplicationPrincipal
from riverhog_core.catalog_base import Base
from riverhog_core.catalog_db import SessionFactory
from riverhog_core.catalog_models import (
    CollectionArchiveCopyRecord,
    CollectionArchiveObjectRecord,
    CollectionFileRecord,
    CollectionRecord,
    CollectionTagRecord,
    TagRecord,
)
from riverhog_core.services.collection_workflows import (
    SqlAlchemyCollectionWorkflowService,
    processing_claim_blockers,
)
from riverhog_protocol.collection_workflows import (
    DERIVATION_EVIDENCE_PATH,
    ArtifactDisposition,
    CollectionDerivation,
    OperationIdentity,
    RecipeIdentity,
    TransformIntent,
)
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

NOW = "2026-08-15T00:00:00Z"


def _session_factory(tmp_path) -> SessionFactory:  # type: ignore[no-untyped-def]
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'state.sqlite3'}")
    Base.metadata.create_all(engine)
    return cast(SessionFactory, sessionmaker(engine, expire_on_commit=False))


def _collection(session, collection_id: int, *, creator: str, tag: str, root: str) -> None:  # type: ignore[no-untyped-def]
    collection = CollectionRecord(
        id=collection_id,
        creation_idempotency_key=f"collection-{collection_id}",
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
    return ApplicationPrincipal(app="jeb", key_id="jeb-key", access=frozenset())


def test_claim_capability_settlement_and_deletion_blocker(tmp_path) -> None:  # type: ignore[no-untyped-def]
    factory = _session_factory(tmp_path)
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

    service = SqlAlchemyCollectionWorkflowService(
        cast(Any, object()),
        session_factory=factory,
    )
    claim = service.create_or_resume_claim(
        input_collection_ids=[1],
        recipe_id="camera/v1",
        recipe_revision=1,
        recipe_sha256="b" * 64,
        operation_id="archive-video/v1",
        operation_sha256="c" * 64,
        effective_intent={"container": "mkv"},
        output_tags=["archive-camera"],
        retirement_policy="retire-after-verified-output",
        retirement_grace_seconds=0,
        principal=_principal(),
    )
    claim_id = str(claim["id"])
    capability = service.issue_capability(
        claim_id,
        fence=1,
        actions=("read-inputs", "write-output"),
        ttl_seconds=600,
        principal=_principal(),
    )
    scoped = service.authenticate_capability(str(capability["token"]))
    assert scoped is not None
    assert scoped.app == f"transform:{claim_id}"

    intent = TransformIntent.from_mapping(claim["intent"])  # type: ignore[arg-type]
    derivation = CollectionDerivation(
        transform_id=intent.transform_id,
        claim_id=claim_id,
        fence=1,
        recipe=RecipeIdentity("camera/v1", 1, "b" * 64),
        operation=OperationIdentity("archive-video/v1", "c" * 64),
        inputs=intent.inputs,
        output_tags=intent.output_tags,
        plan_sha256="d" * 64,
        execution_sha256="e" * 64,
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
            creator=f"transform:{claim_id}",
            tag="archive-camera",
            root="6" * 64,
        )
        session.add(
            CollectionFileRecord(
                collection_id=2,
                path=DERIVATION_EVIDENCE_PATH,
                bytes=len(derivation.to_json_bytes()),
                sha256=derivation.sha256,
            )
        )

    settled = service.settle_claim(
        claim_id,
        fence=1,
        output_collection_id=2,
        derivation=derivation.as_dict(),
        principal=_principal(),
    )
    assert settled["state"] == "settled"
    assert service.authenticate_capability(str(capability["token"])) is None
    with factory() as session:
        blockers = processing_claim_blockers(session, 1)
    assert blockers and claim_id in blockers[0]
