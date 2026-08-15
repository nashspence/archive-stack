"""Black-box HTTP client for independently deployed Munchy targets."""

from __future__ import annotations

from typing import Any, TypeVar

import httpx
from pydantic import BaseModel

from munchy_target_support.protocol import (
    TargetCancelRequest,
    TargetContract,
    TargetJobRequest,
    TargetJobStatus,
    TargetPreflightRequest,
    TargetPreflightResponse,
)

ModelT = TypeVar("ModelT", bound=BaseModel)


class TargetProtocolError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class TransformTargetClient:
    def __init__(self, base_url: str, *, timeout: float | None = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _request(
        self,
        method: str,
        path: str,
        model: type[ModelT],
        payload: BaseModel | None = None,
    ) -> ModelT:
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.request(
                    method,
                    f"{self.base_url}{path}",
                    json=payload.model_dump(mode="json", by_alias=True, exclude_none=True)
                    if payload is not None
                    else None,
                )
        except httpx.HTTPError as exc:
            raise TargetProtocolError(f"target request failed: {exc}") from exc
        if response.status_code >= 400:
            raise TargetProtocolError(
                f"target returned {response.status_code}: {response.text}",
                status_code=response.status_code,
            )
        try:
            return model.model_validate(response.json())
        except (ValueError, TypeError) as exc:
            raise TargetProtocolError("target returned an invalid protocol response") from exc

    def contract(self) -> TargetContract:
        return self._request("GET", "/v1/target", TargetContract)

    def preflight(self, request: TargetPreflightRequest) -> TargetPreflightResponse:
        return self._request("POST", "/v1/preflight", TargetPreflightResponse, request)

    def put_job(self, request: TargetJobRequest) -> TargetJobStatus:
        return self._request("PUT", f"/v1/jobs/{request.job_id}", TargetJobStatus, request)

    def status(self, job_id: str) -> TargetJobStatus:
        return self._request("GET", f"/v1/jobs/{job_id}", TargetJobStatus)

    def cancel(self, job_id: str, request: TargetCancelRequest | None = None) -> TargetJobStatus:
        return self._request(
            "POST",
            f"/v1/jobs/{job_id}/cancel",
            TargetJobStatus,
            request or TargetCancelRequest(),
        )


def protocol_report(client: TransformTargetClient) -> dict[str, Any]:
    contract = client.contract()
    return {
        "status": "conformant",
        "protocol": contract.protocol,
        "implementation_id": contract.implementation_id,
        "implementation_version": contract.implementation_version,
        "source_revision": contract.source_revision,
        "target_contract_sha256": contract.contract_sha256,
        "operations": [
            {
                "operation_id": item.operation_id,
                "operation_contract_sha256": item.operation_contract_sha256,
                "options_schema_sha256": item.options_schema.sha256,
            }
            for item in contract.operations
        ],
    }


__all__ = ["TargetProtocolError", "TransformTargetClient", "protocol_report"]
