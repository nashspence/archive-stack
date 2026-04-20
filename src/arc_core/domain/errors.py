from __future__ import annotations


class ArcError(Exception):
    code = "arc_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class BadRequest(ArcError):
    code = "bad_request"


class InvalidTarget(ArcError):
    code = "invalid_target"


class NotFound(ArcError):
    code = "not_found"


class Conflict(ArcError):
    code = "conflict"


class InvalidState(ArcError):
    code = "invalid_state"


class HashMismatch(ArcError):
    code = "hash_mismatch"


class NotYetImplemented(ArcError):
    code = "not_implemented"
