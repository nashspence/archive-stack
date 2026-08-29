"""Consumer-runnable conformance checks for content observers."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal, Protocol, Self

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, model_validator
from stove0_observer_client import ContentObserverClient, load_semantic_validator_registry
from stove0_observer_protocol import (
    JSON_SCHEMA_ONLY_SEMANTIC_PROFILE,
    ObservationInvocation,
    ObservationRequest,
    ObservationRequestPayload,
    ObservationResult,
    ObserverDescriptor,
    SemanticFactsConformanceVectors,
    SemanticValidatorProvider,
    accept_observation_result,
    canonical_json_bytes,
    validate_observation_request,
)

OBSERVER_CONFORMANCE_RESULT: Literal["stove0-observer-conformance-result/v1"] = (
    "stove0-observer-conformance-result/v1"
)


class _ObserverConformanceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ObserverConformanceCoverage(_ObserverConformanceModel):
    advertised: int = Field(ge=0, strict=True)
    exercised: int = Field(ge=0, strict=True)
    complete: bool

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        if self.exercised > self.advertised or self.complete != (self.exercised == self.advertised):
            raise ValueError("observer conformance coverage is inconsistent")
        return self


class ObserverSemanticVectorEvidence(_ObserverConformanceModel):
    vectors: SemanticFactsConformanceVectors
    accepted_vector_ids: tuple[str, ...]
    rejected_vector_ids: tuple[str, ...]

    @model_validator(mode="after")
    def validate_outcomes(self) -> Self:
        accepted = tuple(item.id for item in self.vectors.vectors if item.accepted)
        rejected = tuple(item.id for item in self.vectors.vectors if not item.accepted)
        if self.accepted_vector_ids != accepted or self.rejected_vector_ids != rejected:
            raise ValueError("observer semantic-vector evidence differs from its vectors")
        return self


class ObserverContractConformanceEvidence(_ObserverConformanceModel):
    request: ObservationRequest
    observation: ObservationResult


class ObserverContractConformance(_ObserverConformanceModel):
    contract_id: str
    contract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    options_schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    facts_schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    facts_semantics_id: str
    facts_semantics_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    facts_semantics_conformance_vectors_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    preferred_subject_batch_size: int = Field(ge=1, strict=True)
    maximum_result_bytes: int = Field(ge=1, strict=True)
    execution: Literal["not-exercised", "exercised"]
    semantic_conformance: Literal["not-exercised", "schema-only", "exercised"]
    semantic_vectors: ObserverSemanticVectorEvidence | None = None
    evidence: ObserverContractConformanceEvidence | None = None


class ObserverConformanceResult(_ObserverConformanceModel):
    format: Literal["stove0-observer-conformance-result/v1"] = OBSERVER_CONFORMANCE_RESULT
    status: Literal["conformant", "partially-exercised", "inspected"]
    descriptor: ObserverDescriptor
    coverage: ObserverConformanceCoverage
    contracts: tuple[ObserverContractConformance, ...]

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        expected_status = (
            "conformant"
            if self.coverage.complete
            else "partially-exercised"
            if self.coverage.exercised
            else "inspected"
        )
        if self.status != expected_status:
            raise ValueError("observer conformance status differs from its coverage")
        if self.coverage.advertised != len(self.contracts):
            raise ValueError("observer conformance coverage differs from its contracts")
        if len(self.contracts) != len(self.descriptor.contracts):
            raise ValueError("observer conformance contracts differ from the descriptor")
        exercised = 0
        complete = True
        for report, support in zip(self.contracts, self.descriptor.contracts, strict=True):
            if (
                report.contract_id != support.contract_id
                or report.contract_sha256 != support.contract_sha256
                or report.options_schema_sha256 != support.options_schema.sha256
                or report.facts_schema_sha256 != support.facts_schema.sha256
                or report.facts_semantics_id != support.facts_semantics.id
                or report.facts_semantics_sha256 != support.facts_semantics.profile_sha256
                or report.facts_semantics_conformance_vectors_sha256
                != support.facts_semantics.conformance_vectors_sha256
                or report.preferred_subject_batch_size != support.preferred_subject_batch_size
                or report.maximum_result_bytes != support.maximum_result_bytes
            ):
                raise ValueError("observer conformance contract differs from the descriptor")
            has_evidence = report.evidence is not None
            if report.execution != ("exercised" if has_evidence else "not-exercised"):
                raise ValueError("observer execution state differs from its evidence")
            if has_evidence:
                exercised += 1
                assert report.evidence is not None
                request = report.evidence.request
                result = report.evidence.observation
                validate_observation_request(request, self.descriptor)
                if request.observer_contract_id != report.contract_id:
                    raise ValueError("observer evidence names a different contract")
                if (
                    result.request_id != request.request_id
                    or result.observer_contract_id != support.contract_id
                    or result.observer_contract_sha256 != support.contract_sha256
                    or result.observer.descriptor_sha256 != self.descriptor.descriptor_sha256
                    or result.subjects != request.subjects
                ):
                    raise ValueError("observer result does not bind its conformance request")
                if result.state == "observed":
                    if result.facts_schema != support.facts_schema or result.facts is None:
                        raise ValueError("observer result uses an unexpected facts schema")
                    Draft202012Validator(support.facts_schema.document).validate(result.facts)
                if (
                    len(canonical_json_bytes(result.model_dump(mode="json", exclude_none=True)))
                    > request.maximum_result_bytes
                ):
                    raise ValueError("observer result exceeds its conformance request limit")

            profile = support.facts_semantics
            if profile == JSON_SCHEMA_ONLY_SEMANTIC_PROFILE:
                if report.semantic_conformance != "schema-only" or report.semantic_vectors:
                    raise ValueError("schema-only observer semantics have inconsistent evidence")
            elif report.semantic_vectors is None:
                if report.semantic_conformance != "not-exercised":
                    raise ValueError("observer semantic state lacks vector evidence")
                complete = False
            else:
                vectors = report.semantic_vectors.vectors
                if (
                    report.semantic_conformance != "exercised"
                    or vectors.profile_id != profile.id
                    or vectors.sha256 != profile.conformance_vectors_sha256
                ):
                    raise ValueError("observer semantic evidence differs from its profile")
            complete = complete and has_evidence
        if self.coverage.exercised != exercised or self.coverage.complete != complete:
            raise ValueError("observer conformance coverage differs from its evidence")
        return self


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
) -> ObserverConformanceResult:
    descriptor = client.descriptor()
    invocation_by_contract = {item.request.observer_contract_id: item for item in invocations}
    if len(invocation_by_contract) != len(invocations):
        raise ValueError("observer conformance invocations must name unique contracts")
    vectors_by_identity = {(item.profile_id, item.sha256): item for item in semantic_vectors}
    if len(vectors_by_identity) != len(semantic_vectors):
        raise ValueError("observer semantic conformance vectors must have unique identities")
    consumed_vectors: set[tuple[str, str]] = set()
    contract_reports: list[dict[str, Any]] = []
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
            entry["evidence"] = {
                "request": request,
                "observation": result,
            }

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
                    "vectors": vectors,
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
    return ObserverConformanceResult.model_validate(
        {
            "status": (
                "conformant" if complete else "partially-exercised" if exercised else "inspected"
            ),
            "descriptor": descriptor,
            "coverage": {
                "advertised": len(contract_reports),
                "exercised": exercised,
                "complete": complete,
            },
            "contracts": contract_reports,
        }
    )


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
    print(
        json.dumps(
            report.model_dump(mode="json", exclude_none=True),
            indent=2,
            sort_keys=True,
        )
    )
    return 0


__all__ = [
    "OBSERVER_CONFORMANCE_RESULT",
    "ObserverClient",
    "ObserverConformanceResult",
    "conformance_report",
    "main",
]
