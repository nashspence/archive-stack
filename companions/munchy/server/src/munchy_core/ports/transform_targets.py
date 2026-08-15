"""Ports for Munchy's registered transform targets and optional resource brokers."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from munchy_target_support.protocol import (
    TargetCancelRequest,
    TargetContract,
    TargetJobRequest,
    TargetJobStatus,
    TargetPreflightRequest,
    TargetPreflightResponse,
)


class TransformTarget(Protocol):
    def contract(self) -> TargetContract: ...

    def preflight(self, request: TargetPreflightRequest) -> TargetPreflightResponse: ...

    def put_job(self, request: TargetJobRequest) -> TargetJobStatus: ...

    def status(self, job_id: str) -> TargetJobStatus: ...

    def cancel(self, job_id: str, request: TargetCancelRequest) -> TargetJobStatus: ...


class TransformTargetPlatform(Protocol):
    def registration_ids(self) -> tuple[str, ...]: ...

    def workspace_root(self, registration_id: str) -> Path: ...

    def contract(self, registration_id: str) -> TargetContract: ...

    def preflight(
        self,
        registration_id: str,
        request: TargetPreflightRequest,
    ) -> TargetPreflightResponse: ...

    def put_job(self, registration_id: str, request: TargetJobRequest) -> TargetJobStatus: ...

    def status(self, registration_id: str, job_id: str) -> TargetJobStatus: ...

    def cancel(
        self,
        registration_id: str,
        job_id: str,
        request: TargetCancelRequest,
    ) -> TargetJobStatus: ...

    def resource_request(
        self,
        registration_id: str,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None: ...
