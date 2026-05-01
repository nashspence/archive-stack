from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from sqlalchemy import select
from sqlalchemy.orm import Session

from arc_core.archive_compliance import (
    copy_counts_as_verified,
    copy_counts_toward_protection,
    image_protection_state,
    normalize_glacier_state,
    normalize_required_copy_count,
    registered_copy_shortfall,
)
from arc_core.catalog_models import (
    CollectionFileRecord,
    CollectionRecord,
    CollectionUploadRecord,
    FinalizedImageCollectionArtifactRecord,
    FinalizedImageCoveragePartRecord,
    FinalizedImageCoveredPathRecord,
    FinalizedImageRecord,
    ImageCopyEventRecord,
    ImageCopyRecord,
    PlannedCandidateRecord,
)
from arc_core.domain.enums import CopyState, GlacierState, VerificationState
from arc_core.domain.errors import InvalidState, NotFound, NotYetImplemented
from arc_core.finalized_image_coverage import (
    read_finalized_image_collection_artifacts,
    read_finalized_image_coverage_parts,
)
from arc_core.iso.streaming import IsoStream, stream_iso_from_root
from arc_core.recovery_payloads import (
    CommandAgeBatchpassRecoveryPayloadCodec,
    RecoveryPayloadCodec,
)
from arc_core.runtime_config import RuntimeConfig
from arc_core.sqlite_db import make_session_factory, session_scope


class SqlAlchemyPlanningService:
    def __init__(
        self,
        config: RuntimeConfig,
        recovery_payload_codec: RecoveryPayloadCodec | None = None,
    ) -> None:
        self._session_factory = make_session_factory(str(config.sqlite_path))
        self._recovery_payload_codec = (
            recovery_payload_codec
            or CommandAgeBatchpassRecoveryPayloadCodec(
                command=config.recovery_payload_command,
                passphrase=config.recovery_payload_passphrase,
                work_factor=config.recovery_payload_work_factor,
                max_work_factor=config.recovery_payload_max_work_factor,
            )
        )
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
            finalized_ids = set(session.scalars(select(FinalizedImageRecord.image_id)).all())
            all_candidates = session.scalars(select(PlannedCandidateRecord)).all()
            candidates = [c for c in all_candidates if c.finalized_id not in finalized_ids]
            active_upload_collection_ids = {
                upload.collection_id
                for upload in session.scalars(select(CollectionUploadRecord)).all()
                if upload.state != "finalized"
            }
            admitted_collection_ids = {
                collection.id
                for collection in session.scalars(select(CollectionRecord)).all()
                if collection.id not in active_upload_collection_ids
                if collection.archive is not None
                and normalize_glacier_state(collection.archive.state) == GlacierState.UPLOADED
            }
            candidates = [
                candidate
                for candidate in candidates
                if all(
                    covered_path.collection_id in admitted_collection_ids
                    for covered_path in candidate.covered_paths
                )
            ]

            ref = candidates[0] if candidates else (all_candidates[0] if all_candidates else None)
            target_bytes = ref.target_bytes if ref else 0
            min_fill_bytes = ref.min_fill_bytes if ref else 0

            covered_file_pairs: set[tuple[str, str]] = set()
            for cand in all_candidates:
                for cp in cand.covered_paths:
                    covered_file_pairs.add((cp.collection_id, cp.path))

            all_files = session.scalars(select(CollectionFileRecord)).all()
            unplanned_bytes = sum(
                f.bytes for f in all_files if (f.collection_id, f.path) not in covered_file_pairs
            )

            candidate_views = [_candidate_plan_view(c, target_bytes) for c in candidates]

        if q:
            needle = q.casefold()
            candidate_views = [
                v
                for v in candidate_views
                if needle in v["candidate_id"].casefold()
                or any(needle in cid.casefold() for cid in v["_collections"])
                or any(needle in pp.casefold() for pp in v["_projected_paths"])
            ]
        if collection:
            candidate_views = [v for v in candidate_views if collection in v["_collections"]]
        if iso_ready is not None:
            candidate_views = [v for v in candidate_views if v["iso_ready"] is iso_ready]

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
                v
                for v in image_views
                if needle in v["id"].casefold()
                or needle in v["filename"].casefold()
                or any(needle in cid.casefold() for cid in v["_collection_ids"])
            ]
        if collection:
            image_views = [v for v in image_views if collection in v["_collection_ids"]]
        if has_copies is not None:
            image_views = [
                v for v in image_views if (v["physical_copies_registered"] > 0) is has_copies
            ]

        reverse = order == "desc"
        sort_key = {
            "finalized_at": lambda v: (v["id"], v["filename"]),
            "bytes": lambda v: (v["_bytes"], v["id"]),
            "physical_copies_registered": lambda v: (
                v["physical_copies_registered"],
                v["id"],
            ),
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
                    required_copy_count=2,
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
                for artifact in read_finalized_image_collection_artifacts(
                    candidate.image_root,
                    self._recovery_payload_codec,
                ):
                    session.add(
                        FinalizedImageCollectionArtifactRecord(
                            image_id=candidate.finalized_id,
                            collection_id=artifact.collection_id,
                            manifest_path=artifact.manifest_path,
                            proof_path=artifact.proof_path,
                        )
                    )
                for part in read_finalized_image_coverage_parts(
                    candidate.image_root,
                    self._recovery_payload_codec,
                ):
                    session.add(
                        FinalizedImageCoveragePartRecord(
                            image_id=candidate.finalized_id,
                            collection_id=part.collection_id,
                            path=part.path,
                            part_index=part.part_index,
                            part_count=part.part_count,
                            object_path=part.object_path,
                            sidecar_path=part.sidecar_path,
                        )
                    )
                _seed_required_copy_slots(session, image)
                session.flush()
                session.refresh(image)
                existing = image
            return _strip_internal(_finalized_image_view(existing, session))

    async def get_iso_stream(self, image_id: str) -> IsoStream:
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


class CandidatePlanView(TypedDict):
    candidate_id: str
    bytes: int
    fill: float
    files: int
    collections: int
    collection_ids: list[str]
    iso_ready: bool
    _bytes: int
    _collections: list[str]
    _projected_paths: list[str]


class FinalizedImageView(TypedDict):
    id: str
    filename: str
    finalized_at: str
    bytes: int
    fill: float
    files: int
    collections: int
    collection_ids: list[str]
    iso_ready: bool
    physical_protection_state: str
    physical_copies_required: int
    physical_copies_registered: int
    physical_copies_verified: int
    physical_copies_missing: int
    _bytes: int
    _collection_ids: list[str]


def _candidate_plan_view(candidate: PlannedCandidateRecord, target_bytes: int) -> CandidatePlanView:
    collection_ids = sorted({cp.collection_id for cp in candidate.covered_paths})
    projected_paths = sorted(f"{cp.collection_id}/{cp.path}" for cp in candidate.covered_paths)
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


def _finalized_image_view(image: FinalizedImageRecord, session: Session) -> FinalizedImageView:
    copy_rows = session.scalars(
        select(ImageCopyRecord).where(ImageCopyRecord.image_id == image.image_id)
    ).all()
    registered_copy_count = sum(
        1 for copy in copy_rows if copy_counts_toward_protection(copy.state)
    )
    verified_copy_count = sum(
        1
        for copy in copy_rows
        if copy_counts_as_verified(
            state=copy.state,
            verification_state=copy.verification_state,
        )
    )
    required_copy_count = normalize_required_copy_count(image.required_copy_count)
    protection_state = image_protection_state(
        required_copy_count=required_copy_count,
        registered_copy_count=registered_copy_count,
    )
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
        "physical_protection_state": protection_state.value,
        "physical_copies_required": required_copy_count,
        "physical_copies_registered": registered_copy_count,
        "physical_copies_verified": verified_copy_count,
        "physical_copies_missing": registered_copy_shortfall(
            required_copy_count=required_copy_count,
            registered_copy_count=registered_copy_count,
        ),
        "_bytes": image.bytes,
        "_collection_ids": collection_ids,
    }


