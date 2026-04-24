from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select

from arc_core.catalog_models import (
    CollectionFileRecord,
    FinalizedImageCoveredPathRecord,
    FinalizedImageRecord,
    ImageCopyRecord,
    PlannedCandidateRecord,
)
from arc_core.domain.errors import InvalidState, NotFound, NotYetImplemented
from arc_core.iso.streaming import IsoStream, stream_iso_from_root
from arc_core.runtime_config import RuntimeConfig
from arc_core.sqlite_db import make_session_factory, session_scope


class SqlAlchemyPlanningService:
    def __init__(self, config: RuntimeConfig) -> None:
        self._session_factory = make_session_factory(str(config.sqlite_path))
        self._iso_service = ImageRootPlanningService(
            image_lookup=self._image_root_record,
            list_lookup=self.list_images,
            plan_lookup=self.get_plan,
            finalize_lookup=self.finalize_image,
        )

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
    ) -> dict[str, object]:
        with session_scope(self._session_factory) as session:
            finalized_ids = set(
                session.scalars(select(FinalizedImageRecord.image_id)).all()
            )
            all_candidates = session.scalars(select(PlannedCandidateRecord)).all()
            candidates = [c for c in all_candidates if c.finalized_id not in finalized_ids]

            ref = candidates[0] if candidates else (all_candidates[0] if all_candidates else None)
            target_bytes = ref.target_bytes if ref else 0
            min_fill_bytes = ref.min_fill_bytes if ref else 0

            covered_file_pairs: set[tuple[str, str]] = set()
            for cand in all_candidates:
                for cp in cand.covered_paths:
                    covered_file_pairs.add((cp.collection_id, cp.path))

            all_files = session.scalars(select(CollectionFileRecord)).all()
            unplanned_bytes = sum(
                f.bytes
                for f in all_files
                if (f.collection_id, f.path) not in covered_file_pairs
            )

            candidate_views = [_candidate_plan_view(c, target_bytes) for c in candidates]

        if q:
            needle = q.casefold()
            candidate_views = [
                v for v in candidate_views
                if needle in v["candidate_id"].casefold()
                or any(needle in cid.casefold() for cid in v["_collections"])
                or any(needle in pp.casefold() for pp in v["_projected_paths"])
            ]
        if collection:
            candidate_views = [
                v for v in candidate_views if collection in v["_collections"]
            ]
        if iso_ready is not None:
            candidate_views = [
                v for v in candidate_views if v["iso_ready"] is iso_ready
            ]

        reverse = order == "desc"
        sort_key = {
            "fill": lambda v: (v["fill"], v["_bytes"], v["candidate_id"]),
            "bytes": lambda v: (v["_bytes"], v["fill"], v["candidate_id"]),
            "files": lambda v: (v["files"], v["_bytes"], v["candidate_id"]),
            "collections": lambda v: (v["collections"], v["_bytes"], v["candidate_id"]),
            "candidate_id": lambda v: (v["candidate_id"],),
        }[sort]
        candidate_views = sorted(candidate_views, key=sort_key, reverse=reverse)

        total = len(candidate_views)
        pages = math.ceil(total / per_page) if total else 0
        start = (page - 1) * per_page
        page_views = candidate_views[start : start + per_page]

        return {
            "page": page,
            "per_page": per_page,
            "total": total,
            "pages": pages,
            "sort": sort,
            "order": order,
            "ready": bool(candidate_views),
            "target_bytes": target_bytes,
            "min_fill_bytes": min_fill_bytes,
            "candidates": [_strip_internal(v) for v in page_views],
            "unplanned_bytes": unplanned_bytes,
        }

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
    ) -> dict[str, object]:
        with session_scope(self._session_factory) as session:
            all_images = session.scalars(select(FinalizedImageRecord)).all()
            image_views = [_finalized_image_view(img, session) for img in all_images]

        if q:
            needle = q.casefold()
            image_views = [
                v for v in image_views
                if needle in v["id"].casefold()
                or needle in v["filename"].casefold()
                or any(needle in cid.casefold() for cid in v["_collection_ids"])
            ]
        if collection:
            image_views = [v for v in image_views if collection in v["_collection_ids"]]
        if has_copies is not None:
            image_views = [v for v in image_views if (v["copy_count"] > 0) is has_copies]

        reverse = order == "desc"
        sort_key = {
            "finalized_at": lambda v: (v["id"], v["filename"]),
            "bytes": lambda v: (v["_bytes"], v["id"]),
            "copy_count": lambda v: (v["copy_count"], v["id"]),
        }[sort]
        image_views = sorted(image_views, key=sort_key, reverse=reverse)

        total = len(image_views)
        pages = math.ceil(total / per_page) if total else 0
        start = (page - 1) * per_page
        page_views = image_views[start : start + per_page]

        return {
            "page": page,
            "per_page": per_page,
            "total": total,
            "pages": pages,
            "sort": sort,
            "order": order,
            "images": [_strip_internal(v) for v in page_views],
        }

    def get_image(self, image_id: str) -> dict[str, object]:
        with session_scope(self._session_factory) as session:
            record = session.get(FinalizedImageRecord, image_id)
            if record is None:
                raise NotFound(f"image not found: {image_id}")
            return _strip_internal(_finalized_image_view(record, session))

    def finalize_image(self, candidate_id: str) -> dict[str, object]:
        with session_scope(self._session_factory) as session:
            candidate = session.get(PlannedCandidateRecord, candidate_id)
            if candidate is None:
                raise NotFound(f"candidate not found: {candidate_id}")
            if not candidate.iso_ready:
                raise InvalidState("image must be ISO-ready before finalization")
            existing = session.get(FinalizedImageRecord, candidate.finalized_id)
            if existing is None:
                image = FinalizedImageRecord(
                    image_id=candidate.finalized_id,
                    candidate_id=candidate.candidate_id,
                    filename=candidate.filename,
                    bytes=candidate.bytes,
                    image_root=candidate.image_root,
                    target_bytes=candidate.target_bytes,
                )
                session.add(image)
                for cp in candidate.covered_paths:
                    session.add(
                        FinalizedImageCoveredPathRecord(
                            image_id=candidate.finalized_id,
                            collection_id=cp.collection_id,
                            path=cp.path,
                        )
                    )
                session.flush()
                session.refresh(image)
                existing = image
            return _strip_internal(_finalized_image_view(existing, session))

    async def get_iso_stream(self, image_id: str) -> object:
        return await self._iso_service.get_iso_stream(image_id)

    def _image_root_record(self, image_id: str) -> ImageRootRecord:
        with session_scope(self._session_factory) as session:
            record = session.get(FinalizedImageRecord, image_id)
            if record is None:
                raise NotFound(f"image not found: {image_id}")
            return ImageRootRecord(
                image_id=record.image_id,
                volume_id=record.image_id,
                filename=record.filename,
                image_root=Path(record.image_root),
            )


