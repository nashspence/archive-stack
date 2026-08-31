from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable

from riverhog_core.app_permissions import ApplicationAccess, ApplicationPrincipal
from riverhog_core.catalog_db import make_session_factory, session_scope
from riverhog_core.catalog_workflow_models import (
    CollectionProcessingClaimRecord,
    CollectionTransformCapabilityArtifactRecord,
    CollectionTransformCapabilityRecord,
)

_NOW = "2026-08-28T00:00:00Z"
_EXPIRES = "2999-01-01T00:00:00Z"


def persisted_artifact_scope(
    database_url: str,
    *,
    access: Iterable[ApplicationAccess],
    artifacts: Iterable[tuple[int, str, int, str]],
) -> ApplicationPrincipal:
    """Install the same persisted artifact authority used by production capabilities."""

    members = tuple(artifacts)
    identity = hashlib.sha256(
        json.dumps(members, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    claim_id = identity
    capability_id = identity[:32]
    with session_scope(make_session_factory(database_url)) as session:
        session.add(
            CollectionProcessingClaimRecord(
                id=claim_id,
                work_id="a" * 64,
                consumer_app="fixture-controller",
                consumer_key_id="fixture-key",
                purpose="fixture artifact authorization",
                work_document_json="{}",
                work_document_sha256=hashlib.sha256(b"{}").hexdigest(),
                retirement_grace_seconds=0,
                state="active",
                fence=1,
                expires_at=_EXPIRES,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
        session.flush()
        session.add(
            CollectionTransformCapabilityRecord(
                id=capability_id,
                claim_id=claim_id,
                fence=1,
                audience="fixture.consumer/v1",
                token_sha256=hashlib.sha256(identity.encode("ascii")).hexdigest(),
                actions_json='["read-inputs"]',
                state="active",
                expires_at=_EXPIRES,
                created_at=_NOW,
            )
        )
        session.flush()
        session.add_all(
            CollectionTransformCapabilityArtifactRecord(
                capability_id=capability_id,
                collection_id=collection_id,
                path=path,
                artifact_order=artifact_order,
                bytes=byte_count,
                sha256=sha256,
            )
            for artifact_order, (collection_id, path, byte_count, sha256) in enumerate(members)
        )
    return ApplicationPrincipal(
        app=f"claim:{claim_id}",
        key_id="fixture-key",
        access=frozenset(access),
        artifact_scope_capability_id=capability_id,
    )


__all__ = ["persisted_artifact_scope"]
