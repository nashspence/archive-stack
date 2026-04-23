from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from arc_core.domain.errors import NotYetImplemented
from arc_core.iso.streaming import IsoStream, stream_iso_from_root


class StubPlanningService:
    def get_plan(
        self,
        *,
        page: int = 1,
        per_page: int = 25,
        sort: str = "fill",
        order: str = "desc",
        q: str | None = None,
        collection: str | None = None,
        iso_ready: bool | None = None,
    ) -> object:
        raise NotYetImplemented("StubPlanningService is not implemented yet")

    def list_images(
        self,
        *,
        page: int,
        per_page: int,
        sort: str,
        order: str,
        q: str | None,
        collection: str | None,
        has_copies: bool | None,
    ) -> object:
        raise NotYetImplemented("StubPlanningService is not implemented yet")

    def get_image(self, image_id: str) -> object:
        raise NotYetImplemented("StubPlanningService is not implemented yet")

    def finalize_image(self, image_id: str) -> object:
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

    def __init__(
        self,
        *,
        image_lookup: Callable[[str], object],
        list_lookup: Callable[..., object] | None = None,
        plan_lookup: Callable[..., object],
        finalize_lookup: Callable[[str], object] | None = None,
    ) -> None:
        self._image_lookup = image_lookup
        self._list_lookup = list_lookup
        self._plan_lookup = plan_lookup
        self._finalize_lookup = finalize_lookup

    def get_plan(
        self,
        *,
        page: int = 1,
        per_page: int = 25,
        sort: str = "fill",
        order: str = "desc",
        q: str | None = None,
        collection: str | None = None,
        iso_ready: bool | None = None,
    ) -> object:
        return self._plan_lookup(
            page=page,
            per_page=per_page,
            sort=sort,
            order=order,
            q=q,
            collection=collection,
            iso_ready=iso_ready,
        )

    def list_images(
        self,
        *,
        page: int,
        per_page: int,
        sort: str,
        order: str,
        q: str | None,
        collection: str | None,
        has_copies: bool | None,
    ) -> object:
        if self._list_lookup is None:
            raise NotYetImplemented("ImageRootPlanningService list_images is not configured")
        return self._list_lookup(
            page=page,
            per_page=per_page,
            sort=sort,
            order=order,
            q=q,
            collection=collection,
            has_copies=has_copies,
        )

    def get_image(self, image_id: str) -> object:
        return self._image_lookup(image_id)

    def finalize_image(self, image_id: str) -> object:
        if self._finalize_lookup is None:
            raise NotYetImplemented("ImageRootPlanningService finalize_image is not configured")
        return self._finalize_lookup(image_id)

    async def get_iso_stream(self, image_id: str) -> IsoStream:
        image = self._image_lookup(image_id)
        if not isinstance(image, ImageRootRecord):
            raise TypeError("image lookup must return ImageRootRecord for get_iso_stream")
        return await stream_iso_from_root(
            image_root=image.image_root,
            volume_id=image.volume_id,
            filename=image.filename,
        )
