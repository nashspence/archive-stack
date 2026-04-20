from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from arc_core.domain.errors import NotYetImplemented
from arc_core.iso.streaming import IsoStream, stream_iso_from_root


class StubPlanningService:
    def get_plan(self) -> object:
        raise NotYetImplemented("StubPlanningService is not implemented yet")

    def get_image(self, image_id: str) -> object:
        raise NotYetImplemented("StubPlanningService is not implemented yet")

    def get_iso_stream(self, image_id: str) -> object:
        raise NotYetImplemented("StubPlanningService is not implemented yet")


@dataclass(slots=True)
class ImageRootRecord:
    image_id: str
    volume_id: str
    filename: str
    image_root: Path


class ImageRootPlanningService:
    """Thin adapter for planner implementations that materialize an image root directory."""

    def __init__(self, *, image_lookup: Callable[[str], object], plan_lookup: Callable[[], object]) -> None:
        self._image_lookup = image_lookup
        self._plan_lookup = plan_lookup

    def get_plan(self) -> object:
        return self._plan_lookup()

    def get_image(self, image_id: str) -> object:
        return self._image_lookup(image_id)

    async def get_iso_stream(self, image_id: str) -> IsoStream:
        image = self._image_lookup(image_id)
        if not isinstance(image, ImageRootRecord):
            raise TypeError("image lookup must return ImageRootRecord for get_iso_stream")
        return await stream_iso_from_root(
            image_root=image.image_root,
            volume_id=image.volume_id,
            filename=image.filename,
        )
