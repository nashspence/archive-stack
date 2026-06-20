from __future__ import annotations

from pydantic import Field

from riverhog_api.schemas.common import RiverhogModel


class PinRequest(RiverhogModel):
    target: str


class HotStatusOut(RiverhogModel):
    state: str
    present_bytes: int
    missing_bytes: int


class FetchHintCopyOut(RiverhogModel):
    id: str
    volume_id: str
    location: str


class FetchHintOut(RiverhogModel):
    id: str
    state: str
    files: int = 0
    bytes: int = 0
    missing_bytes: int = 0
    copy_count: int = 0
    copies: list[FetchHintCopyOut] = Field(default_factory=list)


class PinResponse(RiverhogModel):
    target: str
    pin: bool
    hot: HotStatusOut
    fetch: FetchHintOut | None


class ReleaseRequest(RiverhogModel):
    target: str


class ReleaseResponse(RiverhogModel):
    target: str
    pin: bool


class PinSummaryOut(RiverhogModel):
    target: str
    fetch: FetchHintOut


class PinsResponse(RiverhogModel):
    pins: list[PinSummaryOut]
