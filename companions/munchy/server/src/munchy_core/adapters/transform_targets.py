"""HTTP adapters for explicitly registered Munchy transform targets."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from munchy_target_support.client import TransformTargetClient
from munchy_target_support.protocol import (
    REGISTRATION_ID_PATTERN,
    TARGET_PROTOCOL,
    TargetCancelRequest,
    TargetContract,
    TargetJobRequest,
    TargetJobStatus,
    TargetPreflightRequest,
    TargetPreflightResponse,
    validate_preflight_response_against_request,
)

from munchy_core.ports.transform_targets import TransformTarget


@dataclass(frozen=True, slots=True)
class ResourceBrokerRegistration:
    endpoint: str
    resource: str


@dataclass(frozen=True, slots=True)
class TransformTargetRegistration:
    registration_id: str
    endpoint: str
    workspace_root: Path
    expected_protocol: str
    expected_target_contract_sha256: str
    resource_broker: ResourceBrokerRegistration | None = None


@dataclass(frozen=True, slots=True)
class InProcessTargetRegistration:
    registration_id: str
    target: TransformTarget
    workspace_root: Path
    expected_target_contract_sha256: str


def _endpoint(value: object, *, label: str) -> str:
    endpoint = str(value or "").strip().rstrip("/")
    try:
        parsed = httpx.URL(endpoint)
    except ValueError as exc:
        raise ValueError(f"{label} must be an HTTP(S) URL") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.host:
        raise ValueError(f"{label} must be an HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(f"{label} must not contain credentials, query, or fragment")
    return endpoint


def _workspace_root(value: object, *, label: str) -> Path:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError(f"{label} is required")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{label} must be absolute")
    return path.resolve()


def load_target_registry(raw: str | None = None) -> dict[str, TransformTargetRegistration]:
    source = raw if raw is not None else os.getenv("MUNCHY_TARGET_REGISTRY", "{}")
    try:
        document = json.loads(source)
    except json.JSONDecodeError as exc:
        raise ValueError("MUNCHY_TARGET_REGISTRY must be a JSON object") from exc
    if not isinstance(document, dict):
        raise ValueError("MUNCHY_TARGET_REGISTRY must be a JSON object")
    registry: dict[str, TransformTargetRegistration] = {}
    for raw_registration_id, raw_registration in document.items():
        registration_id = str(raw_registration_id).strip()
        if re.fullmatch(REGISTRATION_ID_PATTERN, registration_id) is None:
            raise ValueError(f"invalid transform target registration ID: {raw_registration_id}")
        if not isinstance(raw_registration, dict):
            raise ValueError(f"transform target {registration_id} registration must be an object")
        unknown = sorted(
            set(raw_registration)
            - {
                "endpoint",
                "workspace_root",
                "expected_protocol",
                "expected_target_contract_sha256",
                "resource_broker",
            }
        )
        if unknown:
            raise ValueError(
                f"transform target {registration_id} has unknown setting(s): {', '.join(unknown)}"
            )
        expected_protocol = str(raw_registration.get("expected_protocol") or "").strip()
        if expected_protocol != TARGET_PROTOCOL:
            raise ValueError(
                f"transform target {registration_id} expected_protocol must be {TARGET_PROTOCOL}"
            )
        expected_digest = str(raw_registration.get("expected_target_contract_sha256") or "").strip()
        if re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None:
            raise ValueError(
                f"transform target {registration_id} expected_target_contract_sha256 "
                "must be SHA-256 hex"
            )
        broker = None
        raw_broker = raw_registration.get("resource_broker")
        if raw_broker is not None:
            if not isinstance(raw_broker, dict):
                raise ValueError(
                    f"transform target {registration_id} resource_broker must be an object"
                )
            broker_unknown = sorted(set(raw_broker) - {"endpoint", "resource"})
            if broker_unknown:
                raise ValueError(
                    f"transform target {registration_id} resource_broker has unknown setting(s): "
                    + ", ".join(broker_unknown)
                )
            resource = str(raw_broker.get("resource") or "").strip()
            if not resource:
                raise ValueError(
                    f"transform target {registration_id} resource_broker.resource is required"
                )
            broker = ResourceBrokerRegistration(
                endpoint=_endpoint(
                    raw_broker.get("endpoint"),
                    label=f"transform target {registration_id} resource_broker.endpoint",
                ),
                resource=resource,
            )
        registry[registration_id] = TransformTargetRegistration(
            registration_id=registration_id,
            endpoint=_endpoint(
                raw_registration.get("endpoint"),
                label=f"transform target {registration_id} endpoint",
            ),
            workspace_root=_workspace_root(
                raw_registration.get("workspace_root"),
                label=f"transform target {registration_id} workspace_root",
            ),
            expected_protocol=expected_protocol,
            expected_target_contract_sha256=expected_digest,
            resource_broker=broker,
        )
    return registry


class HttpTransformTargetPlatform:
    def __init__(
        self,
        registry: dict[str, TransformTargetRegistration] | None = None,
        *,
        in_process: dict[str, InProcessTargetRegistration] | None = None,
    ) -> None:
        self.registry = dict(registry) if registry is not None else load_target_registry()
        self.in_process = dict(in_process or {})
        overlap = set(self.registry) & set(self.in_process)
        if overlap:
            raise ValueError(
                "transform target registration IDs must be unique: " + ", ".join(sorted(overlap))
            )

    def registration_ids(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.registry) | set(self.in_process)))

    def registration(self, registration_id: str) -> TransformTargetRegistration:
        try:
            return self.registry[registration_id]
        except KeyError as exc:
            raise RuntimeError(f"transform target is not registered: {registration_id}") from exc

    def _client(self, registration_id: str) -> TransformTargetClient:
        return TransformTargetClient(self.registration(registration_id).endpoint)

    def workspace_root(self, registration_id: str) -> Path:
        if registration_id in self.in_process:
            return self.in_process[registration_id].workspace_root
        return self.registration(registration_id).workspace_root

    def contract(self, registration_id: str) -> TargetContract:
        if registration_id in self.in_process:
            local_registration = self.in_process[registration_id]
            contract = local_registration.target.contract()
            if contract.contract_sha256 != local_registration.expected_target_contract_sha256:
                raise RuntimeError(
                    f"transform target {registration_id} contract mismatch: "
                    f"{contract.contract_sha256}"
                )
            return contract
        registration = self.registration(registration_id)
        contract = self._client(registration_id).contract()
        if contract.protocol != registration.expected_protocol:
            raise RuntimeError(
                f"transform target {registration_id} protocol mismatch: {contract.protocol}"
            )
        if contract.contract_sha256 != registration.expected_target_contract_sha256:
            raise RuntimeError(
                f"transform target {registration_id} contract mismatch: {contract.contract_sha256}"
            )
        return contract

    def preflight(
        self,
        registration_id: str,
        request: TargetPreflightRequest,
    ) -> TargetPreflightResponse:
        contract = self.contract(registration_id)
        response = (
            self.in_process[registration_id].target.preflight(request)
            if registration_id in self.in_process
            else self._client(registration_id).preflight(request)
        )
        if response.target != contract:
            raise RuntimeError(
                f"transform target {registration_id} changed identity during preflight"
            )
        try:
            validate_preflight_response_against_request(response, request)
        except ValueError as exc:
            raise RuntimeError(
                f"transform target {registration_id} changed the declared transform: {exc}"
            ) from exc
        return response

    def put_job(self, registration_id: str, request: TargetJobRequest) -> TargetJobStatus:
        if registration_id in self.in_process:
            return self.in_process[registration_id].target.put_job(request)
        self.contract(registration_id)
        return self._client(registration_id).put_job(request)

    def status(self, registration_id: str, job_id: str) -> TargetJobStatus:
        if registration_id in self.in_process:
            return self.in_process[registration_id].target.status(job_id)
        return self._client(registration_id).status(job_id)

    def cancel(
        self,
        registration_id: str,
        job_id: str,
        request: TargetCancelRequest,
    ) -> TargetJobStatus:
        if registration_id in self.in_process:
            return self.in_process[registration_id].target.cancel(job_id, request)
        return self._client(registration_id).cancel(job_id, request)

    def resource_request(
        self,
        registration_id: str,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if registration_id in self.in_process:
            return None
        broker = self.registration(registration_id).resource_broker
        if broker is None:
            return None
        request_payload = dict(payload or {})
        request_payload.setdefault("target", broker.resource)
        with httpx.Client(timeout=None) as client:
            response = client.request(method, f"{broker.endpoint}{path}", json=request_payload)
        if response.status_code >= 400:
            raise RuntimeError(
                f"resource broker for {registration_id} returned "
                f"{response.status_code}: {response.text}"
            )
        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError(f"resource broker for {registration_id} returned non-object JSON")
        return data
