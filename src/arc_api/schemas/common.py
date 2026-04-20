from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ArcModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ErrorBody(ArcModel):
    code: str
    message: str


class ErrorResponse(ArcModel):
    error: ErrorBody
