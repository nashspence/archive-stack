from __future__ import annotations

from http_api_contracts import ErrorBody, ErrorResponse, HealthResponse
from pydantic import BaseModel, ConfigDict


class RiverhogModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


__all__ = ["ErrorBody", "ErrorResponse", "HealthResponse", "RiverhogModel"]
