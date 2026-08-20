"""Safe construction of contract-bound content-observation results."""

from __future__ import annotations

from collections.abc import Mapping

from jsonschema import Draft202012Validator
from pydantic import JsonValue
from stove0_observer_protocol import (
    ObservationFailure,
    ObservationRequest,
    ObservationResult,
    ObservationResultPayload,
    ObserverDescriptor,
    ObserverImplementation,
    canonical_json_sha256,
    validate_observation_result,
)


class ObservationResultBuilder:
    """Build bounded results that exactly bind one sealed observation request."""

    def __init__(
        self,
        descriptor: ObserverDescriptor,
        request: ObservationRequest,
    ) -> None:
        if descriptor.descriptor_sha256 != request.observer_descriptor_sha256:
            raise ValueError("observer descriptor differs from the sealed request")
        support = descriptor.support_for(request.observer_contract_id)
        if support.contract_sha256 != request.observer_contract_sha256:
            raise ValueError("observer contract differs from the sealed request")
        if len(request.subjects) > support.maximum_subjects:
            raise ValueError("observation request exceeds the observer subject limit")
        if request.maximum_result_bytes > support.maximum_result_bytes:
            raise ValueError("observation request exceeds the observer result limit")
        Draft202012Validator(support.options_schema.document).validate(request.options)
        self.descriptor = descriptor
        self.request = request
        self.support = support

    def observed(
        self,
        facts: Mapping[str, JsonValue],
        *,
        execution_evidence: Mapping[str, JsonValue] | None = None,
    ) -> ObservationResult:
        document = dict(facts)
        Draft202012Validator(self.support.facts_schema.document).validate(document)
        return self._seal(
            state="observed",
            facts_schema=self.support.facts_schema,
            facts=document,
            facts_sha256=canonical_json_sha256(document),
            execution_evidence=dict(execution_evidence or {}),
        )

    def inapplicable(
        self,
        *,
        execution_evidence: Mapping[str, JsonValue] | None = None,
    ) -> ObservationResult:
        return self._seal(
            state="inapplicable",
            execution_evidence=dict(execution_evidence or {}),
        )

    def failed(
        self,
        *,
        code: str,
        message: str,
        retryable: bool,
        execution_evidence: Mapping[str, JsonValue] | None = None,
    ) -> ObservationResult:
        return self._seal(
            state="failed",
            failure=ObservationFailure(
                code=code,
                message=message,
                retryable=retryable,
            ),
            execution_evidence=dict(execution_evidence or {}),
        )

    def canceled(
        self,
        *,
        execution_evidence: Mapping[str, JsonValue] | None = None,
    ) -> ObservationResult:
        return self._seal(
            state="canceled",
            execution_evidence=dict(execution_evidence or {}),
        )

    def _seal(self, **updates: object) -> ObservationResult:
        payload: dict[str, object] = {
            "request_id": self.request.request_id,
            "observer": ObserverImplementation(
                id=self.descriptor.implementation_id,
                version=self.descriptor.implementation_version,
                source_revision=self.descriptor.source_revision,
                descriptor_sha256=self.descriptor.descriptor_sha256,
            ),
            "observer_contract_id": self.support.contract_id,
            "observer_contract_sha256": self.support.contract_sha256,
            "subjects": self.request.subjects,
        }
        payload.update(updates)
        result = ObservationResult.seal(ObservationResultPayload.model_validate(payload))
        validate_observation_result(result, self.request, self.descriptor)
        return result


__all__ = ["ObservationResultBuilder"]
