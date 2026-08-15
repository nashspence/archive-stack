"""One immutable collection set in, one immutable Riverhog collection out.

This service is payload-stateless: Munchy retains only workflow state. A
content-aware target owns any bounded encrypted or ephemeral workspace and
returns finalized output artifacts for direct Riverhog collection creation.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from riverhog_api_client import ApiClient
from riverhog_api_client.producer import CollectionProducer, ProducerFile
from riverhog_protocol.collection_workflows import (
    DERIVATION_EVIDENCE_PATH,
    ArtifactDisposition,
    CollectionDerivation,
    TransformIntent,
    canonical_json_sha256,
)
from time_formats import utc_timestamp_now


class CollectionTransformStore(Protocol):
    def load(self, job_id: str) -> dict[str, Any] | None: ...

    def save(self, job: Mapping[str, Any]) -> dict[str, Any]: ...


class CollectionTransformTarget(Protocol):
    """Content-aware target that owns all temporary payload workspace."""

    def execute(self, request: TargetCollectionRequest) -> TargetCollectionResult: ...

    def release(self, job_id: str) -> None: ...


@dataclass(frozen=True, slots=True)
class TargetCollectionRequest:
    job_id: str
    claim_id: str
    fence: int
    capability_token: str
    intent: TransformIntent


@dataclass(frozen=True, slots=True)
class TargetCollectionResult:
    outputs: tuple[ProducerFile, ...]
    plan_sha256: str
    execution_sha256: str
    dispositions: tuple[ArtifactDisposition, ...]
    provenance_journals: Mapping[str, bytes]
    source_context: Mapping[str, object]

    def __post_init__(self) -> None:
        if not self.outputs:
            raise ValueError("successful collection transform must produce output files")
        output_paths = [item.path for item in self.outputs]
        if len(output_paths) != len(set(output_paths)):
            raise ValueError("collection transform output paths must be unique")
        if DERIVATION_EVIDENCE_PATH in output_paths:
            raise ValueError("target may not replace Riverhog derivation evidence")
        for label, value in (
            ("plan", self.plan_sha256),
            ("execution", self.execution_sha256),
        ):
            if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
                raise ValueError(f"collection transform {label} identity is invalid")
        canonical = tuple(sorted(self.dispositions))
        if not canonical or canonical != self.dispositions:
            raise ValueError("collection transform dispositions must be canonical and nonempty")


class SQLiteCollectionTransformStore:
    """Adapter over Munchy's existing durable generic state store."""

    kind = "collection-transform"

    def load(self, job_id: str) -> dict[str, Any] | None:
        from munchy_core.persistence import sqlite_state

        return sqlite_state.read_state(self.kind, job_id)

    def save(self, job: Mapping[str, Any]) -> dict[str, Any]:
        from munchy_core.persistence import sqlite_state

        payload = dict(job)
        return sqlite_state.write_state(self.kind, str(payload["job_id"]), payload)


