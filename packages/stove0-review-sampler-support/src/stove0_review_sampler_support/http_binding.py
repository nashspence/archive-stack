"""Framework-neutral two-endpoint binding for a terminal review sampler."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from typing import Protocol

from pydantic import ValidationError
from stove0_review_sampler_protocol import SamplerDescriptor, SamplerRequest, SamplerResult


class ReviewSampler(Protocol):
    def descriptor(self) -> SamplerDescriptor: ...

    def sample(self, request: SamplerRequest) -> SamplerResult: ...


@dataclass(frozen=True, slots=True)
class SamplerHttpResponse:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes


class SamplerHttpBinding:
    def __init__(
        self,
        sampler: ReviewSampler,
        *,
        maximum_request_bytes: int = 4 * 1024**2,
        maximum_concurrency: int = 1,
    ) -> None:
        if maximum_request_bytes < 1:
            raise ValueError("sampler request limit must be positive")
        if isinstance(maximum_concurrency, bool) or maximum_concurrency < 1:
            raise ValueError("sampler execution concurrency must be positive")
        self.sampler = sampler
        self.maximum_request_bytes = maximum_request_bytes
        self.maximum_concurrency = maximum_concurrency
        self._execution_slots = threading.BoundedSemaphore(maximum_concurrency)

    def handle(self, method: str, path: str, body: bytes = b"") -> SamplerHttpResponse:
        method = method.upper()
        try:
            if method == "GET" and path == "/v1/sampler":
                if body:
                    return _error(400, "bad_request", "GET /v1/sampler has no body")
                return _model(self.sampler.descriptor())
            if method == "POST" and path == "/v1/sample":
                if len(body) > self.maximum_request_bytes:
                    return _error(413, "request_too_large", "sampler request is too large")
                request = SamplerRequest.model_validate_json(body)
                if request.sampler_descriptor_sha256 != self.sampler.descriptor().descriptor_sha256:
                    return _error(409, "sampler_changed", "sampler descriptor changed")
                with self._execution_slots:
                    return _model(self.sampler.sample(request))
            if path in {"/v1/sampler", "/v1/sample"}:
                return _error(405, "method_not_allowed", "sampler endpoint method is not allowed")
            return _error(404, "not_found", "sampler endpoint not found")
        except (ValidationError, ValueError) as exc:
            return _error(400, "invalid_sampler_request", str(exc))
        except Exception:
            return _error(500, "sampler_failed", "review sampler execution failed")


def _model(model: SamplerDescriptor | SamplerResult) -> SamplerHttpResponse:
    return SamplerHttpResponse(
        200,
        (("Content-Type", "application/json"),),
        model.model_dump_json(exclude_none=True).encode(),
    )


def _error(status: int, code: str, message: str) -> SamplerHttpResponse:
    return SamplerHttpResponse(
        status,
        (("Content-Type", "application/json"),),
        json.dumps(
            {"error": {"code": code, "message": message}},
            sort_keys=True,
            separators=(",", ":"),
        ).encode(),
    )


__all__ = ["ReviewSampler", "SamplerHttpBinding", "SamplerHttpResponse"]