def _seed_required_copy_slots(session: Session, image: FinalizedImageRecord) -> None:
    existing_ids = {
        copy_id
        for copy_id in session.scalars(
            select(ImageCopyRecord.copy_id).where(ImageCopyRecord.image_id == image.image_id)
        ).all()
    }
    required_copy_count = normalize_required_copy_count(image.required_copy_count)
    ordinal = 1
    while len(existing_ids) < required_copy_count:
        copy_id = f"{image.image_id}-{ordinal}"
        ordinal += 1
        if copy_id in existing_ids:
            continue
        created_at = _utc_now()
        session.add(
            ImageCopyRecord(
                image_id=image.image_id,
                copy_id=copy_id,
                label_text=copy_id,
                location=None,
                created_at=created_at,
                state=CopyState.NEEDED.value,
                verification_state=VerificationState.PENDING.value,
            )
        )
        session.flush()
        session.add(
            ImageCopyEventRecord(
                image_id=image.image_id,
                copy_id=copy_id,
                occurred_at=created_at,
                event="created",
                state=CopyState.NEEDED.value,
                verification_state=VerificationState.PENDING.value,
                location=None,
            )
        )
        existing_ids.add(copy_id)


def _utc_now() -> str:
    from datetime import UTC, datetime  # noqa: PLC0415

    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _image_id_to_finalized_at(image_id: str) -> str:
    return (
        f"{image_id[0:4]}-{image_id[4:6]}-{image_id[6:8]}"
        f"T{image_id[9:11]}:{image_id[11:13]}:{image_id[13:15]}Z"
    )


def _strip_internal(view: Mapping[str, object]) -> dict[str, object]:
    return {k: v for k, v in view.items() if not k.startswith("_")}


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
        image_lookup: Callable[[str], ImageRootRecord],
        list_lookup: Callable[..., dict[str, object]] | None = None,
        plan_lookup: Callable[..., dict[str, object]],
        finalize_lookup: Callable[[str], dict[str, object]] | None = None,
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
    ) -> dict[str, object]:
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
    ) -> dict[str, object]:
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

    def get_image(self, image_id: str) -> ImageRootRecord:
        return self._image_lookup(image_id)

    def finalize_image(self, image_id: str) -> dict[str, object]:
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
