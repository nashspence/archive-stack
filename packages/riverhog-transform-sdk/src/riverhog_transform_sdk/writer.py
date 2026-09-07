"""Claim-bound publication of exactly one finalized derived collection."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal, cast

from riverhog_api_client.producer import (
    IncrementalCollectionProducer,
    ProducerArtifactCustody,
    ProducerArtifactIdentity,
    ProducerInput,
)
from riverhog_protocol.collection_workflows import (
    DERIVATION_EVIDENCE_PATH,
    PRODUCER_EVIDENCE_PATH,
    ArtifactDispositionSetIdentity,
    CollectionDerivation,
    JsonValue,
    canonical_json_bytes,
    canonical_json_sha256,
    derivation_evidence_page_path,
)

from riverhog_transform_sdk.models import (
    DerivedCollectionReceipt,
    DerivedCollectionSpec,
)


def _sha256(value: str, label: str) -> str:
    normalized = value.casefold()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError(f"{label} must be a lowercase SHA-256")
    return normalized


def _append_generic_derivation_evidence(
    api: Any,
    producer: IncrementalCollectionProducer,
    *,
    claim_id: str,
    disposition_set: ArtifactDispositionSetIdentity,
) -> None:
    expected = disposition_set.as_dict()
    routes: tuple[tuple[Literal["dispositions", "output-edges"], str, str], ...] = (
        ("dispositions", "list_processing_claim_dispositions", "dispositions"),
        ("output-edges", "list_processing_claim_disposition_outputs", "outputs"),
    )
    for kind, method_name, field_name in routes:
        start = 0
        while True:
            page = getattr(api, method_name)(
                claim_id,
                authority_sha256=disposition_set.sha256,
                start_ordinal=start,
            )
            if page.authority.model_dump(mode="json") != expected or page.start_ordinal != start:
                raise RuntimeError("Riverhog returned changed derivation evidence authority")
            values = getattr(page, field_name)
            if not values:
                raise RuntimeError("Riverhog returned an empty derivation evidence page")
            producer.append_derivation_evidence(
                derivation_evidence_page_path(kind, start),
                canonical_json_bytes(page.model_dump(mode="json", exclude_none=True)),
            )
            if page.next_ordinal is None:
                break
            if page.next_ordinal != start + len(values):
                raise RuntimeError("Riverhog derivation evidence continuation is not contiguous")
            start = page.next_ordinal


class DerivedCollectionWriter:
    """Publish one output collection through a scoped transform capability.

    The writer depends only on controller-sealed identities and evidence. It does
    not import an orchestration application, inspect contents, or choose archive
    object locations.
    """

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
    ) -> None:
        if not claim_id or claim_id != claim_id.strip():
            raise ValueError("derived collection writer requires a canonical claim id")
        if isinstance(fence, bool) or fence < 1:
            raise ValueError("derived collection writer requires a positive fence")
        evidence = dict(controller_evidence)
        if not evidence:
            raise ValueError("derived collection writer requires controller evidence")
        self.api = api
        self.spec = spec
        self.claim_id = claim_id
        self.fence = fence
        self.work_id = _sha256(work_id, "work identity")
        self.execution_id = _sha256(execution_id, "execution identity")
        self.controller_evidence = evidence
        self.controller_evidence_sha256 = canonical_json_sha256(evidence)
        self.producer_app = producer_app
        self.producer_version = producer_version
        claim = api.get_processing_claim(claim_id)
        plan = claim.plan
        if plan is None or plan.execution_id != self.execution_id:
            raise ValueError("derived collection writer requires the sealed claim plan")
        self.input_set_sha256 = plan.inputs.sha256
        self.artifact_set_sha256 = plan.artifacts.sha256

    def replace_api(self, api: Any) -> None:
        self.api = api

    def publish(
        self,
        outputs: Sequence[ProducerInput],
        *,
        execution_envelope_sha256: str,
        execution_sha256: str,
        disposition_set: ArtifactDispositionSetIdentity,
        source_context: Mapping[str, object] | None = None,
        poll_seconds: float = 2.0,
        timeout_seconds: float = 24 * 60 * 60,
    ) -> DerivedCollectionReceipt:
        normalized_outputs = tuple(outputs)
        if not normalized_outputs:
            raise ValueError("successful collection transform must produce output artifacts")
        output_paths = {current.path for current in normalized_outputs}
        if len(output_paths) != len(normalized_outputs):
            raise ValueError("derived collection output paths must be unique")
        if any(
            path in {PRODUCER_EVIDENCE_PATH, DERIVATION_EVIDENCE_PATH}
            or path.startswith("riverhog/")
            for path in output_paths
        ):
            raise ValueError("transform targets may not write Riverhog control paths")
        if disposition_set.output_artifact_count != len(normalized_outputs):
            raise ValueError("sealed disposition authority differs from derived outputs")
        derivation = CollectionDerivation(
            execution_id=self.execution_id,
            claim_id=self.claim_id,
            fence=self.fence,
            recipe=self.spec.recipe,
            operation=self.spec.operation,
            input_set_sha256=self.input_set_sha256,
            artifact_set_sha256=self.artifact_set_sha256,
            execution_envelope_sha256=_sha256(
                execution_envelope_sha256,
                "execution envelope identity",
            ),
            execution_sha256=_sha256(execution_sha256, "execution evidence identity"),
            controller_evidence=cast(dict[str, JsonValue], self.controller_evidence),
            controller_evidence_sha256=self.controller_evidence_sha256,
            disposition_set=disposition_set,
        )
        producer = IncrementalCollectionProducer(
            self.api,
            producer_app=self.producer_app,
            adapter_id="riverhog-derived-collection/v1",
            adapter_version=self.producer_version,
            ingest_source=f"transform:{self.execution_id}",
            source_event_id=self.execution_id,
            source_context={
                **dict(source_context or {}),
                "claim_id": self.claim_id,
                "fence": self.fence,
                "work_id": self.work_id,
                "execution_id": self.execution_id,
                "execution_envelope_sha256": derivation.execution_envelope_sha256,
                "execution_sha256": derivation.execution_sha256,
            },
            idempotency_key=self.execution_id,
            event_context={
                "initiator": {
                    "app": self.producer_app,
                    "claim_id": self.claim_id,
                    "fence": self.fence,
                    "work_id": self.work_id,
                    "execution_id": self.execution_id,
                }
            },
            provenance_mode="captured",
            provenance_omission_reason=(
                "Transform output has no captured host journal for this artifact; "
                "the immutable derivation document records exact execution evidence."
            ),
            server_generated_provenance=True,
        )
        producer.append_inputs(normalized_outputs)
        _append_generic_derivation_evidence(
            self.api,
            producer,
            claim_id=self.claim_id,
            disposition_set=disposition_set,
        )
        receipt = producer.finish(
            terminal_evidence={DERIVATION_EVIDENCE_PATH: derivation.to_json_bytes()},
            poll_seconds=poll_seconds,
            timeout_seconds=timeout_seconds,
        )
        return DerivedCollectionReceipt(
            collection_id=receipt.collection_id,
            archive_root_sha256=receipt.archive_root_sha256,
            content_identity=receipt.content_identity,
            derivation=derivation,
        )


class IncrementalDerivedCollectionWriter:
    """Publish exact derived artifacts as they finalize, then explicitly seal once."""

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
        producer_version: str,
        execution_envelope_sha256: str,
        source_context: Mapping[str, object] | None = None,
    ) -> None:
        self.spec = spec
        self.claim_id = claim_id
        self.fence = fence
        self.work_id = _sha256(work_id, "work identity")
        self.execution_id = _sha256(execution_id, "execution identity")
        self.controller_evidence = dict(controller_evidence)
        self.controller_evidence_sha256 = canonical_json_sha256(self.controller_evidence)
        self.execution_envelope_sha256 = _sha256(
            execution_envelope_sha256,
            "execution envelope identity",
        )
        claim = api.get_processing_claim(claim_id)
        plan = claim.plan
        if plan is None or plan.execution_id != self.execution_id:
            raise ValueError("incremental writer requires the sealed claim plan")
        self.input_set_sha256 = plan.inputs.sha256
        self.artifact_set_sha256 = plan.artifacts.sha256
        self._artifacts: dict[str, ProducerArtifactIdentity] = {}
        self.producer = IncrementalCollectionProducer(
            api,
            producer_app=producer_app,
            adapter_id="riverhog-derived-collection/v1",
            adapter_version=producer_version,
            ingest_source=f"transform:{self.execution_id}",
            source_event_id=self.execution_id,
            source_context={
                **dict(source_context or {}),
                "claim_id": claim_id,
                "fence": fence,
                "work_id": self.work_id,
                "execution_id": self.execution_id,
                "execution_envelope_sha256": self.execution_envelope_sha256,
            },
            idempotency_key=self.execution_id,
            event_context={
                "initiator": {
                    "app": producer_app,
                    "claim_id": claim_id,
                    "fence": fence,
                    "work_id": self.work_id,
                    "execution_id": self.execution_id,
                }
            },
            provenance_mode="captured",
            server_generated_provenance=True,
            provenance_omission_reason=(
                "Transform output has no captured host journal for this artifact; "
                "the immutable derivation document records exact execution evidence."
            ),
        )

    @property
    def custody_receipts(self) -> Mapping[str, ProducerArtifactCustody]:
        return self.producer.custody_receipts

    def heartbeat(self) -> None:
        self.producer.heartbeat()

    def stop(self) -> None:
        self.producer.stop()

    def append(
        self,
        source: ProducerInput,
        *,
        identity: ProducerArtifactIdentity,
    ) -> tuple[ProducerArtifactCustody, ...]:
        if source.path != identity.path:
            raise ValueError("incremental transform source path differs from its identity")
        existing = self._artifacts.get(identity.path)
        if existing is not None and existing != identity:
            raise ValueError(f"incremental transform artifact identity changed: {identity.path}")
        self._artifacts[identity.path] = identity
        return self.producer.append_inputs(
            [source],
            expected_identities={identity.path: identity},
        )

    def finish(
        self,
        *,
        execution_sha256: str,
        disposition_set: ArtifactDispositionSetIdentity,
        poll_seconds: float = 2.0,
        timeout_seconds: float = 24 * 60 * 60,
    ) -> DerivedCollectionReceipt:
        if not self._artifacts:
            raise ValueError("successful collection transform must produce output artifacts")
        if disposition_set.output_artifact_count != len(self._artifacts):
            raise ValueError("sealed disposition authority differs from derived outputs")
        derivation = CollectionDerivation(
            execution_id=self.execution_id,
            claim_id=self.claim_id,
            fence=self.fence,
            recipe=self.spec.recipe,
            operation=self.spec.operation,
            input_set_sha256=self.input_set_sha256,
            artifact_set_sha256=self.artifact_set_sha256,
            execution_envelope_sha256=self.execution_envelope_sha256,
            execution_sha256=_sha256(execution_sha256, "execution evidence identity"),
            controller_evidence=cast(dict[str, JsonValue], self.controller_evidence),
            controller_evidence_sha256=self.controller_evidence_sha256,
            disposition_set=disposition_set,
        )
        _append_generic_derivation_evidence(
            self.producer.api,
            self.producer,
            claim_id=self.claim_id,
            disposition_set=disposition_set,
        )
        produced = self.producer.finish(
            terminal_evidence={DERIVATION_EVIDENCE_PATH: derivation.to_json_bytes()},
            poll_seconds=poll_seconds,
            timeout_seconds=timeout_seconds,
        )
        return DerivedCollectionReceipt(
            collection_id=produced.collection_id,
            archive_root_sha256=produced.archive_root_sha256,
            content_identity=produced.content_identity,
            derivation=derivation,
        )


__all__ = ["DerivedCollectionWriter", "IncrementalDerivedCollectionWriter"]
