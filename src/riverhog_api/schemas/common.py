from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class RiverhogModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ErrorBody(RiverhogModel):
    code: str
    message: str


class ErrorResponse(RiverhogModel):
    error: ErrorBody
