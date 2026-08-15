#!/usr/bin/env python3
"""Apply the issue #522 hard-cut integration to an exact release/v1 checkout.

This script exists only to build the review branch from the recorded base SHA. It
is fail-closed and idempotent so a workflow rerun cannot silently patch a changed
source tree. The exported handoff archive excludes this script.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MARKER = "issue-522-collection-boundary"


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, content: str) -> None:
    (ROOT / path).write_text(content, encoding="utf-8")


def replace_once(path: str, old: str, new: str) -> None:
    value = read(path)
    if new in value:
        return
    count = value.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected one patch anchor, found {count}: {old[:80]!r}")
    write(path, value.replace(old, new, 1))


def append_once(path: str, marker: str, content: str) -> None:
    value = read(path)
    if marker in value:
        return
    write(path, value.rstrip() + "\n\n" + content.rstrip() + "\n")


def regex_once(path: str, pattern: str, replacement: str, *, flags: int = 0) -> None:
    value = read(path)
    if MARKER in value and replacement.strip() in value:
        return
    updated, count = re.subn(pattern, replacement, value, count=1, flags=flags)
    if count != 1:
        raise RuntimeError(f"{path}: regex patch did not match exactly once: {pattern}")
    write(path, updated)


def patch_exports() -> None:
    append_once(
        "packages/riverhog-protocol/src/riverhog_protocol/__init__.py",
        "CollectionRootIdentity",
        '''from riverhog_protocol.collection_workflows import (\n    DERIVATION_EVIDENCE_PATH,\n    PRODUCER_EVIDENCE_PATH,\n    ArtifactDisposition,\n    CollectionDerivation,\n    CollectionRootIdentity,\n    OperationIdentity,\n    ProducerEvidence,\n    RecipeIdentity,\n    TransformIntent,\n    canonical_json_bytes,\n    canonical_json_sha256,\n)\n\n__all__ += [\n    "ArtifactDisposition",\n    "CollectionDerivation",\n    "CollectionRootIdentity",\n    "DERIVATION_EVIDENCE_PATH",\n    "OperationIdentity",\n    "PRODUCER_EVIDENCE_PATH",\n    "ProducerEvidence",\n    "RecipeIdentity",\n    "TransformIntent",\n    "canonical_json_bytes",\n    "canonical_json_sha256",\n]''',
    )
    append_once(
        "packages/riverhog-api-client/src/riverhog_api_client/__init__.py",
        "CollectionWorkflowClient",
        '''from riverhog_api_client.producer import CollectionProducer, ProducedCollection, ProducerFile\nfrom riverhog_api_client.workflows import CollectionWorkflowClient\n\n__all__ += [\n    "CollectionProducer",\n    "CollectionWorkflowClient",\n    "ProducedCollection",\n    "ProducerFile",\n]''',
    )
    append_once(
        "packages/munchy-api-client/src/munchy_api_client/__init__.py",
        "MunchyCollectionTransformClient",
        '''from munchy_api_client.collection_transforms import (\n    MunchyCollectionTransformClient,\n    MunchyCollectionTransformError,\n)\n\n__all__ += [\n    "MunchyCollectionTransformClient",\n    "MunchyCollectionTransformError",\n]''',
    )


def patch_dependencies() -> None:
    replace_once(
        "packages/riverhog-api-client/pyproject.toml",
        '  "riverhog-protocol>=0.1,<0.2",\n]',
        '  "riverhog-protocol>=0.1,<0.2",\n  "riverhog-provenance>=0.1,<0.2",\n]',
    )
    replace_once(
        "packages/riverhog-api-client/pyproject.toml",
        "riverhog-protocol = { workspace = true }\n",
        "riverhog-protocol = { workspace = true }\nriverhog-provenance = { workspace = true }\n",
    )
    replace_once(
        "companions/jeb/server/pyproject.toml",
        '  "riverhog-cli-support>=0.1,<0.2",\n',
        '  "riverhog-api-client>=0.1,<0.2",\n  "riverhog-cli-support>=0.1,<0.2",\n  "riverhog-protocol>=0.1,<0.2",\n',
    )
    replace_once(
        "companions/jeb/server/pyproject.toml",
        "riverhog-cli-support = { workspace = true }\n",
        "riverhog-api-client = { workspace = true }\nriverhog-cli-support = { workspace = true }\nriverhog-protocol = { workspace = true }\n",
    )
    replace_once(
        "companions/jeb/server/pyproject.toml",
        'description = "Transport-neutral watched-drop collection and target submission."',
        'description = "Payload-free event-driven Riverhog collection transformation controller."',
    )
    replace_once(
        "companions/munchy/server/pyproject.toml",
        'description = "Media ingest, routing, transformation, and destination handoff."',
        'description = "Content-aware Riverhog collection-set-to-collection transform executor."',
    )


def patch_catalog_schema() -> None:
    append_once(
        "riverhog/server/src/riverhog_core/catalog_base.py",
        "catalog_workflow_models",
        '''# Load the hard-cut collection workflow tables into the one current v1 metadata.\nfrom riverhog_core import catalog_workflow_models as _catalog_workflow_models  # noqa: E402,F401''',
    )
    path = "riverhog/server/src/riverhog_core/state_migrations/versions/v1_0003.py"
    replace_once(
        path,
        '"""Normalize persisted retrieval plans to the current v1 policy contract."""',
        '"""Finalize the hard-cut v1 retrieval and collection-workflow schema."""',
    )
    replace_once(
        path,
        "from sqlalchemy import text\n",
        "from sqlalchemy import (\n    BigInteger,\n    CheckConstraint,\n    Column,\n    ForeignKey,\n    ForeignKeyConstraint,\n    Integer,\n    String,\n    Text,\n    text,\n)\n",
    )
    replace_once(
        path,
        '''        connection.execute(\n            text(\n                "UPDATE retrieval_jobs "\n                "SET plan_etag = :plan_etag, constraints_json = :constraints_json "\n                "WHERE id = :job_id"\n            ),\n            {\n                "job_id": str(row["id"]),\n                "plan_etag": str(plan["etag"]),\n                "constraints_json": json.dumps(plan, sort_keys=True, separators=(",", ":")),\n            },\n        )\n''',
        '''        connection.execute(\n            text(\n                "UPDATE retrieval_jobs "\n                "SET plan_etag = :plan_etag, constraints_json = :constraints_json "\n                "WHERE id = :job_id"\n            ),\n            {\n                "job_id": str(row["id"]),\n                "plan_etag": str(plan["etag"]),\n                "constraints_json": json.dumps(plan, sort_keys=True, separators=(",", ":")),\n            },\n        )\n    _create_collection_workflow_schema()\n''',
    )
    insert = r'''

def _create_collection_workflow_schema() -> None:
    collection_id = BigInteger().with_variant(Integer, "sqlite")
    op.create_table(
        "collection_processing_claims",
        Column("id", String(64), primary_key=True),
        Column("transform_id", String(64), nullable=False, unique=True),
        Column("consumer_app", String(), nullable=False),
        Column("consumer_key_id", String(), nullable=True),
        Column("purpose", String(), nullable=False),
        Column("intent_json", Text(), nullable=False),
        Column("recipe_id", String(), nullable=False),
        Column("recipe_revision", Integer(), nullable=False),
        Column("recipe_sha256", String(64), nullable=False),
        Column("operation_id", String(), nullable=False),
        Column("operation_sha256", String(64), nullable=False),
        Column("output_tags_json", Text(), nullable=False),
        Column("retirement_policy", String(), nullable=False),
        Column("retirement_grace_seconds", BigInteger(), nullable=False, server_default="0"),
        Column("state", String(), nullable=False),
        Column("fence", BigInteger(), nullable=False),
        Column("expires_at", String(), nullable=False),
        Column(
            "output_collection_id",
            collection_id,
            ForeignKey("collections.id", ondelete="SET NULL"),
            nullable=True,
        ),
        Column("created_at", String(), nullable=False),
        Column("updated_at", String(), nullable=False),
        Column("settled_at", String(), nullable=True),
        Column("released_at", String(), nullable=True),
        CheckConstraint(
            "state IN ('active','settled','retiring','released')",
            name="ck_collection_processing_claims_state",
        ),
        CheckConstraint("fence >= 1", name="ck_collection_processing_claims_fence"),
        CheckConstraint(
            "retirement_grace_seconds >= 0",
            name="ck_collection_processing_claims_grace",
        ),
    )
    op.create_index(
        "ix_collection_processing_claims_owner_state",
        "collection_processing_claims",
        ["consumer_app", "state", "updated_at"],
    )
    op.create_index(
        "ix_collection_processing_claims_expiry",
        "collection_processing_claims",
        ["state", "expires_at"],
    )
    op.create_table(
        "collection_processing_claim_inputs",
        Column(
            "claim_id",
            String(64),
            ForeignKey("collection_processing_claims.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        Column(
            "collection_id",
            collection_id,
            ForeignKey("collections.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        Column("collection_order", Integer(), nullable=False),
        Column("manifest_sha256", String(64), nullable=False),
        Column("content_etag", String(64), nullable=False),
    )
    op.create_index(
        "ix_collection_processing_claim_inputs_collection",
        "collection_processing_claim_inputs",
        ["collection_id", "claim_id"],
    )
    op.create_index(
        "uq_collection_processing_claim_inputs_order",
        "collection_processing_claim_inputs",
        ["claim_id", "collection_order"],
        unique=True,
    )
    op.create_table(
        "collection_transform_capabilities",
        Column("id", String(32), primary_key=True),
        Column(
            "claim_id",
            String(64),
            ForeignKey("collection_processing_claims.id", ondelete="CASCADE"),
            nullable=False,
        ),
        Column("fence", BigInteger(), nullable=False),
        Column("token_sha256", String(64), nullable=False, unique=True),
        Column("actions_json", Text(), nullable=False),
        Column("state", String(), nullable=False),
        Column("expires_at", String(), nullable=False),
        Column("created_at", String(), nullable=False),
        Column("revoked_at", String(), nullable=True),
        CheckConstraint(
            "state IN ('active','revoked')",
            name="ck_collection_transform_capabilities_state",
        ),
        CheckConstraint("fence >= 1", name="ck_collection_transform_capabilities_fence"),
    )
    op.create_index(
        "ix_collection_transform_capabilities_claim_state",
        "collection_transform_capabilities",
        ["claim_id", "state", "expires_at"],
    )
    op.create_table(
        "collection_derivations",
        Column(
            "collection_id",
            collection_id,
            ForeignKey("collections.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        Column("transform_id", String(64), nullable=False, unique=True),
        Column("claim_id", String(64), nullable=False),
        Column("fence", BigInteger(), nullable=False),
        Column("document_json", Text(), nullable=False),
        Column("document_sha256", String(64), nullable=False),
        Column("created_at", String(), nullable=False),
        ForeignKeyConstraint(
            ["claim_id"], ["collection_processing_claims.id"], ondelete="RESTRICT"
        ),
        CheckConstraint("fence >= 1", name="ck_collection_derivations_fence"),
    )
    op.create_index(
        "ix_collection_derivations_claim",
        "collection_derivations",
        ["claim_id", "collection_id"],
    )
'''
    replace_once(path, "\ndef _normalize_retrieval_plan", insert + "\n\ndef _normalize_retrieval_plan")


def patch_permissions_and_auth() -> None:
    path = "riverhog/server/src/riverhog_core/app_permissions.py"
    replace_once(
        path,
        'COLLECTIONS_CREATE = "collections:create"\n',
        'COLLECTIONS_CREATE = "collections:create"\nCOLLECTION_TRANSFORMS_MANAGE = "collection-transforms:manage"\n',
    )
    replace_once(
        path,
        "        COLLECTIONS_CREATE,\n        COLLECTION_TAGS_MANAGE,\n",
        "        COLLECTIONS_CREATE,\n        COLLECTION_TRANSFORMS_MANAGE,\n        COLLECTION_TAGS_MANAGE,\n",
    )
    replace_once(
        path,
        '    "COLLECTIONS_CREATE",\n',
        '    "COLLECTIONS_CREATE",\n    "COLLECTION_TRANSFORMS_MANAGE",\n',
    )

    path = "riverhog/server/src/riverhog_api/auth.py"
    replace_once(
        path,
        "    COLLECTIONS_CREATE,\n    COLLECTIONS_DELETE,\n",
        "    COLLECTIONS_CREATE,\n    COLLECTIONS_DELETE,\n    COLLECTION_TRANSFORMS_MANAGE,\n",
    )
    replace_once(
        path,
        "    return container.app_keys.authenticate(supplied)\n",
        '''    principal = container.app_keys.authenticate(supplied)\n    if principal is not None:\n        return principal\n    return container.collection_workflows.authenticate_capability(supplied)\n''',
    )
    replace_once(
        path,
        '''CollectionCreator = Annotated[\n    ApplicationPrincipal,\n    Depends(cast(Callable[..., object], require_permission(COLLECTIONS_CREATE))),\n]\n''',
        '''CollectionCreator = Annotated[\n    ApplicationPrincipal,\n    Depends(cast(Callable[..., object], require_permission(COLLECTIONS_CREATE))),\n]\nCollectionTransformManager = Annotated[\n    ApplicationPrincipal,\n    Depends(\n        cast(Callable[..., object], require_permission(COLLECTION_TRANSFORMS_MANAGE))\n    ),\n]\n''',
    )
    replace_once(
        path,
        '    "CollectionTagManager",\n',
        '    "CollectionTagManager",\n    "CollectionTransformManager",\n',
    )


def patch_container_and_apps() -> None:
    path = "riverhog/server/src/riverhog_api/deps.py"
    replace_once(
        path,
        "from riverhog_core.services.collection_uploads import SqlAlchemyCollectionUploadService\n",
        "from riverhog_core.services.collection_uploads import SqlAlchemyCollectionUploadService\nfrom riverhog_core.services.collection_workflows import SqlAlchemyCollectionWorkflowService\n",
    )
    replace_once(
        path,
        "    collection_uploads: SqlAlchemyCollectionUploadService\n",
        "    collection_uploads: SqlAlchemyCollectionUploadService\n    collection_workflows: SqlAlchemyCollectionWorkflowService\n",
    )
    replace_once(
        path,
        "        provenance=SqlAlchemyProvenanceService(config, session_factory=session_factory),\n",
        '''        collection_workflows=SqlAlchemyCollectionWorkflowService(\n            config, session_factory=session_factory\n        ),\n        provenance=SqlAlchemyProvenanceService(config, session_factory=session_factory),\n''',
    )

    path = "riverhog/server/src/riverhog_api/app.py"
    replace_once(
        path,
        "from riverhog_api.routers.tags import router as tags_router\n",
        "from riverhog_api.routers.tags import router as tags_router\nfrom riverhog_api.routers.workflows import router as workflows_router\n",
    )
    replace_once(
        path,
        "    container.collection_uploads.process_due_finalizations(limit=1)\n",
        "    container.collection_uploads.process_due_finalizations(limit=1)\n    container.collection_workflows.reap_expired_claims(limit=100)\n",
    )
    value = read(path)
    if "app.include_router(workflows_router" not in value:
        marker = "    app.openapi_schema = apply_openapi_error_contract(app.openapi())"
        if marker not in value:
            raise RuntimeError("Riverhog app OpenAPI anchor missing")
        value = value.replace(
            marker,
            '    app.include_router(workflows_router, prefix="/v1")\n' + marker,
            1,
        )
        write(path, value)

    path = "companions/munchy/server/src/munchy_api/app.py"
    replace_once(
        path,
        "from munchy_api.composition import configure_adapters\n",
        "from munchy_api.collection_transforms import router as collection_transform_router\nfrom munchy_api.composition import configure_adapters\n",
    )
    value = read(path)
    if "app.include_router(collection_transform_router" not in value:
        marker = "app.openapi_schema = apply_openapi_error_contract(app.openapi())"
        if marker not in value:
            raise RuntimeError("Munchy app OpenAPI anchor missing")
        value = value.replace(
            marker,
            'app.include_router(collection_transform_router, prefix="/v1")\n' + marker,
            1,
        )
        write(path, value)


def patch_collection_identities() -> None:
    path = "riverhog/server/src/riverhog_core/domain/models.py"
    replace_once(
        path,
        '''class CollectionSummary:\n    id: CollectionId\n    created_at: str\n    tags: tuple[str, ...]\n    files: int\n    bytes: int\n''',
        '''class CollectionSummary:\n    id: CollectionId\n    created_at: str\n    tags: tuple[str, ...]\n    content_etag: str\n    manifest_sha256: str\n    files: int\n    bytes: int\n''',
    )

    path = "riverhog/server/src/riverhog_core/services/collections.py"
    replace_once(
        path,
        '''        tags=tuple(sorted(current.tag_id for current in collection.tags)),\n        files=int(row.files),\n''',
        '''        tags=tuple(sorted(current.tag_id for current in collection.tags)),\n        content_etag=collection.content_etag,\n        manifest_sha256=_manifest_identity(copies),\n        files=int(row.files),\n''',
    )
    append_once(
        path,
        "def _manifest_identity",
        '''def _manifest_identity(copies: tuple[ArchiveCopyStatus, ...]) -> str:\n    identities = {\n        current.collection_manifest.sha256\n        for current in copies\n        if current.collection_manifest is not None and current.collection_manifest.sha256\n    }\n    if len(identities) != 1:\n        raise RuntimeError("finalized collection has no unambiguous immutable manifest identity")\n    return str(next(iter(identities)))''',
    )

    path = "riverhog/server/src/riverhog_api/mappers.py"
    replace_once(
        path,
        '''        "tags": list(summary.tags),\n        "files": summary.files,\n''',
        '''        "tags": list(summary.tags),\n        "content_etag": summary.content_etag,\n        "manifest_sha256": summary.manifest_sha256,\n        "files": summary.files,\n''',
    )

    path = "riverhog/server/src/riverhog_api/schemas/collections.py"
    replace_once(
        path,
        '''class CollectionSummaryOut(RiverhogModel):\n    id: int\n    created_at: str\n    tags: list[str]\n    files: int\n''',
        '''class CollectionSummaryOut(RiverhogModel):\n    id: int\n    created_at: str\n    tags: list[str]\n    content_etag: str = Field(pattern=r"^[0-9a-f]{64}$")\n    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")\n    files: int\n''',
    )
    replace_once(
        path,
        '''class DeleteCollectionRequest(RiverhogModel):\n    challenge: str\n    event_context: dict[str, Any] | None = None\n''',
        '''class DeleteCollectionRequest(RiverhogModel):\n    challenge: str\n    retirement_claim_id: str | None = Field(default=None, min_length=1, max_length=64)\n    event_context: dict[str, Any] | None = None\n''',
    )
    replace_once(
        path,
        '''    metadata_rows: dict[str, int]\n    blockers: list[str]\n''',
        '''    metadata_rows: dict[str, int]\n    retirement_claim: dict[str, Any] | None = None\n    blockers: list[str]\n''',
    )

    path = "riverhog/server/src/riverhog_core/services/collection_uploads.py"
    replace_once(
        path,
        '''    summary = {\n        "id": collection.id,\n        "created_at": collection.created_at,\n        "tags": tags,\n        "files": len(collection.files),\n''',
        '''    manifest = (\n        next((current for current in copy.objects if current.object_id == "manifest"), None)\n        if copy\n        else None\n    )\n    if manifest is None or not manifest.sha256:\n        raise RuntimeError("finalized collection has no immutable manifest identity")\n    summary = {\n        "id": collection.id,\n        "created_at": collection.created_at,\n        "tags": tags,\n        "content_etag": collection.content_etag,\n        "manifest_sha256": manifest.sha256,\n        "files": len(collection.files),\n''',
    )
    replace_once(
        path,
        '''        "provenance_etag": collection.provenance_etag,\n        "archive_store": copy.store if copy else store_name,\n''',
        '''        "provenance_etag": collection.provenance_etag,\n        "content_etag": collection.content_etag,\n        "manifest_sha256": manifest.sha256,\n        "archive_store": copy.store if copy else store_name,\n''',
    )


def patch_deletion() -> None:
    path = "riverhog/server/src/riverhog_core/services/collection_deletions.py"
    replace_once(
        path,
        "from riverhog_core.services.lifecycle_events import (\n",
        '''from riverhog_core.services.collection_workflows import (\n    processing_claim_blockers,\n    require_retirement_exemption,\n)\nfrom riverhog_core.services.lifecycle_events import (\n''',
    )
    replace_once(
        path,
        '''    def plan(self, collection_id: int) -> dict[str, object]:\n        normalized_id = _normalize_collection_id_or_raise(collection_id)\n        with session_scope(self._session_factory) as session:\n            active = session.get(CollectionDeletionRecord, normalized_id)\n            if active is not None:\n                return _public_plan(cast(dict[str, object], json.loads(active.plan_json)))\n            expires = (utc_now() + PLAN_TTL).replace(microsecond=0)\n            plan = _build_plan(session, collection_id=normalized_id, expires_at=expires)\n            plan["challenge"] = (\n                None if plan["blockers"] else plan_challenge(_CHALLENGE_PREFIX, plan, expires)\n            )\n            return plan\n''',
        '''    def plan(\n        self,\n        collection_id: int,\n        *,\n        principal: ApplicationPrincipal | None = None,\n        retirement_claim_id: str | None = None,\n    ) -> dict[str, object]:\n        normalized_id = _normalize_collection_id_or_raise(collection_id)\n        with session_scope(self._session_factory) as session:\n            active = session.get(CollectionDeletionRecord, normalized_id)\n            if active is not None:\n                return _public_plan(cast(dict[str, object], json.loads(active.plan_json)))\n            retirement = None\n            if retirement_claim_id is not None:\n                if principal is None:\n                    raise Conflict("retirement deletion requires an authenticated claim owner")\n                retirement = require_retirement_exemption(\n                    session,\n                    claim_id=retirement_claim_id,\n                    collection_id=normalized_id,\n                    principal=principal,\n                )\n            expires = (utc_now() + PLAN_TTL).replace(microsecond=0)\n            plan = _build_plan(\n                session,\n                collection_id=normalized_id,\n                expires_at=expires,\n                exempt_claim_id=retirement_claim_id,\n            )\n            plan["retirement_claim"] = retirement\n            plan["challenge"] = (\n                None if plan["blockers"] else plan_challenge(_CHALLENGE_PREFIX, plan, expires)\n            )\n            return plan\n''',
    )
    replace_once(
        path,
        '''        event_context: dict[str, object] | None = None,\n    ) -> dict[str, object]:\n''',
        '''        event_context: dict[str, object] | None = None,\n        retirement_claim_id: str | None = None,\n    ) -> dict[str, object]:\n''',
    )
    replace_once(
        path,
        '''            if active is not None:\n                if not secrets.compare_digest(active.challenge, supplied_challenge):\n                    raise Conflict("collection deletion challenge does not match active deletion")\n                plan = cast(dict[str, object], json.loads(active.plan_json))\n''',
        '''            if active is not None:\n                if not secrets.compare_digest(active.challenge, supplied_challenge):\n                    raise Conflict("collection deletion challenge does not match active deletion")\n                plan = cast(dict[str, object], json.loads(active.plan_json))\n                expected_retirement = _retirement_claim_id(plan)\n                if expected_retirement != retirement_claim_id:\n                    raise Conflict("collection deletion retirement claim changed")\n''',
    )
    replace_once(
        path,
        '''                plan = _build_plan(session, collection_id=normalized_id, expires_at=expires)\n                if not secrets.compare_digest(\n''',
        '''                retirement = None\n                if retirement_claim_id is not None:\n                    retirement = require_retirement_exemption(\n                        session,\n                        claim_id=retirement_claim_id,\n                        collection_id=normalized_id,\n                        principal=initiator,\n                    )\n                plan = _build_plan(\n                    session,\n                    collection_id=normalized_id,\n                    expires_at=expires,\n                    exempt_claim_id=retirement_claim_id,\n                )\n                plan["retirement_claim"] = retirement\n                if not secrets.compare_digest(\n''',
    )
    replace_once(
        path,
        "            blockers = _active_blockers(session, collection_id)\n",
        "            blockers = _active_blockers(\n                session,\n                collection_id,\n                exempt_claim_id=_retirement_claim_id(plan),\n            )\n",
    )
    replace_once(
        path,
        '''def _build_plan(\n    session: Session,\n    *,\n    collection_id: int,\n    expires_at: datetime,\n) -> dict[str, object]:\n''',
        '''def _build_plan(\n    session: Session,\n    *,\n    collection_id: int,\n    expires_at: datetime,\n    exempt_claim_id: str | None = None,\n) -> dict[str, object]:\n''',
    )
    replace_once(
        path,
        "    blockers = _active_blockers(session, collection_id)\n",
        "    blockers = _active_blockers(\n        session, collection_id, exempt_claim_id=exempt_claim_id\n    )\n",
    )
    replace_once(
        path,
        "def _active_blockers(session: Session, collection_id: int) -> list[str]:\n",
        '''def _active_blockers(\n    session: Session,\n    collection_id: int,\n    *,\n    exempt_claim_id: str | None = None,\n) -> list[str]:\n''',
    )
    replace_once(
        path,
        "    blockers: list[str] = []\n",
        '''    blockers: list[str] = processing_claim_blockers(\n        session, collection_id, exempt_claim_id=exempt_claim_id, limit=_BLOCKER_SAMPLE_LIMIT\n    )\n''',
    )
    append_once(
        path,
        "def _retirement_claim_id",
        '''def _retirement_claim_id(plan: dict[str, object]) -> str | None:\n    value = plan.get("retirement_claim")\n    if not isinstance(value, dict):\n        return None\n    claim_id = value.get("claim_id")\n    return str(claim_id) if claim_id else None''',
    )

    path = "riverhog/server/src/riverhog_api/routers/collections.py"
    replace_once(
        path,
        '''def plan_collection_deletion(\n    collection_id: int,\n    container: ContainerDep,\n    principal: CollectionDeleter,\n) -> CollectionDeletionPlanOut:\n''',
        '''def plan_collection_deletion(\n    collection_id: int,\n    container: ContainerDep,\n    principal: CollectionDeleter,\n    retirement_claim_id: str | None = None,\n) -> CollectionDeletionPlanOut:\n''',
    )
    replace_once(
        path,
        "        container.collection_deletions.plan(collection_id)\n",
        '''        container.collection_deletions.plan(\n            collection_id,\n            principal=principal,\n            retirement_claim_id=retirement_claim_id,\n        )\n''',
    )
    replace_once(
        path,
        '''            initiator=principal,\n            event_context=request.event_context,\n''',
        '''            initiator=principal,\n            event_context=request.event_context,\n            retirement_claim_id=request.retirement_claim_id,\n''',
    )

    path = "packages/riverhog-api-client/src/riverhog_api_client/client.py"
    replace_once(
        path,
        '''    def plan_collection_deletion(self, collection_id: int) -> dict[str, Any]:\n        return self._json(\n            "POST",\n            f"/v1/collections/{str(collection_id)}/deletion-plan",\n        )\n''',
        '''    def plan_collection_deletion(\n        self,\n        collection_id: int,\n        *,\n        retirement_claim_id: str | None = None,\n    ) -> dict[str, Any]:\n        params = (\n            {"retirement_claim_id": retirement_claim_id}\n            if retirement_claim_id is not None\n            else None\n        )\n        return self._json(\n            "POST",\n            f"/v1/collections/{str(collection_id)}/deletion-plan",\n            params=params,\n        )\n''',
    )
    replace_once(
        path,
        '''        challenge: str,\n        event_context: Mapping[str, Any] | None = None,\n    ) -> dict[str, Any]:\n        payload: dict[str, Any] = {"challenge": challenge}\n''',
        '''        challenge: str,\n        retirement_claim_id: str | None = None,\n        event_context: Mapping[str, Any] | None = None,\n    ) -> dict[str, Any]:\n        payload: dict[str, Any] = {"challenge": challenge}\n        if retirement_claim_id is not None:\n            payload["retirement_claim_id"] = retirement_claim_id\n''',
    )


def patch_operation_interfaces() -> None:
    path = "riverhog/server/src/riverhog_api/routers/workflows.py"
    value = read(path)
    for signature in [
        '    response_model=ProcessingClaimPageOut,\n)',
        '    response_model=ProcessingClaimOut,\n)\ndef get_processing_claim',
        '    response_model=ProcessingClaimOut,\n)\ndef begin_processing_claim_retirement',
        '    response_model=ProcessingClaimOut,\n)\ndef release_processing_claim',
        '    response_model=CollectionDerivationOut,\n)\ndef get_collection_derivation',
    ]:
        if signature in value:
            value = value.replace(
                signature,
                signature.replace(
                    "\n)", '\n    openapi_extra=operation_interface("client-only-primitive"),\n)', 1
                ),
                1,
            )
    write(path, value)


def patch_architecture() -> None:
    path = "docs/architecture.md"
    section = '''## Collection workflow model\n\nFinalized Riverhog collections are the only durable payload units. Minimal protocol\nadapters create collections and retain only bounded transient custody until the\nfinalized root receipt. Jeb is a payload-free, tag-targeted transformation\ncontroller. It freezes exact immutable input roots, owns fenced claims, verifies\nderived collections from Riverhog, and separately orchestrates optional retirement.\nMunchy is a content-aware collection transform executor: one finalized collection\nset and one sealed intent produce exactly one finalized collection on success.\nTargets own any bounded encrypted or ephemeral payload workspace.\n\nTags select work but are never transform identity. Processing claims bind exact\nmanifest and content identities. Scoped transform capabilities never expose archive\npassphrases, broad S3 credentials, archive-key selection, or deletion. Every derived\ncollection carries immutable input-root, plan, execution, disposition, and\nprovenance evidence. Active or retiring claims participate in Riverhog deletion\nsafety.\n'''
    append_once(path, "## Collection workflow model", section)


def main() -> None:
    marker = ROOT / ".issue-522-applied"
    if marker.exists():
        return
    patch_exports()
    patch_dependencies()
    patch_catalog_schema()
    patch_permissions_and_auth()
    patch_container_and_apps()
    patch_collection_identities()
    patch_deletion()
    patch_operation_interfaces()
    patch_architecture()
    marker.write_text("issue-522-collection-boundary\n", encoding="utf-8")


if __name__ == "__main__":
    main()
