"""Consumer-runnable conformance checks for content observers."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

from jsonschema import Draft202012Validator
from stove0_observer_client import ContentObserverClient, load_semantic_validator_registry
from stove0_observer_protocol import (
    JSON_SCHEMA_ONLY_SEMANTIC_PROFILE,
    ObservationInvocation,
    ObservationRequest,
    ObservationRequestPayload,
    ObserverDescriptor,
    SemanticFactsConformanceVectors,
    SemanticValidatorProvider,
    accept_observation_result,
    canonical_json_bytes,
)


class ObserverClient(Protocol):
    def descriptor(self) -> ObserverDescriptor: ...

    def observe(
        self,
        invocation: ObservationInvocation,
        *,
        descriptor: ObserverDescriptor,
    ) -> Any: ...


def conformance_report(
    client: ObserverClient,
    *,
    invocations: Sequence[ObservationInvocation] = (),
    semantic_vectors: Sequence[SemanticFactsConformanceVectors] = (),
    semantic_validators: SemanticValidatorProvider | None = None,
) -> dict[str, Any]:
    descriptor = client.descriptor()
    invocation_by_contract = {item.request.observer_contract_id: item for item in invocations}
    if len(invocation_by_contract) != len(invocations):
        raise ValueError("observer conformance invocations must name unique contracts")
    vectors_by_identity = {(item.profile_id, item.sha256): item for item in semantic_vectors}
    if len(vectors_by_identity) != len(semantic_vectors):
        raise ValueError("observer semantic conformance vectors must have unique identities")
    consumed_vectors: set[tuple[str, str]] = set()
    contract_reports: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    for support in descriptor.contracts:
        entry: dict[str, Any] = {
            "contract_id": support.contract_id,
            "contract_sha256": support.contract_sha256,
            "options_schema_sha256": support.options_schema.sha256,
            "facts_schema_sha256": support.facts_schema.sha256,
            "facts_semantics_id": support.facts_semantics.id,
            "facts_semantics_sha256": support.facts_semantics.profile_sha256,
            "facts_semantics_conformance_vectors_sha256": (
                support.facts_semantics.conformance_vectors_sha256
            ),
            "preferred_subject_batch_size": support.preferred_subject_batch_size,
            "maximum_result_bytes": support.maximum_result_bytes,
            "execution": "not-exercised",
            "semantic_conformance": "not-exercised",
        }
        invocation = invocation_by_contract.get(support.contract_id)
        if invocation is not None:
            request = invocation.request
            if request.observer_contract_sha256 != support.contract_sha256:
                raise RuntimeError("invocation does not bind the observer's published contract")
            Draft202012Validator(support.options_schema.document).validate(request.options)
            result = client.observe(invocation, descriptor=descriptor)
            accept_observation_result(result, request, descriptor, semantic_validators)
            if result.state == "observed":
                Draft202012Validator(support.facts_schema.document).validate(result.facts)
            if len(canonical_json_bytes(result.model_dump(mode="json", exclude_none=True))) > (
                request.maximum_result_bytes
            ):
                raise RuntimeError("observer result exceeds the invocation limit")
            entry["execution"] = "exercised"
            observations.append(result.model_dump(mode="json", exclude_none=True))

        profile = support.facts_semantics
        if profile == JSON_SCHEMA_ONLY_SEMANTIC_PROFILE:
            entry["semantic_conformance"] = "schema-only"
        else:
            expected_vectors_sha256 = profile.conformance_vectors_sha256
            assert expected_vectors_sha256 is not None
            vectors = vectors_by_identity.get((profile.id, expected_vectors_sha256))
            if vectors is not None:
                if invocation is None:
                    raise ValueError(
                        "semantic-vector conformance requires an invocation for its contract"
                    )
                validator = (
                    semantic_validators.resolve(profile.id, profile.profile_sha256)
                    if semantic_validators is not None
                    else None
                )
                if validator is None:
                    raise ValueError(
                        "semantic validator is unavailable for observer profile "
                        f"{profile.id}@{profile.profile_sha256}"
                    )
                accepted_ids: list[str] = []
                rejected_ids: list[str] = []
                base = invocation.request
                for vector in vectors.vectors:
                    Draft202012Validator(support.options_schema.document).validate(vector.options)
                    Draft202012Validator(support.facts_schema.document).validate(vector.facts)
                    vector_request = ObservationRequest.seal(
                        ObservationRequestPayload(
                            **base.model_dump(
                                mode="python",
                                exclude={"request_id", "subjects", "options"},
                            ),
                            subjects=vector.subjects,
                            options=vector.options,
                        )
                    )
                    try:
                        validator(vector_request, vector.facts)
                    except ValueError as exc:
                        if vector.accepted:
                            raise RuntimeError(
                                f"semantic validator rejected accepted vector: {vector.id}"
                            ) from exc
                        rejected_ids.append(vector.id)
                    else:
                        if not vector.accepted:
                            raise RuntimeError(
                                f"semantic validator accepted rejected vector: {vector.id}"
                            )
                        accepted_ids.append(vector.id)
                entry["semantic_conformance"] = "exercised"
                entry["semantic_vectors"] = {
                    "sha256": vectors.sha256,
                    "accepted_vector_ids": accepted_ids,
                    "rejected_vector_ids": rejected_ids,
                }
                consumed_vectors.add((profile.id, expected_vectors_sha256))
        contract_reports.append(entry)

    unknown_invocations = set(invocation_by_contract) - {
        support.contract_id for support in descriptor.contracts
    }
    if unknown_invocations:
        raise ValueError("observer conformance invocation names an unadvertised contract")
    if consumed_vectors != set(vectors_by_identity):
        raise ValueError("semantic vectors do not bind an advertised observer profile")
    complete = all(
        item["execution"] == "exercised"
        and item["semantic_conformance"] in {"schema-only", "exercised"}
        for item in contract_reports
    )
    exercised = sum(item["execution"] == "exercised" for item in contract_reports)
    report: dict[str, Any] = {
        "status": (
            "conformant" if complete else "partially-exercised" if exercised else "inspected"
        ),
        "protocol": descriptor.protocol,
        "implementation_id": descriptor.implementation_id,
        "implementation_version": descriptor.implementation_version,
        "source_revision": descriptor.source_revision,
        "image_digest": descriptor.image_digest,
        "descriptor_sha256": descriptor.descriptor_sha256,
        "coverage": {
            "advertised": len(contract_reports),
            "exercised": exercised,
            "complete": complete,
        },
        "contracts": contract_reports,
    }
    if observations:
        report["observations"] = observations
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stove0-observer-conformance",
        description="Check a deployed stove0 content observer's v1 contract.",
    )
    parser.add_argument("base_url")
    parser.add_argument(
        "--invocation",
        type=Path,
        action="append",
        default=[],
        help="JSON ObservationInvocation to exercise (repeat once per advertised contract)",
    )
    parser.add_argument(
        "--semantic-vectors",
        type=Path,
        action="append",
        default=[],
        help="contract-owned SemanticFactsConformanceVectors to exercise (repeatable)",
    )
    parser.add_argument(
        "--semantic-validator-provider",
        action="append",
        default=[],
        help="installed contract-validator provider entry point to enable (repeatable)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    invocations = tuple(
        ObservationInvocation.model_validate_json(path.read_text(encoding="utf-8"))
        for path in args.invocation
    )
    semantic_vectors = tuple(
        SemanticFactsConformanceVectors.model_validate_json(path.read_text(encoding="utf-8"))
        for path in args.semantic_vectors
    )
    semantic_validators = load_semantic_validator_registry(args.semantic_validator_provider)
    report = conformance_report(
        ContentObserverClient(
            args.base_url,
            semantic_validators=semantic_validators,
        ),
        invocations=invocations,
        semantic_vectors=semantic_vectors,
        semantic_validators=semantic_validators,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


__all__ = ["ObserverClient", "conformance_report", "main"]
