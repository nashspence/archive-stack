"""Target-facing collection transform execution context."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Self

from riverhog_api_client import ApiClient
from riverhog_api_client.producer import ProducerInput
from riverhog_protocol.collection_workflows import ArtifactDisposition, CollectionRootIdentity

from riverhog_transform_sdk.capability import CapabilityApiClient
from riverhog_transform_sdk.models import (
    ClaimedArtifact,
    DerivedCollectionReceipt,
    DerivedCollectionSpec,
)
from riverhog_transform_sdk.provenance import prepare_transform_provenance
from riverhog_transform_sdk.reader import ClaimedCollectionReader, ClaimedRetrieval
from riverhog_transform_sdk.workspace import TransformWorkspace, WorkspaceAssurance
from riverhog_transform_sdk.writer import DerivedCollectionWriter

CancellationCheck = Callable[[], None]


class ClaimedCollectionRuntime:
    """Capability-scoped read custody over exact immutable Riverhog inputs."""

    def __init__(
        self,
        api: Any,
        *,
        inputs: Sequence[CollectionRootIdentity],
        claim_id: str,
        fence: int,
        work_id: str,
        execution_id: str,
        cancellation_check: CancellationCheck | None = None,
        input_retrieval_policy: Literal["available-only", "allow"] = "available-only",
        owned_api: bool = False,
    ) -> None:
        self.api = (
            api
            if isinstance(api, CapabilityApiClient)
            else CapabilityApiClient(api, owns_client=owned_api)
        )
        self.work_id = work_id
        self.execution_id = execution_id
        self.claim_id = claim_id
        self.fence = fence
        self.cancellation_check = cancellation_check
        if input_retrieval_policy not in {"available-only", "allow"}:
            raise ValueError("claimed collection input retrieval policy is invalid")
        self.input_retrieval_policy = input_retrieval_policy
        self._closed = False
        self._retrievals: list[ClaimedRetrieval] = []
        self.reader = ClaimedCollectionReader(
            self.api,
            inputs=inputs,
            work_id=work_id,
            claim_id=claim_id,
            fence=fence,
            heartbeat=self.heartbeat,
        )

    @classmethod
    def from_capability(
        cls,
        *,
        base_url: str,
        capability_token: str,
        inputs: Sequence[CollectionRootIdentity],
        claim_id: str,
        fence: int,
        work_id: str,
        execution_id: str,
        allow_insecure_http: bool = False,
        **kwargs: Any,
    ) -> ClaimedCollectionRuntime:
        api = ApiClient(
            base_url=base_url,
            token=_capability_token(capability_token),
            allow_insecure_http=allow_insecure_http,
        )
        return cls(
            api,
            inputs=inputs,
            claim_id=claim_id,
            fence=fence,
            work_id=work_id,
            execution_id=execution_id,
            owned_api=True,
            **kwargs,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.close()

    def close(self, *, raise_errors: bool = True) -> None:
        if self._closed:
            return
        self._closed = True
        failures: list[Exception] = []
        for retrieval in self._retrievals:
            if retrieval.closed:
                continue
            try:
                retrieval.close(success=False)
            except Exception as exc:
                failures.append(exc)
        self.api.close()
        if failures and raise_errors:
            raise RuntimeError("failed to cancel active claimed collection retrieval jobs") from (
                failures[0]
            )

    def heartbeat(self) -> None:
        if self._closed:
            raise RuntimeError("claimed collection runtime is closed")
        if self.cancellation_check is not None:
            self.cancellation_check()

    def refresh_capability(self, capability_token: str) -> None:
        """Replace an expiring token without changing claim/fence identity."""

        self.heartbeat()
        current = self.api.current
        replacement = ApiClient(
            base_url=current.base_url,
            token=_capability_token(capability_token),
            allow_insecure_http=current.allow_insecure_http,
        )
        replacement.host_header = current.host_header
        replacement.http2 = current.http2
        replacement.timeout_seconds = current.timeout_seconds
        replacement.upload_timeout_seconds = current.upload_timeout_seconds
        self.api.replace(replacement, owns_client=True)

    def inventory(self) -> tuple[ClaimedArtifact, ...]:
        self.heartbeat()
        return self.reader.inventory()

    def prepare_inputs(
        self,
        artifacts: Sequence[ClaimedArtifact] | None = None,
        **kwargs: Any,
    ) -> ClaimedRetrieval:
        self.heartbeat()
        kwargs.setdefault("restore_policy", self.input_retrieval_policy)
        retrieval = self.reader.prepare(artifacts, **kwargs)
        self._retrievals.append(retrieval)
        return retrieval

    def open_workspace(
        self,
        root: Path,
        *,
        assurance: WorkspaceAssurance,
    ) -> TransformWorkspace:
        self.heartbeat()
        return TransformWorkspace.open(
            root,
            execution_id=self.execution_id,
            assurance=assurance,
        )


class CollectionTransformRuntime:
    """One capability-scoped target execution over immutable Riverhog inputs."""

    def __init__(
        self,
        api: Any,
        *,
        spec: DerivedCollectionSpec,
        claim_id: str,
        fence: int,
        work_id: str,
        execution_id: str,
        controller_evidence: Mapping[str, object],
        producer_app: str,
        producer_version: str = "development",
        cancellation_check: CancellationCheck | None = None,
        input_retrieval_policy: Literal["available-only", "allow"] = "available-only",
        owned_api: bool = False,
    ) -> None:
        self.api = (
            api
            if isinstance(api, CapabilityApiClient)
            else CapabilityApiClient(api, owns_client=owned_api)
        )
        self.spec = spec
        self.work_id = work_id
        self.execution_id = execution_id
        self.producer_app = producer_app
        self.producer_version = producer_version
        self.started_at = (
            datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
        )
        self.controller_evidence = dict(controller_evidence)
        if not self.controller_evidence:
            raise ValueError("collection transform runtime requires controller evidence")
        self.claim_id = claim_id
        self.fence = fence
        self.cancellation_check = cancellation_check
        if input_retrieval_policy not in {"available-only", "allow"}:
            raise ValueError("transform input retrieval policy is invalid")
        self.input_retrieval_policy = input_retrieval_policy
        self._closed = False
        self._published_receipt: DerivedCollectionReceipt | None = None
        self._retrievals: list[ClaimedRetrieval] = []
        self.reader = ClaimedCollectionReader(
            self.api,
            inputs=spec.inputs,
            work_id=work_id,
            claim_id=claim_id,
            fence=fence,
            heartbeat=self.heartbeat,
        )
        self.writer = DerivedCollectionWriter(
            self.api,
            spec=spec,
            work_id=work_id,
            claim_id=claim_id,
            fence=fence,
            execution_id=execution_id,
            controller_evidence=self.controller_evidence,
            producer_app=producer_app,
            producer_version=producer_version,
        )

    @classmethod
    def from_capability(
        cls,
        *,
        base_url: str,
        capability_token: str,
        spec: DerivedCollectionSpec,
        claim_id: str,
        fence: int,
        work_id: str,
        execution_id: str,
        controller_evidence: Mapping[str, object],
        allow_insecure_http: bool = False,
        **kwargs: Any,
    ) -> CollectionTransformRuntime:
        api = ApiClient(
            base_url=base_url,
            token=_capability_token(capability_token),
            allow_insecure_http=allow_insecure_http,
        )
        return cls(
            api,
            spec=spec,
            claim_id=claim_id,
            fence=fence,
            work_id=work_id,
            execution_id=execution_id,
            controller_evidence=controller_evidence,
            owned_api=True,
            **kwargs,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        # Retrieval cleanup is subordinate once the immutable output collection
        # has finalized. Before publication, cleanup failures remain actionable.
        self.close(raise_errors=self._published_receipt is None)

    def close(self, *, raise_errors: bool = True) -> None:
        if self._closed:
            return
        self._closed = True
        failures: list[Exception] = []
        for retrieval in self._retrievals:
            if retrieval.closed:
                continue
            try:
                retrieval.close(success=False)
            except Exception as exc:
                failures.append(exc)
        self.api.close()
        if failures and raise_errors:
            raise RuntimeError("failed to cancel active transform retrieval jobs") from failures[0]

    def heartbeat(self) -> None:
        if self._closed:
            raise RuntimeError("collection transform runtime is closed")
        if self.cancellation_check is not None:
            self.cancellation_check()

    def refresh_capability(self, capability_token: str) -> None:
        """Replace an expiring token without changing claim/fence identity."""

        self.heartbeat()
        current = self.api.current
        replacement = ApiClient(
            base_url=current.base_url,
            token=_capability_token(capability_token),
            allow_insecure_http=current.allow_insecure_http,
        )
        replacement.host_header = current.host_header
        replacement.http2 = current.http2
        replacement.timeout_seconds = current.timeout_seconds
        replacement.upload_timeout_seconds = current.upload_timeout_seconds
        self.api.replace(replacement, owns_client=True)

    def inventory(self) -> tuple[ClaimedArtifact, ...]:
        self.heartbeat()
        return self.reader.inventory()

    def prepare_inputs(
        self,
        artifacts: Sequence[ClaimedArtifact] | None = None,
        **kwargs: Any,
    ) -> ClaimedRetrieval:
        self.heartbeat()
        kwargs.setdefault("restore_policy", self.input_retrieval_policy)
        retrieval = self.reader.prepare(artifacts, **kwargs)
        self._retrievals.append(retrieval)
        return retrieval

    def open_workspace(
        self,
        root: Path,
        *,
        assurance: WorkspaceAssurance,
    ) -> TransformWorkspace:
        self.heartbeat()
        return TransformWorkspace.open(
            root,
            execution_id=self.execution_id,
            assurance=assurance,
        )

    def publish(
        self,
        outputs: Sequence[ProducerInput],
        *,
        execution_envelope_sha256: str,
        execution_sha256: str,
        dispositions: Sequence[ArtifactDisposition],
        source_context: Mapping[str, object] | None = None,
        **kwargs: Any,
    ) -> DerivedCollectionReceipt:
        self.heartbeat()
        normalized_dispositions = tuple(dispositions)
        inventory = self.inventory()
        expected = {
            (current.root.collection_id, current.root.archive_root_sha256, current.path)
            for current in inventory
        }
        actual = {
            (
                current.input_collection_id,
                current.input_archive_root_sha256,
                current.input_path,
            )
            for current in normalized_dispositions
        }
        if actual != expected or len(actual) != len(normalized_dispositions):
            raise ValueError(
                "artifact dispositions must account for every claimed input exactly once"
            )
        provenance_builder = prepare_transform_provenance(
            self.api,
            inventory=inventory,
            dispositions=normalized_dispositions,
            execution_id=self.execution_id,
            operation_id=self.spec.operation.id,
            producer_app=self.producer_app,
            producer_version=self.producer_version,
            started_at=self.started_at,
            heartbeat=self.heartbeat,
        )
        receipt = self.writer.publish(
            outputs,
            execution_envelope_sha256=execution_envelope_sha256,
            execution_sha256=execution_sha256,
            dispositions=normalized_dispositions,
            provenance_builder=provenance_builder,
            source_context=source_context,
            **kwargs,
        )
        self._published_receipt = receipt
        return receipt


def _capability_token(value: str) -> str:
    token = value.strip()
    if not token:
        raise ValueError("transform capability token must be nonempty")
    return token


__all__ = ["CancellationCheck", "ClaimedCollectionRuntime", "CollectionTransformRuntime"]
