from __future__ import annotations

from arc_api.schemas.common import ArcModel


class PinRequest(ArcModel):
    target: str


class HotStatusOut(ArcModel):
    state: str
    present_bytes: int
    missing_bytes: int


class FetchHintCopyOut(ArcModel):
    id: str
    location: str


class FetchHintOut(ArcModel):
    id: str
    state: str
    copies: list[FetchHintCopyOut]


class PinResponse(ArcModel):
    target: str
    pin: bool
    hot: HotStatusOut
    fetch: FetchHintOut | None


class ReleaseRequest(ArcModel):
    target: str


class ReleaseResponse(ArcModel):
    target: str
    pin: bool


class PinSummaryOut(ArcModel):
    target: str


class PinsResponse(ArcModel):
    pins: list[PinSummaryOut]
