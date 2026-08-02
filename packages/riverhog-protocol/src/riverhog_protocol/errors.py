from __future__ import annotations

from typing import Any


class RiverhogError(Exception):
    code = "riverhog_error"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code or type(self).code
        self.status = status
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


class InvalidState(RiverhogError):
    code = "invalid_state"


class HashMismatch(RiverhogError):
    code = "hash_mismatch"


class NotYetImplemented(RiverhogError):
    code = "not_implemented"


class ServiceUnavailable(RiverhogError):
    code = "service_unavailable"


class DownloadAllowanceExceeded(RiverhogError):
    code = "download_allowance_exceeded"