def _candidate_plan_view(
    candidate: PlannedCandidateRecord, target_bytes: int
) -> dict[str, object]:
    collection_ids = sorted({cp.collection_id for cp in candidate.covered_paths})
    projected_paths = sorted(
        f"{cp.collection_id}/{cp.path}" for cp in candidate.covered_paths
    )
    fill = candidate.bytes / target_bytes if target_bytes else 0.0
    return {
        "candidate_id": candidate.candidate_id,
        "bytes": candidate.bytes,
        "fill": fill,
        "files": len(candidate.covered_paths),
        "collections": len(collection_ids),
        "collection_ids": collection_ids,
        "iso_ready": candidate.iso_ready,
        "_bytes": candidate.bytes,
        "_collections": collection_ids,
        "_projected_paths": projected_paths,
    }


def _finalized_image_view(
    image: FinalizedImageRecord, session: object
) -> dict[str, object]:
    from sqlalchemy.orm import Session  # noqa: PLC0415

    assert isinstance(session, Session)
    copy_count = session.scalar(
        select(func.count(ImageCopyRecord.copy_id)).where(
            ImageCopyRecord.image_id == image.image_id
        )
    ) or 0
    collection_ids = sorted({cp.collection_id for cp in image.covered_paths})
    files = len(image.covered_paths)
    fill = image.bytes / image.target_bytes if image.target_bytes else 0.0
    finalized_at = _image_id_to_finalized_at(image.image_id)
    return {
        "id": image.image_id,
        "filename": image.filename,
        "finalized_at": finalized_at,
        "bytes": image.bytes,
        "fill": fill,
        "files": files,
        "collections": len(collection_ids),
        "collection_ids": collection_ids,
        "iso_ready": True,
        "copy_count": copy_count,
        "_bytes": image.bytes,
        "_collection_ids": collection_ids,
    }


def _image_id_to_finalized_at(image_id: str) -> str:
    return (
        f"{image_id[0:4]}-{image_id[4:6]}-{image_id[6:8]}"
        f"T{image_id[9:11]}:{image_id[11:13]}:{image_id[13:15]}Z"
    )


def _strip_internal(view: dict[str, object]) -> dict[str, object]:
    return {k: v for k, v in view.items() if not k.startswith("_")}


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
