from __future__ import annotations

from typing import Any


class RiverhogError(Exception):
    code = "riverhog_error"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        observed_status: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code or type(self).code
        if observed_status is not None and not 400 <= observed_status <= 599:
            raise ValueError("observed HTTP status must be a 4xx or 5xx response")
        self.observed_status = observed_status
        self.details = dict(details or {})


class BadRequest(RiverhogError):
    code = "bad_request"


class Forbidden(RiverhogError):
    code = "forbidden"


class Unauthorized(RiverhogError):
    code = "unauthorized"


class InvalidPath(RiverhogError):
    code = "invalid_path"


class NotFound(RiverhogError):
    code = "not_found"


class Conflict(RiverhogError):
    code = "conflict"


class PreconditionFailed(RiverhogError):
    code = "precondition_failed"


class PreconditionRequired(RiverhogError):
    code = "precondition_required"


class InvalidRange(RiverhogError):
    code = "invalid_range"


class InvalidState(RiverhogError):
    code = "invalid_state"


class HashMismatch(RiverhogError):
    code = "hash_mismatch"


class ServiceUnavailable(RiverhogError):
    code = "service_unavailable"


class DownloadAllowanceExceeded(RiverhogError):
    code = "download_allowance_exceeded"


RIVERHOG_ERROR_TYPES_BY_CODE: dict[str, type[RiverhogError]] = {
    error_type.code: error_type
    for error_type in (
        BadRequest,
        Unauthorized,
        Forbidden,
        InvalidPath,
        NotFound,
        Conflict,
        PreconditionFailed,
        PreconditionRequired,
        InvalidRange,
        InvalidState,
        HashMismatch,
        ServiceUnavailable,
        DownloadAllowanceExceeded,
    )
}


def error_type_for_code(code: str) -> type[RiverhogError] | None:
    return RIVERHOG_ERROR_TYPES_BY_CODE.get(code)