class MunchyCollectionTransformService:
    """Durable coordinator for the hard-cut collection transform contract."""

    def __init__(
        self,
        *,
        target: CollectionTransformTarget,
        store: CollectionTransformStore | None = None,
        riverhog_api_factory: Callable[[str], ApiClient] | None = None,
        producer_version: str = "development",
    ) -> None:
        self.target = target
        self.store = store or SQLiteCollectionTransformStore()
        self.riverhog_api_factory = riverhog_api_factory or (
            lambda token: ApiClient(token=token)
        )
        self.producer_version = producer_version
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        # Capability bearer material is intentionally process-local. It is scoped,
        # short-lived, and can be refreshed idempotently by Jeb after restart.
        self._capability_tokens: dict[str, str] = {}
        self._capability_tokens_guard = threading.Lock()

    def create_or_resume(
        self,
        *,
        job_id: str,
        claim_id: str,
        fence: int,
        capability_token: str,
        intent: Mapping[str, object],
        owner_app: str | None = None,
    ) -> dict[str, Any]:
        document = TransformIntent.from_mapping(intent)
        if job_id != document.transform_id:
            raise ValueError("Munchy job identity must equal the sealed transform identity")
        request_digest = canonical_json_sha256(
            {
                "job_id": job_id,
                "claim_id": claim_id,
                "fence": fence,
                "intent": document.as_dict(),
                "owner_app": owner_app,
            }
        )
        existing = self.store.load(job_id)
        if existing is not None:
            if existing.get("request_sha256") != request_digest:
                raise ValueError("Munchy transform job identity was reused with another request")
            if str(existing.get("state") or "") not in {"succeeded", "failed", "canceled"}:
                self._set_capability_token(job_id, capability_token)
                if existing.get("phase") == "waiting_for_capability":
                    existing["phase"] = "queued"
                    existing["updated_at"] = utc_timestamp_now()
                    existing = self.store.save(existing)
            return _public_job(existing)
        now = utc_timestamp_now()
        self._set_capability_token(job_id, capability_token)
        return self.store.save(
            {
                "format": "munchy-collection-transform/v1",
                "job_id": job_id,
                "claim_id": claim_id,
                "fence": fence,
                "request_sha256": request_digest,
                "owner_app": owner_app,
                "intent": document.as_dict(),
                "state": "queued",
                "phase": "queued",
                "created_at": now,
                "updated_at": now,
            }
        )

    def get(self, job_id: str, *, include_secret: bool = False) -> dict[str, Any]:
        job = self.store.load(job_id)
        if job is None:
            raise KeyError(job_id)
        return _public_job(job, include_secret=include_secret)

    def run(self, job_id: str) -> dict[str, Any]:
        with self._lock(job_id):
            job = self.store.load(job_id)
            if job is None:
                raise KeyError(job_id)
            if str(job.get("state") or "") in {"succeeded", "failed", "canceled"}:
                return _public_job(job)
            intent = TransformIntent.from_mapping(_mapping(job.get("intent"), "transform intent"))
            token = self._capability_token(job_id)
            if not token:
                job["state"] = "queued"
                job["phase"] = "waiting_for_capability"
                job["updated_at"] = utc_timestamp_now()
                return _public_job(self.store.save(job))
            job["state"] = "running"
            job["phase"] = "target"
            job["started_at"] = job.get("started_at") or utc_timestamp_now()
            self.store.save(job)
            try:
                target_result = self.target.execute(
                    TargetCollectionRequest(
                        job_id=job_id,
                        claim_id=str(job["claim_id"]),
                        fence=int(job["fence"]),
                        capability_token=token,
                        intent=intent,
                    )
                )
                derivation = CollectionDerivation(
                    transform_id=intent.transform_id,
                    claim_id=str(job["claim_id"]),
                    fence=int(job["fence"]),
                    recipe=intent.recipe,
                    operation=intent.operation,
                    inputs=intent.inputs,
                    output_tags=intent.output_tags,
                    plan_sha256=target_result.plan_sha256,
                    execution_sha256=target_result.execution_sha256,
                    dispositions=target_result.dispositions,
                )
                job["phase"] = "output_collection"
                self.store.save(job)
                with self.riverhog_api_factory(token) as api:
                    receipt = CollectionProducer(
                        api,
                        producer_app="munchy",
                        adapter_id="munchy-collection-transform/v1",
                        adapter_version=self.producer_version,
                        ingest_source=f"transform:{intent.transform_id}",
                        tags=intent.output_tags,
                        provenance_omission_reason=(
                            "Transform output has no captured host journal for this artifact; "
                            "the immutable derivation document records exact execution evidence."
                        ),
                    ).publish(
                        target_result.outputs,
                        source_event_id=intent.transform_id,
                        source_context={
                            "claim_id": str(job["claim_id"]),
                            "fence": int(job["fence"]),
                            "plan_sha256": target_result.plan_sha256,
                            "execution_sha256": target_result.execution_sha256,
                            **dict(target_result.source_context),
                        },
                        inline_evidence={DERIVATION_EVIDENCE_PATH: derivation.to_json_bytes()},
                        provenance_journals=target_result.provenance_journals,
                        idempotency_key=intent.transform_id,
                        event_context={
                            "initiator": {
                                "app": "munchy",
                                "claim_id": str(job["claim_id"]),
                                "transform_id": intent.transform_id,
                            }
                        },
                    )
                job.update(
                    {
                        "state": "succeeded",
                        "phase": "done",
                        "output_collection_id": receipt.collection_id,
                        "output_manifest_sha256": receipt.manifest_sha256,
                        "output_content_etag": receipt.content_etag,
                        "derivation": derivation.as_dict(),
                        "finished_at": utc_timestamp_now(),
                    }
                )
            except Exception as exc:
                job.update(
                    {
                        "state": "failed",
                        "phase": "failed",
                        "error": {
                            "code": "collection_transform_failed",
                            "message": str(exc)[:1000],
                        },
                        "finished_at": utc_timestamp_now(),
                    }
                )
            finally:
                self._drop_capability_token(job_id)
                self.store.save(job)
                try:
                    self.target.release(job_id)
                except Exception:
                    # Output settlement is authoritative. Workspace release is
                    # retryable target maintenance and may not alter the result.
                    job["workspace_release_pending"] = True
                    self.store.save(job)
            return _public_job(job)

    def _lock(self, job_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(job_id, threading.Lock())

    def _set_capability_token(self, job_id: str, token: str) -> None:
        supplied = token.strip()
        if not supplied:
            raise ValueError("collection transform requires a scoped Riverhog capability")
        with self._capability_tokens_guard:
            self._capability_tokens[job_id] = supplied

    def _capability_token(self, job_id: str) -> str:
        with self._capability_tokens_guard:
            return self._capability_tokens.get(job_id, "")

    def _drop_capability_token(self, job_id: str) -> None:
        with self._capability_tokens_guard:
            self._capability_tokens.pop(job_id, None)


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise RuntimeError(f"stored {label} is not an object")
    return value


def _public_job(job: Mapping[str, Any], *, include_secret: bool = False) -> dict[str, Any]:
    # ``include_secret`` is retained for adapter compatibility; bearer material is
    # never persisted and therefore can never be projected from durable state.
    _ = include_secret
    return dict(job)


__all__ = [
    "CollectionTransformStore",
    "CollectionTransformTarget",
    "MunchyCollectionTransformService",
    "SQLiteCollectionTransformStore",
    "TargetCollectionRequest",
    "TargetCollectionResult",
]
