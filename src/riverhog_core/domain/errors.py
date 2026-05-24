from __future__ import annotations


class RiverhogError(Exception):
    code = "riverhog_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class BadRequest(RiverhogError):
    code = "bad_request"


class InvalidTarget(RiverhogError):
    code = "invalid_target"


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
