from __future__ import annotations

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
    copies: list[FetchHintCopyOut]


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


class EvictRequest(RiverhogModel):
    target: str


class EvictResponse(RiverhogModel):
    target: str
    selected_files: int
    selected_bytes: int
    evicted_files: int
    evicted_bytes: int
    already_cold_files: int
    pinned_files: int
    unarchived_files: int


class PinSummaryOut(RiverhogModel):
    target: str
    fetch: FetchHintOut


class PinsResponse(RiverhogModel):
    pins: list[PinSummaryOut]
