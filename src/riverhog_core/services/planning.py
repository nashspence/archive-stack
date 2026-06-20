from __future__ import annotations

import fcntl
import hashlib
import logging
import math
import os
import subprocess
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypedDict

from sqlalchemy import func, insert, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload, sessionmaker

from riverhog_core.archive_compliance import (
    copy_counts_as_verified,
    copy_counts_toward_protection,
    image_protection_state,
    normalize_glacier_state,
    normalize_required_copy_count,
    registered_copy_shortfall,
)
from riverhog_core.catalog_db import make_session_factory, session_scope
from riverhog_core.catalog_models import (
    CandidateCoveredPathRecord,
    CollectionArchiveRecord,
    CollectionFileRecord,
    CollectionRecord,
    CollectionUploadRecord,
    FinalizedImageCollectionArtifactRecord,
    FinalizedImageCoveragePartRecord,
    FinalizedImageCoveredPathRecord,
    FinalizedImageRecord,
    ImageCopyEventRecord,
    ImageCopyRecord,
    ImageOperatorSummaryRecord,
    PlannedCandidateRecord,
)
from riverhog_core.crypto_age import (
    encrypted_size_for_plaintext_size,
    max_plaintext_size_for_encrypted_budget,
)
from riverhog_core.domain.enums import CopyState, GlacierState, VerificationState
from riverhog_core.domain.errors import InvalidState, NotFound, NotYetImplemented
from riverhog_core.finalized_image_coverage import (
    read_finalized_image_collection_artifacts,
    read_finalized_image_coverage_parts,
)
from riverhog_core.fs_paths import safe_remove_tree
from riverhog_core.iso import estimate_iso_size_from_root
from riverhog_core.iso.streaming import IsoStream, stream_iso_from_root
from riverhog_core.planner.layout import LayoutFileMeta, LayoutPiece, assign_paths, manifest_bytes
from riverhog_core.planner.manifest import (
    MANIFEST_FILENAME,
    README_FILENAME,
    PlannerFileMeta,
    assign_collection_artifact_paths,
    recovery_readme_bytes,
    sidecar_bytes,
)
from riverhog_core.ports.archive_store import ArchiveStore
from riverhog_core.ports.hot_store import HotStore
from riverhog_core.recovery_payloads import (
    CommandAgeBatchpassRecoveryPayloadCodec,
    RecoveryPayloadCodec,
    RecoveryPayloadError,
)
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.webhooks import (
    ImagesReadyBatch,
    ReadyImage,
    WebhookConfig,
    build_images_ready_payload,
    post_webhook,
    utcnow,
)

_LOG = logging.getLogger(__name__)
# These reserves intentionally estimate the represented ISO bytes, not only
# encrypted payload bytes. They are calibrated from materialized xorriso output
# and kept conservative enough to avoid overfitting one tiny-file-heavy disc.
_AGE_ENCRYPTED_HEADER_PAD_BYTES = 256
_ISO_LEAF_PAD_BYTES = 2048
_DISC_MANIFEST_ENTRY_PAD_BYTES = 256
_CANDIDATE_BASE_METADATA_PAD_BYTES = 4 * 1024 * 1024
_PLANNER_ALLOCATION_VERSION = 5
_REFRESH_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class _CollectionArtifactCache:
    collection_id: str
    manifest_bytes: bytes
    proof_bytes: bytes


@dataclass(frozen=True, slots=True)
class _PlanFile:
    collection_id: str
    path: str
    bytes: int
    sha256: str
    collection_optional_split_allowed: bool = True
    collection_required_image_count: int = 1
    collection_finalized_image_count: int = 0


@dataclass(frozen=True, slots=True)
class _PlanPiece:
    collection_id: str
    path: str
    file_id: str
    bytes: int
    sha256: str
    offset: int
    plaintext_bytes: int
    part_index: int
    part_count: int
    estimated_payload_bytes: int
    estimated_sidecar_bytes: int
    estimated_disc_manifest_bytes: int = 0

    @property
    def estimated_total_bytes(self) -> int:
        return (
            self.estimated_payload_bytes
            + self.estimated_sidecar_bytes
            + self.estimated_disc_manifest_bytes
        )


@dataclass(frozen=True, slots=True)
class _MaterializedCandidate:
    candidate_id: str
    finalized_id: str
    filename: str
    bytes: int
    iso_ready: bool
    image_root: Path
    covered_paths: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class _CandidateSpec:
    candidate_id: str
    plan_fingerprint: str
    finalized_id: str
    estimated_bytes: int
    pieces: tuple[_PlanPiece, ...]


@dataclass(frozen=True, slots=True)
class _CollectionPieceGroup:
    collection_id: str
    pieces: tuple[_PlanPiece, ...]
    estimated_bytes: int
    artifact_estimate: int
    voluntary_split: bool = False


@dataclass(slots=True)
class _CandidateBin:
    estimated_bytes: int
    collection_ids: set[str]
    groups: list[_CollectionPieceGroup]
    voluntary_split_collection_ids: set[str]


@dataclass(frozen=True, slots=True)
class _OptionalSplitMove:
    target_bin_index: int
    donor_bin_index: int
    donor_group_index: int
    collection_id: str
    moved_pieces: tuple[_PlanPiece, ...]
    moved_payload_bytes: int
    target_group_estimated_bytes: int


class SqlAlchemyPlanningService:
    def __init__(
        self,
        config: RuntimeConfig,
        hot_store: HotStore | None = None,
        archive_store: ArchiveStore | None = None,
        recovery_payload_codec: RecoveryPayloadCodec | None = None,
    ) -> None:
        self._config = config
        self._session_factory = make_session_factory(config.database_url)
        self._hot_store = hot_store
        self._archive_store = archive_store
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

    def process_due_refresh(self, *, limit: int = 1) -> int:
        if limit < 1 or self._hot_store is None:
            return 0
        processed = 0
        with session_scope(self._session_factory) as session:
            refresh_needed = _planner_refresh_needed(
                session,
                self._config,
                archive_store=self._archive_store,
            )
        if refresh_needed:
            refresh_provisional_plan(
                config=self._config,
                hot_store=self._hot_store,
                archive_store=self._archive_store,
                recovery_payload_codec=self._recovery_payload_codec,
            )
            processed = 1
        if _deliver_due_ready_candidate_notifications(
            config=self._config,
            session_factory=self._session_factory,
        ):
            processed = 1
        return processed

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
            candidates = [
                c
                for c in all_candidates
                if c.finalized_id not in finalized_ids and _candidate_state(c) == "ready"
            ]
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

            target_bytes = self._config.planner_disc_target_bytes
            min_fill_bytes = self._config.planner_min_fill_bytes

            covered_file_pairs: set[tuple[str, str]] = set()
            covered_file_pairs.update(_finalized_covered_file_pairs(session))
            for cand in candidates:
                for cp in cand.covered_paths:
                    covered_file_pairs.add((cp.collection_id, cp.path))

            all_files = session.scalars(select(CollectionFileRecord)).all()
            unplanned_bytes = sum(
                f.bytes for f in all_files if (f.collection_id, f.path) not in covered_file_pairs
            )

            candidate_views = [_candidate_plan_view(c) for c in candidates]

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
            stmt = select(ImageOperatorSummaryRecord)
            if q:
                needle = q.casefold()
                stmt = stmt.where(
                    or_(
                        func.lower(ImageOperatorSummaryRecord.image_id).contains(needle),
                        func.lower(ImageOperatorSummaryRecord.filename).contains(needle),
                        func.lower(ImageOperatorSummaryRecord.collection_ids_text).contains(needle),
                    )
                )
            if collection:
                stmt = stmt.where(_image_summary_collection_clause(collection))
            if has_copies is not None:
                copy_clause = ImageOperatorSummaryRecord.physical_copies_registered > 0
                stmt = stmt.where(copy_clause if has_copies else ~copy_clause)

            total = int(session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
            pages = math.ceil(total / per_page) if total else 0
            rows = session.scalars(
                stmt.order_by(*_image_summary_order(sort=sort, order=order))
                .offset((page - 1) * per_page)
                .limit(per_page)
            ).all()
            page_views = [_finalized_image_view_from_summary(row) for row in rows]

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
            row = session.get(ImageOperatorSummaryRecord, image_id)
            if row is None:
                raise NotFound(f"image not found: {image_id}")
            return _strip_internal(_finalized_image_view_from_summary(row))

    def finalize_image(self, candidate_id: str) -> dict[str, object]:
        try:
            return self._finalize_image_once(candidate_id)
        except IntegrityError:
            existing = self._finalized_candidate_view(candidate_id)
            if existing is None:
                raise
            _LOG.info(
                "candidate %s was finalized concurrently; returning existing image",
                candidate_id,
            )
            return existing

    def _finalized_candidate_view(self, candidate_id: str) -> dict[str, object] | None:
        with session_scope(self._session_factory) as session:
            existing = session.scalar(
                select(FinalizedImageRecord).where(
                    FinalizedImageRecord.candidate_id == candidate_id
                )
            )
            if existing is None:
                return None
            return _strip_internal(_finalized_image_view(existing, session))

    def _finalize_image_once(self, candidate_id: str) -> dict[str, object]:
        with session_scope(self._session_factory) as session:
            candidate = session.get(PlannedCandidateRecord, candidate_id)
            if candidate is None:
                raise NotFound(f"candidate not found: {candidate_id}")
            if _candidate_state(candidate) != "ready":
                raise InvalidState("image must finish materializing before finalization")
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
                session.flush()
                covered_path_rows = [
                    {
                        "image_id": candidate.finalized_id,
                        "collection_id": collection_id,
                        "path": path,
                    }
                    for collection_id, path in session.execute(
                        select(
                            CandidateCoveredPathRecord.collection_id,
                            CandidateCoveredPathRecord.path,
                        ).where(CandidateCoveredPathRecord.candidate_id == candidate_id)
                    ).all()
                ]
                if covered_path_rows:
                    session.execute(insert(FinalizedImageCoveredPathRecord), covered_path_rows)
                artifact_rows = [
                    {
                        "image_id": candidate.finalized_id,
                        "collection_id": artifact.collection_id,
                        "manifest_path": artifact.manifest_path,
                        "proof_path": artifact.proof_path,
                    }
                    for artifact in read_finalized_image_collection_artifacts(
                        candidate.image_root,
                        self._recovery_payload_codec,
                    )
                ]
                if artifact_rows:
                    session.execute(insert(FinalizedImageCollectionArtifactRecord), artifact_rows)
                coverage_part_rows = [
                    {
                        "image_id": candidate.finalized_id,
                        "collection_id": part.collection_id,
                        "path": part.path,
                        "part_index": part.part_index,
                        "part_count": part.part_count,
                        "object_path": part.object_path,
                        "sidecar_path": part.sidecar_path,
                    }
                    for part in read_finalized_image_coverage_parts(
                        candidate.image_root,
                        self._recovery_payload_codec,
                    )
                ]
                if coverage_part_rows:
                    session.execute(insert(FinalizedImageCoveragePartRecord), coverage_part_rows)
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
                bytes=record.bytes,
            )


def cache_collection_manifest_artifacts(
    config: RuntimeConfig,
    *,
    collection_id: str,
    manifest_bytes: bytes,
    proof_bytes: bytes,
) -> None:
    cache_dir = _collection_artifact_cache_dir(config, collection_id)
    cache_dir.mkdir(parents=True, exist_ok=True)
    _write_bytes_atomic(cache_dir / "collection-id.txt", collection_id.encode("utf-8"))
    _write_bytes_atomic(cache_dir / "manifest.yml", manifest_bytes)
    _write_bytes_atomic(cache_dir / "manifest.yml.ots", proof_bytes)


def _restore_collection_artifact_cache(
    *,
    config: RuntimeConfig,
    archive_store: ArchiveStore,
    collection_id: str,
    archive: CollectionArchiveRecord,
) -> _CollectionArtifactCache | None:
    if not archive.manifest_object_path or not archive.ots_object_path:
        return None
    try:
        manifest_bytes = archive_store.read_restored_collection_manifest(
            collection_id=collection_id,
            object_path=archive.manifest_object_path,
        )
        proof_bytes = archive_store.read_restored_collection_manifest_proof(
            collection_id=collection_id,
            object_path=archive.ots_object_path,
        )
    except Exception:
        _LOG.warning(
            "failed to restore cached archive artifacts for %s",
            collection_id,
            exc_info=True,
        )
        return None
    if (
        archive.manifest_sha256
        and hashlib.sha256(manifest_bytes).hexdigest() != archive.manifest_sha256
    ):
        _LOG.warning("restored collection manifest sha256 mismatch for %s", collection_id)
        return None
    if archive.ots_sha256 and hashlib.sha256(proof_bytes).hexdigest() != archive.ots_sha256:
        _LOG.warning("restored collection proof sha256 mismatch for %s", collection_id)
        return None
    cache_collection_manifest_artifacts(
        config,
        collection_id=collection_id,
        manifest_bytes=manifest_bytes,
        proof_bytes=proof_bytes,
    )
    return _CollectionArtifactCache(
        collection_id=collection_id,
        manifest_bytes=manifest_bytes,
        proof_bytes=proof_bytes,
    )


def refresh_provisional_plan(
    *,
    config: RuntimeConfig,
    hot_store: HotStore,
    archive_store: ArchiveStore | None = None,
    recovery_payload_codec: RecoveryPayloadCodec,
) -> None:
    if not _REFRESH_LOCK.acquire(blocking=False):
        _LOG.info("skipping provisional plan refresh because one is already running")
        return
    started = time.perf_counter()
    try:
        with _planner_refresh_file_lock(config) as acquired:
            if not acquired:
                _LOG.info(
                    "skipping provisional plan refresh because another process holds the lock"
                )
                return
            _LOG.info("planner refresh started")
            _refresh_provisional_plan_locked(
                config=config,
                hot_store=hot_store,
                archive_store=archive_store,
                recovery_payload_codec=recovery_payload_codec,
            )
            _LOG.info(
                "planner refresh completed in %.1fs",
                time.perf_counter() - started,
            )
    finally:
        _REFRESH_LOCK.release()


@contextmanager
def _planner_refresh_file_lock(config: RuntimeConfig) -> Iterator[bool]:
    lock_root = config.planner_image_root
    lock_root.mkdir(parents=True, exist_ok=True)
    lock_path = lock_root / ".planner-refresh.lock"
    with lock_path.open("a+") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _refresh_provisional_plan_locked(
    *,
    config: RuntimeConfig,
    hot_store: HotStore,
    archive_store: ArchiveStore | None,
    recovery_payload_codec: RecoveryPayloadCodec,
) -> None:
    session_factory = make_session_factory(config.database_url)
    with session_scope(session_factory) as session:
        plan_files = _load_plan_files(session, config, archive_store=archive_store)
        plan_collection_ids = {file.collection_id for file in plan_files}
        optional_split_collection_ids = {
            file.collection_id for file in plan_files if file.collection_optional_split_allowed
        }
        _LOG.info(
            "planner refresh loaded plan files: collections=%s optional_split_eligible=%s "
            "files=%s bytes=%s",
            len(plan_collection_ids),
            len(optional_split_collection_ids),
            len(plan_files),
            sum(file.bytes for file in plan_files),
        )
        piece_groups = _build_plan_piece_groups(plan_files, config)
        _LOG.info(
            "planner refresh packed candidate groups: candidates=%s estimated_bytes=%s",
            len(piece_groups),
            [_candidate_estimated_bytes(tuple(pieces), config) for pieces in piece_groups],
        )
        desired_ids = {
            _candidate_id_from_fingerprint(_candidate_plan_fingerprint(pieces, config))
            for pieces in piece_groups
        }
        old_image_roots = _delete_stale_provisional_candidates(session, keep_ids=desired_ids)
        specs = _ensure_candidate_specs(session, config=config, piece_groups=piece_groups)
        _LOG.info(
            "planner refresh synchronized candidate specs: specs=%s stale_roots=%s",
            len(specs),
            len(old_image_roots),
        )

    _remove_provisional_image_roots(config, old_image_roots)
    candidates_root = _candidate_image_root(config)
    candidates_root.mkdir(parents=True, exist_ok=True)

    for spec in specs:
        if spec.estimated_bytes < config.planner_min_fill_bytes:
            with session_scope(session_factory) as session:
                record = session.get(PlannedCandidateRecord, spec.candidate_id)
                if record is None:
                    continue
                record.state = "waiting"
                record.failure = None
                record.bytes = 0
                record.iso_ready = False
                record.updated_at = _utc_now()
                image_root = record.image_root
            _LOG.info(
                "planner candidate %s is waiting for more data: "
                "estimated_bytes=%s min_fill_bytes=%s",
                spec.candidate_id,
                spec.estimated_bytes,
                config.planner_min_fill_bytes,
            )
            _remove_provisional_image_roots(config, [image_root])
            continue

        with session_scope(session_factory) as session:
            record = session.get(PlannedCandidateRecord, spec.candidate_id)
            if record is None:
                continue
            if _candidate_state(record) == "ready" and Path(record.image_root).exists():
                _LOG.info(
                    "planner candidate %s is already ready: bytes=%s iso_ready=%s",
                    spec.candidate_id,
                    record.bytes,
                    record.iso_ready,
                )
                continue
            finalized_ids = set(session.scalars(select(FinalizedImageRecord.image_id)).all())
            if record.finalized_id in finalized_ids:
                continue
            record.state = "materializing"
            record.failure = None
            record.updated_at = _utc_now()

        try:
            _LOG.info(
                "planner candidate %s materialization started: finalized_id=%s "
                "estimated_bytes=%s files=%s collections=%s",
                spec.candidate_id,
                spec.finalized_id,
                spec.estimated_bytes,
                len(spec.pieces),
                len({piece.collection_id for piece in spec.pieces}),
            )
            materialized = _materialize_candidate(
                config=config,
                hot_store=hot_store,
                recovery_payload_codec=recovery_payload_codec,
                candidate_id=spec.candidate_id,
                finalized_id=spec.finalized_id,
                pieces=spec.pieces,
                candidates_root=candidates_root,
            )
        except Exception as exc:
            with session_scope(session_factory) as session:
                record = session.get(PlannedCandidateRecord, spec.candidate_id)
                if record is not None:
                    record.state = "failed"
                    record.failure = str(exc).strip() or exc.__class__.__name__
                    record.updated_at = _utc_now()
            raise

        with session_scope(session_factory) as session:
            finalized_ids = set(session.scalars(select(FinalizedImageRecord.image_id)).all())
            record = session.get(PlannedCandidateRecord, spec.candidate_id)
            if record is None or materialized.finalized_id in finalized_ids:
                continue
            record.finalized_id = materialized.finalized_id
            record.filename = materialized.filename
            record.bytes = materialized.bytes
            record.iso_ready = materialized.iso_ready
            record.image_root = str(materialized.image_root)
            record.target_bytes = config.planner_disc_target_bytes
            record.min_fill_bytes = config.planner_min_fill_bytes
            record.plan_fingerprint = spec.plan_fingerprint
            record.state = "ready"
            record.failure = None
            record.updated_at = _utc_now()
            _LOG.info(
                "planner candidate %s materialization completed: bytes=%s iso_ready=%s",
                materialized.candidate_id,
                materialized.bytes,
                materialized.iso_ready,
            )


def _collection_artifact_cache_root(config: RuntimeConfig) -> Path:
    return config.planner_image_root / "collection-artifacts"


def _candidate_image_root(config: RuntimeConfig) -> Path:
    return config.planner_image_root / "candidates"


def _collection_artifact_cache_dir(config: RuntimeConfig, collection_id: str) -> Path:
    digest = hashlib.sha256(collection_id.encode("utf-8")).hexdigest()
    return _collection_artifact_cache_root(config) / digest


def _read_collection_artifact_cache(
    config: RuntimeConfig,
    collection_id: str,
) -> _CollectionArtifactCache | None:
    cache_dir = _collection_artifact_cache_dir(config, collection_id)
    manifest_path = cache_dir / "manifest.yml"
    proof_path = cache_dir / "manifest.yml.ots"
    if not manifest_path.exists() or not proof_path.exists():
        return None
    return _CollectionArtifactCache(
        collection_id=collection_id,
        manifest_bytes=manifest_path.read_bytes(),
        proof_bytes=proof_path.read_bytes(),
    )


def _write_bytes_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_bytes(content)
    os.replace(tmp_path, path)


def _ensure_candidate_specs(
    session: Session,
    *,
    config: RuntimeConfig,
    piece_groups: Sequence[Sequence[_PlanPiece]],
) -> list[_CandidateSpec]:
    reserved_finalized_ids, _reserved_candidate_ids = _reserved_plan_ids(session)
    specs: list[_CandidateSpec] = []
    current_time = _utc_now()
    candidates_root = _candidate_image_root(config)
    for pieces in piece_groups:
        piece_tuple = tuple(pieces)
        fingerprint = _candidate_plan_fingerprint(piece_tuple, config)
        candidate_id = _candidate_id_from_fingerprint(fingerprint)
        estimated_bytes = _candidate_estimated_bytes(piece_tuple, config)
        record = session.get(PlannedCandidateRecord, candidate_id)
        if record is None:
            finalized_id = _next_finalized_id(reserved_finalized_ids)
            reserved_finalized_ids.add(finalized_id)
            record = PlannedCandidateRecord(
                candidate_id=candidate_id,
                finalized_id=finalized_id,
                plan_fingerprint=fingerprint,
                state="materializing",
                failure=None,
                updated_at=current_time,
                filename=f"{finalized_id}.iso",
                bytes=0,
                iso_ready=False,
                image_root=str(candidates_root / candidate_id),
                target_bytes=config.planner_disc_target_bytes,
                min_fill_bytes=config.planner_min_fill_bytes,
            )
            session.add(record)
            for collection_id, path in _covered_paths_for_pieces(piece_tuple):
                record.covered_paths.append(
                    CandidateCoveredPathRecord(
                        candidate_id=candidate_id,
                        collection_id=collection_id,
                        path=path,
                    )
                )
        else:
            finalized_id = record.finalized_id
            reserved_finalized_ids.add(finalized_id)
            record.plan_fingerprint = fingerprint
            record.target_bytes = config.planner_disc_target_bytes
            record.min_fill_bytes = config.planner_min_fill_bytes
            record.updated_at = record.updated_at or current_time
        specs.append(
            _CandidateSpec(
                candidate_id=candidate_id,
                plan_fingerprint=fingerprint,
                finalized_id=finalized_id,
                estimated_bytes=estimated_bytes,
                pieces=piece_tuple,
            )
        )
    return specs


def _delete_stale_provisional_candidates(session: Session, *, keep_ids: set[str]) -> list[str]:
    finalized_ids = set(session.scalars(select(FinalizedImageRecord.image_id)).all())
    candidates = session.scalars(select(PlannedCandidateRecord)).all()
    image_roots: list[str] = []
    for candidate in candidates:
        if candidate.finalized_id in finalized_ids:
            continue
        if candidate.candidate_id in keep_ids:
            continue
        image_roots.append(candidate.image_root)
        session.delete(candidate)
    session.flush()
    return image_roots


def _remove_provisional_image_roots(config: RuntimeConfig, image_roots: Sequence[str]) -> None:
    candidates_root = _candidate_image_root(config).resolve()
    for raw_root in image_roots:
        image_root = Path(raw_root).expanduser().resolve()
        if image_root == candidates_root or not image_root.is_relative_to(candidates_root):
            _LOG.warning("skipping non-provisional image root cleanup: %s", image_root)
            continue
        safe_remove_tree(image_root)
        tmp_root = image_root.with_name(f".{image_root.name}.tmp")
        if tmp_root != candidates_root and tmp_root.is_relative_to(candidates_root):
            safe_remove_tree(tmp_root)


def _reserved_plan_ids(session: Session) -> tuple[set[str], set[str]]:
    finalized_ids = set(session.scalars(select(FinalizedImageRecord.image_id)).all())
    planned_finalized_ids = set(session.scalars(select(PlannedCandidateRecord.finalized_id)).all())
    candidate_ids = set(session.scalars(select(PlannedCandidateRecord.candidate_id)).all())
    return finalized_ids | planned_finalized_ids, candidate_ids


def _candidate_state(candidate: PlannedCandidateRecord) -> str:
    return candidate.state or "ready"


def _candidate_estimated_bytes(pieces: Sequence[_PlanPiece], config: RuntimeConfig) -> int:
    collection_ids = {piece.collection_id for piece in pieces}
    return (
        sum(piece.estimated_total_bytes for piece in pieces)
        + sum(
            _collection_artifact_estimate(config, collection_id) for collection_id in collection_ids
        )
        + _candidate_metadata_pad(config.planner_disc_target_bytes)
    )


def _covered_paths_for_pieces(pieces: Sequence[_PlanPiece]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted({(piece.collection_id, piece.path) for piece in pieces}))


def _candidate_plan_fingerprint(
    pieces: Sequence[_PlanPiece],
    config: RuntimeConfig,
) -> str:
    digest = hashlib.sha256()
    digest.update(f"allocation_version\t{_PLANNER_ALLOCATION_VERSION}\n".encode())
    digest.update(f"target\t{config.planner_disc_target_bytes}\n".encode())
    digest.update(f"min\t{config.planner_min_fill_bytes}\n".encode())
    for piece in pieces:
        digest.update(
            (
                f"{piece.collection_id}\t{piece.path}\t{piece.sha256}\t{piece.bytes}\t"
                f"{piece.part_index}\t{piece.part_count}\t{piece.offset}\t"
                f"{piece.plaintext_bytes}\n"
            ).encode()
        )
    return digest.hexdigest()


def _candidate_id_from_fingerprint(fingerprint: str) -> str:
    return f"candidate-{fingerprint[:24]}"


def _planner_refresh_needed(
    session: Session,
    config: RuntimeConfig,
    *,
    archive_store: ArchiveStore | None = None,
) -> bool:
    plan_files = _load_plan_files(session, config, archive_store=archive_store)
    plan_pairs = {(file.collection_id, file.path) for file in plan_files}
    candidates = [
        candidate
        for candidate in session.scalars(select(PlannedCandidateRecord)).all()
        if session.get(FinalizedImageRecord, candidate.finalized_id) is None
    ]
    if not plan_pairs:
        return bool(candidates)
    if any(_candidate_state(candidate) not in {"ready", "waiting"} for candidate in candidates):
        return True
    if any(
        candidate.target_bytes != config.planner_disc_target_bytes
        or candidate.min_fill_bytes != config.planner_min_fill_bytes
        for candidate in candidates
    ):
        return True
    covered_pairs = {
        (covered.collection_id, covered.path)
        for candidate in candidates
        for covered in candidate.covered_paths
    }
    if not plan_pairs.issubset(covered_pairs):
        return True
    desired_fingerprints = {
        _candidate_plan_fingerprint(pieces, config)
        for pieces in _build_plan_piece_groups(plan_files, config)
    }
    candidate_fingerprints = {candidate.plan_fingerprint for candidate in candidates}
    return desired_fingerprints != candidate_fingerprints


def _deliver_due_ready_candidate_notifications(
    *,
    config: RuntimeConfig,
    session_factory: sessionmaker[Session],
) -> int:
    if not config.operator_webhook_url:
        return 0
    current = utcnow()
    current_text = _isoformat_z(current)
    with session_scope(session_factory) as session:
        due_candidates = [
            candidate
            for candidate in _ready_notification_candidates(session)
            if _ready_notification_due(candidate, current_text)
        ]
    delivered = 0
    for candidate_id in [candidate.candidate_id for candidate in due_candidates]:
        with session_scope(session_factory) as session:
            candidate = session.get(PlannedCandidateRecord, candidate_id)
            if candidate is None or not _candidate_is_ready_for_notification(
                session,
                candidate,
            ):
                continue
            batch = _candidate_ready_notification_batch(candidate)
        try:
            webhook_config = WebhookConfig(
                url=config.operator_webhook_url,
                base_url=config.public_base_url or "",
                timeout_seconds=config.operator_webhook_timeout.total_seconds(),
                retry_seconds=config.operator_webhook_retry_delay.total_seconds(),
                reminder_interval_seconds=config.operator_webhook_reminder_interval.total_seconds(),
            )
            post_webhook(
                config=webhook_config,
                payload=build_images_ready_payload(
                    config=webhook_config,
                    batch=batch,
                    delivered_at=current,
                ),
            )
        except Exception as exc:
            with session_scope(session_factory) as session:
                candidate = session.get(PlannedCandidateRecord, candidate_id)
                if candidate is not None:
                    candidate.ready_notification_failure = (
                        str(exc).strip() or exc.__class__.__name__
                    )
                    candidate.ready_notification_next_attempt_at = _isoformat_z(
                        current + config.operator_webhook_retry_delay
                    )
            _LOG.warning(
                "failed to deliver ready image webhook for %s",
                candidate_id,
                exc_info=True,
            )
            continue

        with session_scope(session_factory) as session:
            candidate = session.get(PlannedCandidateRecord, candidate_id)
            if candidate is None:
                continue
            if candidate.ready_notification_sent_at is None:
                candidate.ready_notification_sent_at = current_text
            candidate.ready_notification_count = int(candidate.ready_notification_count or 0) + 1
            candidate.ready_notification_failure = None
            if config.operator_webhook_reminder_interval.total_seconds() > 0:
                candidate.ready_notification_next_attempt_at = _isoformat_z(
                    current + config.operator_webhook_reminder_interval
                )
            else:
                candidate.ready_notification_next_attempt_at = None
        delivered += 1
    return delivered


def _ready_notification_candidates(session: Session) -> list[PlannedCandidateRecord]:
    finalized_ids = set(session.scalars(select(FinalizedImageRecord.image_id)).all())
    return [
        candidate
        for candidate in session.scalars(select(PlannedCandidateRecord)).all()
        if candidate.finalized_id not in finalized_ids
        if _candidate_state(candidate) == "ready"
        if candidate.iso_ready
    ]


def _candidate_is_ready_for_notification(
    session: Session,
    candidate: PlannedCandidateRecord,
) -> bool:
    if session.get(FinalizedImageRecord, candidate.finalized_id) is not None:
        return False
    return _candidate_state(candidate) == "ready" and bool(candidate.iso_ready)


def _ready_notification_due(candidate: PlannedCandidateRecord, current_text: str) -> bool:
    if candidate.ready_notification_sent_at is None:
        return True
    next_attempt = candidate.ready_notification_next_attempt_at
    return next_attempt is not None and next_attempt <= current_text


def _candidate_ready_notification_batch(
    candidate: PlannedCandidateRecord,
) -> ImagesReadyBatch:
    notification_count = int(candidate.ready_notification_count or 0)
    return ImagesReadyBatch(
        batch_id=f"candidate-ready-{candidate.candidate_id}",
        images=[
            ReadyImage(
                image_id=candidate.finalized_id,
                filename=candidate.filename,
                iso_available=True,
            )
        ],
        reminder_count=max(0, notification_count - 1),
        initial_sent_at=_parse_isoformat_z(candidate.ready_notification_sent_at),
        next_attempt_at=_parse_isoformat_z(candidate.ready_notification_next_attempt_at),
    )


def _load_plan_files(
    session: Session,
    config: RuntimeConfig,
    *,
    archive_store: ArchiveStore | None = None,
) -> list[_PlanFile]:
    finalized_paths = _finalized_covered_file_pairs(session)
    finalized_collection_ids = {collection_id for collection_id, _path in finalized_paths}
    finalized_image_counts = _finalized_image_counts_by_collection(session)
    collections = session.scalars(
        select(CollectionRecord)
        .options(selectinload(CollectionRecord.files))
        .options(selectinload(CollectionRecord.archive))
        .order_by(CollectionRecord.id.asc())
    ).all()

    plan_files: list[_PlanFile] = []
    for collection in collections:
        if collection.archive is None:
            continue
        if normalize_glacier_state(collection.archive.state) is not GlacierState.UPLOADED:
            continue
        unfinalized_hot_files = [
            file_record
            for file_record in sorted(collection.files, key=lambda current: current.path)
            if file_record.hot and (collection.id, file_record.path) not in finalized_paths
        ]
        if not unfinalized_hot_files:
            continue
        artifact = _read_collection_artifact_cache(config, collection.id)
        if artifact is None and archive_store is not None:
            artifact = _restore_collection_artifact_cache(
                config=config,
                archive_store=archive_store,
                collection_id=collection.id,
                archive=collection.archive,
            )
        if artifact is None:
            _LOG.warning(
                "skipping collection %s during planning because archive artifacts are not cached",
                collection.id,
            )
            continue
        artifact_estimate = _collection_artifact_bytes_estimate(artifact)
        collection_optional_split_allowed = (
            collection.id not in finalized_collection_ids
            and _collection_fits_single_image(
                collection.files,
                config=config,
                artifact_estimate=artifact_estimate,
            )
        )
        collection_required_image_count = _collection_required_image_count(
            collection.files,
            config=config,
            artifact_estimate=artifact_estimate,
        )
        collection_finalized_image_count = finalized_image_counts.get(collection.id, 0)
        for file_record in unfinalized_hot_files:
            plan_files.append(
                _PlanFile(
                    collection_id=collection.id,
                    path=file_record.path,
                    bytes=file_record.bytes,
                    sha256=file_record.sha256,
                    collection_optional_split_allowed=collection_optional_split_allowed,
                    collection_required_image_count=collection_required_image_count,
                    collection_finalized_image_count=collection_finalized_image_count,
                )
            )
    return plan_files


def _finalized_image_counts_by_collection(session: Session) -> dict[str, int]:
    rows = session.execute(
        select(
            FinalizedImageCoveredPathRecord.collection_id,
            func.count(func.distinct(FinalizedImageCoveredPathRecord.image_id)),
        ).group_by(FinalizedImageCoveredPathRecord.collection_id)
    ).all()
    return {str(collection_id): int(count) for collection_id, count in rows}


def _collection_fits_single_image(
    files: Sequence[CollectionFileRecord],
    *,
    config: RuntimeConfig,
    artifact_estimate: int,
) -> bool:
    max_piece_plaintext_bytes = _max_piece_plaintext_bytes(config.planner_disc_target_bytes)
    payload_capacity = max(
        1,
        config.planner_disc_target_bytes
        - _candidate_metadata_pad(config.planner_disc_target_bytes),
    )
    estimated_bytes = artifact_estimate
    for file_record in files:
        piece_count = max(1, math.ceil(file_record.bytes / max_piece_plaintext_bytes))
        if piece_count != 1:
            return False
        sidecar = sidecar_bytes(
            _planner_file_meta(
                path=file_record.path,
                bytes=file_record.bytes,
                sha256=file_record.sha256,
            ),
            collection_id=file_record.collection_id,
            part_index=0,
            part_count=1,
        )
        estimated_bytes += _estimated_encrypted_leaf_size(file_record.bytes)
        estimated_bytes += _estimated_encrypted_leaf_size(len(sidecar))
        estimated_bytes += _DISC_MANIFEST_ENTRY_PAD_BYTES
        if estimated_bytes > payload_capacity:
            return False
    return True


def _collection_required_image_count(
    files: Sequence[CollectionFileRecord],
    *,
    config: RuntimeConfig,
    artifact_estimate: int,
) -> int:
    max_piece_plaintext_bytes = _max_piece_plaintext_bytes(config.planner_disc_target_bytes)
    payload_capacity = max(
        1,
        config.planner_disc_target_bytes
        - _candidate_metadata_pad(config.planner_disc_target_bytes),
    )
    pieces: list[_PlanPiece] = []
    for file_record in files:
        piece_count = max(1, math.ceil(file_record.bytes / max_piece_plaintext_bytes))
        for part_index in range(piece_count):
            offset = part_index * max_piece_plaintext_bytes
            plaintext_bytes = max(0, min(max_piece_plaintext_bytes, file_record.bytes - offset))
            sidecar = sidecar_bytes(
                _planner_file_meta(
                    path=file_record.path,
                    bytes=file_record.bytes,
                    sha256=file_record.sha256,
                ),
                collection_id=file_record.collection_id,
                part_index=part_index,
                part_count=piece_count,
            )
            pieces.append(
                _PlanPiece(
                    collection_id=file_record.collection_id,
                    path=file_record.path,
                    file_id=f"{file_record.collection_id}\0{file_record.path}",
                    bytes=file_record.bytes,
                    sha256=file_record.sha256,
                    offset=offset,
                    plaintext_bytes=plaintext_bytes,
                    part_index=part_index,
                    part_count=piece_count,
                    estimated_payload_bytes=_estimated_encrypted_leaf_size(plaintext_bytes),
                    estimated_sidecar_bytes=_estimated_encrypted_leaf_size(len(sidecar)),
                    estimated_disc_manifest_bytes=_DISC_MANIFEST_ENTRY_PAD_BYTES,
                )
            )
    return max(
        1,
        len(
            _build_collection_piece_groups(
                pieces,
                artifact_estimate=artifact_estimate,
                payload_capacity=payload_capacity,
            )
        ),
    )


def _finalized_covered_file_pairs(session: Session) -> set[tuple[str, str]]:
    return {
        (str(collection_id), str(path))
        for collection_id, path in session.execute(
            select(
                FinalizedImageCoveredPathRecord.collection_id,
                FinalizedImageCoveredPathRecord.path,
            )
        ).all()
    }


def _build_plan_piece_groups(
    plan_files: Sequence[_PlanFile],
    config: RuntimeConfig,
) -> list[list[_PlanPiece]]:
    if not plan_files:
        return []

    max_piece_plaintext_bytes = _max_piece_plaintext_bytes(config.planner_disc_target_bytes)
    pieces: list[_PlanPiece] = []
    for plan_file in plan_files:
        piece_count = max(1, math.ceil(plan_file.bytes / max_piece_plaintext_bytes))
        for part_index in range(piece_count):
            offset = part_index * max_piece_plaintext_bytes
            plaintext_bytes = max(0, min(max_piece_plaintext_bytes, plan_file.bytes - offset))
            meta = _planner_file_meta(
                path=plan_file.path,
                bytes=plan_file.bytes,
                sha256=plan_file.sha256,
            )
            sidecar = sidecar_bytes(
                meta,
                collection_id=plan_file.collection_id,
                part_index=part_index,
                part_count=piece_count,
            )
            pieces.append(
                _PlanPiece(
                    collection_id=plan_file.collection_id,
                    path=plan_file.path,
                    file_id=f"{plan_file.collection_id}\0{plan_file.path}",
                    bytes=plan_file.bytes,
                    sha256=plan_file.sha256,
                    offset=offset,
                    plaintext_bytes=plaintext_bytes,
                    part_index=part_index,
                    part_count=piece_count,
                    estimated_payload_bytes=_estimated_encrypted_leaf_size(plaintext_bytes),
                    estimated_sidecar_bytes=_estimated_encrypted_leaf_size(len(sidecar)),
                    estimated_disc_manifest_bytes=_DISC_MANIFEST_ENTRY_PAD_BYTES,
                )
            )

    pieces_by_collection: dict[str, list[_PlanPiece]] = {}
    for piece in sorted(pieces, key=lambda p: (p.collection_id, p.path, p.part_index)):
        pieces_by_collection.setdefault(piece.collection_id, []).append(piece)
    artifact_estimates = {
        collection_id: _collection_artifact_estimate(config, collection_id)
        for collection_id in sorted({piece.collection_id for piece in pieces})
    }
    optional_split_collection_ids = {
        plan_file.collection_id
        for plan_file in plan_files
        if plan_file.collection_optional_split_allowed
    }
    collection_required_image_counts = {
        plan_file.collection_id: plan_file.collection_required_image_count
        for plan_file in plan_files
    }
    collection_finalized_image_counts = {
        plan_file.collection_id: plan_file.collection_finalized_image_count
        for plan_file in plan_files
    }
    metadata_pad = _candidate_metadata_pad(config.planner_disc_target_bytes)
    payload_capacity = max(1, config.planner_disc_target_bytes - metadata_pad)
    minimum_payload_fill = max(1, config.planner_min_fill_bytes - metadata_pad)
    collection_groups: list[_CollectionPieceGroup] = []
    for collection_id in sorted(pieces_by_collection):
        collection_groups.extend(
            _build_collection_piece_groups(
                pieces_by_collection[collection_id],
                artifact_estimate=artifact_estimates[collection_id],
                payload_capacity=payload_capacity,
            )
        )
    return _pack_collection_piece_groups(
        collection_groups,
        payload_capacity=payload_capacity,
        minimum_payload_fill=minimum_payload_fill,
        optionally_splittable_collections=optional_split_collection_ids,
        collection_required_image_counts=collection_required_image_counts,
        collection_finalized_image_counts=collection_finalized_image_counts,
        saturation_threshold_bytes=config.planner_unplanned_saturation_bytes,
    )


def _build_collection_piece_groups(
    pieces: Sequence[_PlanPiece],
    *,
    artifact_estimate: int,
    payload_capacity: int,
) -> list[_CollectionPieceGroup]:
    if not pieces:
        return []

    collection_id = pieces[0].collection_id
    bins: list[tuple[int, list[_PlanPiece]]] = []
    for piece in sorted(
        pieces,
        key=lambda p: (-p.estimated_total_bytes, p.path, p.part_index),
    ):
        best_index: int | None = None
        best_remaining: int | None = None
        for idx, (estimated_bytes, _bin_pieces) in enumerate(bins):
            next_bytes = estimated_bytes + piece.estimated_total_bytes
            if next_bytes > payload_capacity:
                continue
            remaining = payload_capacity - next_bytes
            if best_remaining is None or remaining < best_remaining:
                best_index = idx
                best_remaining = remaining

        if best_index is None:
            bins.append((artifact_estimate + piece.estimated_total_bytes, [piece]))
            continue

        estimated_bytes, bin_pieces = bins[best_index]
        bin_pieces.append(piece)
        bins[best_index] = (estimated_bytes + piece.estimated_total_bytes, bin_pieces)

    return [
        _CollectionPieceGroup(
            collection_id=collection_id,
            pieces=tuple(sorted(bin_pieces, key=lambda p: (p.path, p.part_index))),
            estimated_bytes=estimated_bytes,
            artifact_estimate=artifact_estimate,
        )
        for estimated_bytes, bin_pieces in bins
    ]


def _pack_collection_piece_groups(
    collection_groups: Sequence[_CollectionPieceGroup],
    *,
    payload_capacity: int,
    minimum_payload_fill: int = 1,
    optionally_splittable_collections: set[str] | None = None,
    collection_required_image_counts: Mapping[str, int] | None = None,
    collection_finalized_image_counts: Mapping[str, int] | None = None,
    saturation_threshold_bytes: int = 0,
) -> list[list[_PlanPiece]]:
    collection_group_counts: dict[str, int] = {}
    for group in collection_groups:
        collection_group_counts[group.collection_id] = (
            collection_group_counts.get(group.collection_id, 0) + 1
        )
    eligible_collection_ids = (
        set(collection_group_counts)
        if optionally_splittable_collections is None
        else optionally_splittable_collections
    )
    optional_collection_ids = {
        group.collection_id
        for group in collection_groups
        if group.collection_id in eligible_collection_ids
        if collection_group_counts[group.collection_id] == 1
        if len(group.pieces) > 1
        if all(piece.part_count == 1 for piece in group.pieces)
    }
    required_counts = {
        collection_id: int((collection_required_image_counts or {}).get(collection_id, group_count))
        for collection_id, group_count in collection_group_counts.items()
    }
    finalized_counts = {
        collection_id: int((collection_finalized_image_counts or {}).get(collection_id, 0))
        for collection_id in collection_group_counts
    }
    voluntary_split_counts = {
        collection_id: max(
            0,
            finalized_counts[collection_id]
            + collection_group_counts[collection_id]
            - required_counts[collection_id],
        )
        for collection_id in collection_group_counts
    }

    candidate_bins: list[_CandidateBin] = []
    for group in sorted(
        collection_groups,
        key=lambda g: (-g.estimated_bytes, g.collection_id, g.pieces[0].path),
    ):
        best_index: int | None = None
        best_remaining: int | None = None
        for idx, candidate_bin in enumerate(candidate_bins):
            if group.collection_id in candidate_bin.collection_ids:
                continue
            next_bytes = candidate_bin.estimated_bytes + group.estimated_bytes
            if next_bytes > payload_capacity:
                continue
            remaining = payload_capacity - next_bytes
            if best_remaining is None or remaining < best_remaining:
                best_index = idx
                best_remaining = remaining

        if best_index is None:
            candidate_bins.append(
                _CandidateBin(
                    estimated_bytes=group.estimated_bytes,
                    collection_ids={group.collection_id},
                    groups=[group],
                    voluntary_split_collection_ids=set(),
                )
            )
            continue

        candidate_bin = candidate_bins[best_index]
        candidate_bin.estimated_bytes += group.estimated_bytes
        candidate_bin.collection_ids.add(group.collection_id)
        candidate_bin.groups.append(group)

    _apply_optional_whole_file_collection_splits(
        candidate_bins,
        optionally_splittable_collections=optional_collection_ids,
        voluntary_split_counts=voluntary_split_counts,
        payload_capacity=payload_capacity,
        minimum_payload_fill=minimum_payload_fill,
    )
    _apply_saturation_whole_file_collection_splits(
        candidate_bins,
        voluntary_split_counts=voluntary_split_counts,
        payload_capacity=payload_capacity,
        minimum_payload_fill=minimum_payload_fill,
        saturation_threshold_bytes=saturation_threshold_bytes,
    )

    return [
        sorted(
            [piece for group in candidate_bin.groups for piece in group.pieces],
            key=lambda p: (p.collection_id, p.path, p.part_index),
        )
        for candidate_bin in candidate_bins
    ]


def _apply_optional_whole_file_collection_splits(
    candidate_bins: list[_CandidateBin],
    *,
    optionally_splittable_collections: set[str],
    voluntary_split_counts: dict[str, int],
    payload_capacity: int,
    minimum_payload_fill: int,
) -> None:
    split_collection_ids = {
        collection_id for collection_id, count in voluntary_split_counts.items() if count >= 1
    }
    while True:
        move = _find_optional_split_move(
            candidate_bins,
            optionally_splittable_collections=optionally_splittable_collections,
            split_collection_ids=split_collection_ids,
            payload_capacity=payload_capacity,
            minimum_payload_fill=minimum_payload_fill,
        )
        if move is None:
            return
        _apply_optional_split_move(candidate_bins, move)
        split_collection_ids.add(move.collection_id)
        voluntary_split_counts[move.collection_id] = (
            voluntary_split_counts.get(move.collection_id, 0) + 1
        )


def _apply_saturation_whole_file_collection_splits(
    candidate_bins: list[_CandidateBin],
    *,
    voluntary_split_counts: dict[str, int],
    payload_capacity: int,
    minimum_payload_fill: int,
    saturation_threshold_bytes: int,
) -> None:
    if saturation_threshold_bytes <= 0:
        return
    while (
        _waiting_candidate_bytes(candidate_bins, minimum_payload_fill) > saturation_threshold_bytes
    ):
        move = _find_saturation_split_move(
            candidate_bins,
            voluntary_split_counts=voluntary_split_counts,
            payload_capacity=payload_capacity,
            minimum_payload_fill=minimum_payload_fill,
        )
        if move is None:
            _LOG.info(
                "planner saturation found no beneficial whole-file split: "
                "waiting_bytes=%s threshold=%s",
                _waiting_candidate_bytes(candidate_bins, minimum_payload_fill),
                saturation_threshold_bytes,
            )
            return
        before = _waiting_candidate_bytes(candidate_bins, minimum_payload_fill)
        _apply_optional_split_move(candidate_bins, move)
        voluntary_split_counts[move.collection_id] = (
            voluntary_split_counts.get(move.collection_id, 0) + 1
        )
        after = _waiting_candidate_bytes(candidate_bins, minimum_payload_fill)
        _LOG.info(
            "planner saturation split applied: collection=%s unnecessary_split_count=%s "
            "waiting_bytes=%s->%s threshold=%s",
            move.collection_id,
            voluntary_split_counts[move.collection_id],
            before,
            after,
            saturation_threshold_bytes,
        )


def _waiting_candidate_bytes(
    candidate_bins: Sequence[_CandidateBin],
    minimum_payload_fill: int,
) -> int:
    return sum(
        candidate_bin.estimated_bytes
        for candidate_bin in candidate_bins
        if candidate_bin.estimated_bytes < minimum_payload_fill
    )


def _find_saturation_split_move(
    candidate_bins: Sequence[_CandidateBin],
    *,
    voluntary_split_counts: Mapping[str, int],
    payload_capacity: int,
    minimum_payload_fill: int,
) -> _OptionalSplitMove | None:
    waiting_before = _waiting_candidate_bytes(candidate_bins, minimum_payload_fill)
    best_move: _OptionalSplitMove | None = None
    best_key: (
        tuple[
            int,
            int,
            int,
            int,
            int,
            int,
            tuple[tuple[str, str, int], ...],
        ]
        | None
    ) = None
    for target_idx in sorted(
        range(len(candidate_bins)),
        key=lambda idx: (candidate_bins[idx].estimated_bytes, idx),
    ):
        target = candidate_bins[target_idx]
        if target.estimated_bytes >= minimum_payload_fill:
            continue
        if target.voluntary_split_collection_ids:
            continue
        remaining = payload_capacity - target.estimated_bytes
        if remaining <= 0:
            continue
        move = _best_saturation_split_move_for_target(
            candidate_bins,
            target_bin_index=target_idx,
            voluntary_split_counts=voluntary_split_counts,
            payload_capacity=payload_capacity,
            minimum_payload_fill=minimum_payload_fill,
            waiting_before=waiting_before,
        )
        if move is None:
            continue
        split_count = voluntary_split_counts.get(move.collection_id, 0)
        target_after = target.estimated_bytes + move.target_group_estimated_bytes
        donor = candidate_bins[move.donor_bin_index]
        donor_after = donor.estimated_bytes - move.moved_payload_bytes
        waiting_after = _waiting_bytes_after_move(
            waiting_before=waiting_before,
            target_before=target.estimated_bytes,
            target_after=target_after,
            donor_before=donor.estimated_bytes,
            donor_after=donor_after,
            minimum_payload_fill=minimum_payload_fill,
        )
        piece_key = tuple(
            (piece.collection_id, piece.path, piece.part_index) for piece in move.moved_pieces
        )
        candidate_key = (
            split_count,
            waiting_after,
            -target_after,
            -donor_after,
            target_idx,
            move.donor_bin_index,
            piece_key,
        )
        if best_key is None or candidate_key < best_key:
            best_key = candidate_key
            best_move = move
    return best_move


def _best_saturation_split_move_for_target(
    candidate_bins: Sequence[_CandidateBin],
    *,
    target_bin_index: int,
    voluntary_split_counts: Mapping[str, int],
    payload_capacity: int,
    minimum_payload_fill: int,
    waiting_before: int,
) -> _OptionalSplitMove | None:
    target = candidate_bins[target_bin_index]
    target_remaining = payload_capacity - target.estimated_bytes
    best_move: _OptionalSplitMove | None = None
    best_key: (
        tuple[
            int,
            int,
            int,
            int,
            tuple[tuple[str, str, int], ...],
        ]
        | None
    ) = None
    for donor_idx in sorted(
        range(len(candidate_bins)),
        key=lambda idx: (-candidate_bins[idx].estimated_bytes, idx),
    ):
        if donor_idx == target_bin_index:
            continue
        donor = candidate_bins[donor_idx]
        if donor.voluntary_split_collection_ids:
            continue

        for group_idx in sorted(
            range(len(donor.groups)),
            key=lambda idx: (
                voluntary_split_counts.get(donor.groups[idx].collection_id, 0),
                donor.groups[idx].collection_id,
                donor.groups[idx].pieces[0].path,
            ),
        ):
            group = donor.groups[group_idx]
            if group.voluntary_split:
                continue
            if group.collection_id in target.collection_ids:
                continue
            if len(group.pieces) <= 1 or any(piece.part_count != 1 for piece in group.pieces):
                continue
            move_capacity = target_remaining - group.artifact_estimate
            if move_capacity <= 0:
                continue
            moved_pieces = _optional_split_piece_subset(group.pieces, move_capacity)
            if not moved_pieces:
                continue
            moved_payload_bytes = sum(piece.estimated_total_bytes for piece in moved_pieces)
            target_group_estimated_bytes = group.artifact_estimate + moved_payload_bytes
            target_after = target.estimated_bytes + target_group_estimated_bytes
            donor_after = donor.estimated_bytes - moved_payload_bytes
            if target_after > payload_capacity or target_after < minimum_payload_fill:
                continue
            waiting_after = _waiting_bytes_after_move(
                waiting_before=waiting_before,
                target_before=target.estimated_bytes,
                target_after=target_after,
                donor_before=donor.estimated_bytes,
                donor_after=donor_after,
                minimum_payload_fill=minimum_payload_fill,
            )
            if waiting_after >= waiting_before:
                continue
            piece_key = tuple(
                (piece.collection_id, piece.path, piece.part_index) for piece in moved_pieces
            )
            candidate_key = (
                voluntary_split_counts.get(group.collection_id, 0),
                waiting_after,
                -target_after,
                -donor_after,
                piece_key,
            )
            if best_key is None or candidate_key < best_key:
                best_key = candidate_key
                best_move = _OptionalSplitMove(
                    target_bin_index=target_bin_index,
                    donor_bin_index=donor_idx,
                    donor_group_index=group_idx,
                    collection_id=group.collection_id,
                    moved_pieces=moved_pieces,
                    moved_payload_bytes=moved_payload_bytes,
                    target_group_estimated_bytes=target_group_estimated_bytes,
                )
    return best_move


def _waiting_bytes_after_move(
    *,
    waiting_before: int,
    target_before: int,
    target_after: int,
    donor_before: int,
    donor_after: int,
    minimum_payload_fill: int,
) -> int:
    before = waiting_before
    if target_before < minimum_payload_fill:
        before -= target_before
    if donor_before < minimum_payload_fill:
        before -= donor_before
    after = before
    if target_after < minimum_payload_fill:
        after += target_after
    if donor_after < minimum_payload_fill:
        after += donor_after
    return after


def _find_optional_split_move(
    candidate_bins: Sequence[_CandidateBin],
    *,
    optionally_splittable_collections: set[str],
    split_collection_ids: set[str],
    payload_capacity: int,
    minimum_payload_fill: int,
) -> _OptionalSplitMove | None:
    best_move: _OptionalSplitMove | None = None
    best_key: tuple[int, int, int, int, int, tuple[tuple[str, str, int], ...]] | None = None
    waiting_before = _waiting_candidate_bytes(candidate_bins, minimum_payload_fill)
    for target_idx in sorted(
        range(len(candidate_bins)),
        key=lambda idx: (candidate_bins[idx].estimated_bytes, idx),
    ):
        target = candidate_bins[target_idx]
        if target.estimated_bytes >= minimum_payload_fill:
            continue
        if target.voluntary_split_collection_ids:
            continue
        remaining = payload_capacity - target.estimated_bytes
        if remaining <= 0:
            continue
        move = _best_optional_split_move_for_target(
            candidate_bins,
            target_bin_index=target_idx,
            optionally_splittable_collections=optionally_splittable_collections,
            split_collection_ids=split_collection_ids,
            payload_capacity=payload_capacity,
        )
        if move is None:
            continue
        target_after = target.estimated_bytes + move.target_group_estimated_bytes
        donor_after = (
            candidate_bins[move.donor_bin_index].estimated_bytes - move.moved_payload_bytes
        )
        piece_key = tuple(
            (piece.collection_id, piece.path, piece.part_index) for piece in move.moved_pieces
        )
        if target_after < minimum_payload_fill:
            continue
        waiting_after = _waiting_bytes_after_move(
            waiting_before=waiting_before,
            target_before=target.estimated_bytes,
            target_after=target_after,
            donor_before=candidate_bins[move.donor_bin_index].estimated_bytes,
            donor_after=donor_after,
            minimum_payload_fill=minimum_payload_fill,
        )
        if waiting_after > waiting_before:
            continue
        candidate_key = (
            waiting_after,
            -target_after,
            -donor_after,
            target_idx,
            move.donor_bin_index,
            piece_key,
        )
        if best_key is None or candidate_key < best_key:
            best_key = candidate_key
            best_move = move
    return best_move


def _best_optional_split_move_for_target(
    candidate_bins: Sequence[_CandidateBin],
    *,
    target_bin_index: int,
    optionally_splittable_collections: set[str],
    split_collection_ids: set[str],
    payload_capacity: int,
) -> _OptionalSplitMove | None:
    target = candidate_bins[target_bin_index]
    target_remaining = payload_capacity - target.estimated_bytes
    best_move: _OptionalSplitMove | None = None
    best_key: tuple[int, int, int, tuple[tuple[str, str, int], ...]] | None = None
    for donor_idx in sorted(
        range(len(candidate_bins)),
        key=lambda idx: (-candidate_bins[idx].estimated_bytes, idx),
    ):
        if donor_idx == target_bin_index:
            continue
        donor = candidate_bins[donor_idx]
        if donor.voluntary_split_collection_ids:
            continue

        for group_idx in sorted(
            range(len(donor.groups)),
            key=lambda idx: (
                donor.groups[idx].collection_id,
                donor.groups[idx].pieces[0].path,
            ),
        ):
            group = donor.groups[group_idx]
            if group.voluntary_split:
                continue
            if group.collection_id not in optionally_splittable_collections:
                continue
            if group.collection_id in split_collection_ids:
                continue
            if group.collection_id in target.collection_ids:
                continue
            move_capacity = target_remaining - group.artifact_estimate
            if move_capacity <= 0:
                continue
            moved_pieces = _optional_split_piece_subset(group.pieces, move_capacity)
            if not moved_pieces:
                continue
            moved_payload_bytes = sum(piece.estimated_total_bytes for piece in moved_pieces)
            target_group_estimated_bytes = group.artifact_estimate + moved_payload_bytes
            target_after = target.estimated_bytes + target_group_estimated_bytes
            donor_after = donor.estimated_bytes - moved_payload_bytes
            if target_after > payload_capacity:
                continue
            piece_key = tuple(
                (piece.collection_id, piece.path, piece.part_index) for piece in moved_pieces
            )
            candidate_key = (target_after, donor_after, -len(moved_pieces), piece_key)
            if best_key is None or candidate_key > best_key:
                best_key = candidate_key
                best_move = _OptionalSplitMove(
                    target_bin_index=target_bin_index,
                    donor_bin_index=donor_idx,
                    donor_group_index=group_idx,
                    collection_id=group.collection_id,
                    moved_pieces=moved_pieces,
                    moved_payload_bytes=moved_payload_bytes,
                    target_group_estimated_bytes=target_group_estimated_bytes,
                )
    return best_move


def _optional_split_piece_subset(
    pieces: Sequence[_PlanPiece],
    capacity: int,
) -> tuple[_PlanPiece, ...]:
    selected: list[_PlanPiece] = []
    used = 0
    for piece in sorted(
        pieces,
        key=lambda current: (
            -current.estimated_total_bytes,
            current.path,
            current.part_index,
        ),
    ):
        next_used = used + piece.estimated_total_bytes
        if next_used > capacity:
            continue
        selected.append(piece)
        used = next_used
    if len(selected) == len(pieces):
        selected.remove(
            min(
                selected,
                key=lambda current: (
                    current.estimated_total_bytes,
                    current.path,
                    current.part_index,
                ),
            )
        )
    return tuple(sorted(selected, key=lambda piece: (piece.path, piece.part_index)))


def _apply_optional_split_move(
    candidate_bins: list[_CandidateBin],
    move: _OptionalSplitMove,
) -> None:
    target = candidate_bins[move.target_bin_index]
    donor = candidate_bins[move.donor_bin_index]
    donor_group = donor.groups[move.donor_group_index]
    moved_piece_keys = {(piece.file_id, piece.part_index) for piece in move.moved_pieces}
    remaining_pieces = tuple(
        piece
        for piece in donor_group.pieces
        if (piece.file_id, piece.part_index) not in moved_piece_keys
    )
    if not remaining_pieces:
        raise RuntimeError("optional collection split cannot move every file")

    donor.groups[move.donor_group_index] = _CollectionPieceGroup(
        collection_id=donor_group.collection_id,
        pieces=remaining_pieces,
        estimated_bytes=donor_group.estimated_bytes - move.moved_payload_bytes,
        artifact_estimate=donor_group.artifact_estimate,
        voluntary_split=True,
    )
    donor.estimated_bytes -= move.moved_payload_bytes
    donor.voluntary_split_collection_ids.add(move.collection_id)

    target.groups.append(
        _CollectionPieceGroup(
            collection_id=move.collection_id,
            pieces=move.moved_pieces,
            estimated_bytes=move.target_group_estimated_bytes,
            artifact_estimate=donor_group.artifact_estimate,
            voluntary_split=True,
        )
    )
    target.groups.sort(
        key=lambda group: (
            group.collection_id,
            group.pieces[0].path if group.pieces else "",
        )
    )
    target.collection_ids.add(move.collection_id)
    target.estimated_bytes += move.target_group_estimated_bytes
    target.voluntary_split_collection_ids.add(move.collection_id)


def _collection_artifact_estimate(config: RuntimeConfig, collection_id: str) -> int:
    artifact = _read_collection_artifact_cache(config, collection_id)
    if artifact is None:
        return 0
    return _collection_artifact_bytes_estimate(artifact)


def _collection_artifact_bytes_estimate(artifact: _CollectionArtifactCache) -> int:
    return _estimated_encrypted_leaf_size(
        len(artifact.manifest_bytes)
    ) + _estimated_encrypted_leaf_size(len(artifact.proof_bytes))


def _estimated_encrypted_leaf_size(plaintext_size: int) -> int:
    return (
        encrypted_size_for_plaintext_size(plaintext_size)
        + _AGE_ENCRYPTED_HEADER_PAD_BYTES
        + _ISO_LEAF_PAD_BYTES
    )


def _candidate_metadata_pad(target_bytes: int) -> int:
    return min(_CANDIDATE_BASE_METADATA_PAD_BYTES, max(1, target_bytes // 10))


def _max_piece_plaintext_bytes(target_bytes: int) -> int:
    budget = max(1, target_bytes - _candidate_metadata_pad(target_bytes))
    encrypted_payload_budget = max(
        1,
        budget
        - _AGE_ENCRYPTED_HEADER_PAD_BYTES
        - _ISO_LEAF_PAD_BYTES
        - _DISC_MANIFEST_ENTRY_PAD_BYTES,
    )
    return max(1, max_plaintext_size_for_encrypted_budget(encrypted_payload_budget))


def _materialize_candidate(
    *,
    config: RuntimeConfig,
    hot_store: HotStore,
    recovery_payload_codec: RecoveryPayloadCodec,
    candidate_id: str,
    finalized_id: str,
    pieces: Sequence[_PlanPiece],
    candidates_root: Path,
) -> _MaterializedCandidate:
    tmp_root = candidates_root / f".{candidate_id}.tmp"
    image_root = candidates_root / candidate_id
    if image_root.exists():
        _LOG.info(
            "planner candidate %s image root already exists; estimating ISO size",
            candidate_id,
        )
        actual_bytes = estimate_iso_size_from_root(
            image_root=image_root,
            volume_id=finalized_id,
            fallback_bytes=_tree_file_bytes(image_root),
        )
        return _MaterializedCandidate(
            candidate_id=candidate_id,
            finalized_id=finalized_id,
            filename=f"{finalized_id}.iso",
            bytes=actual_bytes,
            iso_ready=_candidate_iso_ready(
                actual_bytes,
                config=config,
            ),
            image_root=image_root,
            covered_paths=_covered_paths_for_pieces(pieces),
        )
    if tmp_root.exists():
        _LOG.info("planner candidate %s resumes materialization from %s", candidate_id, tmp_root)
    else:
        _LOG.info("planner candidate %s creates materialization root %s", candidate_id, tmp_root)
    tmp_root.mkdir(parents=True, exist_ok=True)

    try:
        layout_pieces = [_layout_piece(piece) for piece in pieces]
        collections = _collections_layout(pieces)
        path_map = assign_paths(layout_pieces)
        artifact_paths = assign_collection_artifact_paths(collections)

        for collection_id, (manifest_path, proof_path) in artifact_paths.items():
            artifact = _read_collection_artifact_cache(config, collection_id)
            if artifact is None:
                raise FileNotFoundError(f"missing cached archive artifacts for {collection_id}")
            _write_encrypted_bytes_if_missing(
                artifact.manifest_bytes,
                tmp_root / manifest_path,
                recovery_payload_codec,
            )
            _write_encrypted_bytes_if_missing(
                artifact.proof_bytes,
                tmp_root / proof_path,
                recovery_payload_codec,
            )
        _LOG.info(
            "planner materialization artifacts written for %s: collections=%s",
            candidate_id,
            len(artifact_paths),
        )

        written = 0
        skipped = 0
        total_piece_files = len(pieces) * 2
        for piece in pieces:
            payload_path, sidecar_path = path_map[
                (piece.collection_id, piece.file_id, piece.part_index)
            ]
            if _write_encrypted_chunks_if_missing(
                hot_store.iter_collection_file(
                    piece.collection_id,
                    piece.path,
                    offset=piece.offset,
                    size=piece.plaintext_bytes,
                ),
                tmp_root / payload_path,
                recovery_payload_codec,
            ):
                written += 1
            else:
                skipped += 1
            sidecar = sidecar_bytes(
                _planner_file_meta(path=piece.path, bytes=piece.bytes, sha256=piece.sha256),
                collection_id=piece.collection_id,
                part_index=piece.part_index,
                part_count=piece.part_count,
            )
            if _write_encrypted_bytes_if_missing(
                sidecar,
                tmp_root / sidecar_path,
                recovery_payload_codec,
            ):
                written += 1
            else:
                skipped += 1
            completed = written + skipped
            if completed == total_piece_files or completed % 50 == 0:
                _LOG.info(
                    "planner materialization progress for %s: files=%s/%s written=%s skipped=%s",
                    candidate_id,
                    completed,
                    total_piece_files,
                    written,
                    skipped,
                )

        manifest = manifest_bytes(
            finalized_id,
            collections,
            path_map,
            volume_id=finalized_id,
            collection_artifact_paths=artifact_paths,
        )
        _write_encrypted_bytes_if_missing(
            manifest,
            tmp_root / MANIFEST_FILENAME,
            recovery_payload_codec,
        )
        if not (tmp_root / README_FILENAME).exists():
            _write_bytes_atomic(tmp_root / README_FILENAME, recovery_readme_bytes(finalized_id))

        actual_bytes = estimate_iso_size_from_root(
            image_root=tmp_root,
            volume_id=finalized_id,
            fallback_bytes=_tree_file_bytes(tmp_root),
        )
        _LOG.info(
            "planner candidate %s ISO estimate complete: bytes=%s target_bytes=%s",
            candidate_id,
            actual_bytes,
            config.planner_disc_target_bytes,
        )
        tmp_root.rename(image_root)
    except Exception:
        _LOG.exception(
            "planner materialization failed for %s; keeping %s for resumable retry",
            candidate_id,
            tmp_root,
        )
        raise

    return _MaterializedCandidate(
        candidate_id=candidate_id,
        finalized_id=finalized_id,
        filename=f"{finalized_id}.iso",
        bytes=actual_bytes,
        iso_ready=_candidate_iso_ready(
            actual_bytes,
            config=config,
        ),
        image_root=image_root,
        covered_paths=_covered_paths_for_pieces(pieces),
    )


def _candidate_iso_ready(
    actual_bytes: int,
    *,
    config: RuntimeConfig,
) -> bool:
    return config.planner_min_fill_bytes <= actual_bytes <= config.planner_disc_target_bytes


def _collections_layout(pieces: Sequence[_PlanPiece]) -> dict[str, list[LayoutFileMeta]]:
    by_collection: dict[str, list[LayoutFileMeta]] = {}
    by_file: dict[tuple[str, str], LayoutFileMeta] = {}
    for piece in pieces:
        key = (piece.collection_id, piece.path)
        file_meta = by_file.get(key)
        if file_meta is None:
            file_meta = {
                "file_id": piece.file_id,
                "relpath": piece.path,
                "sha256": piece.sha256,
                "piece_count": piece.part_count,
                "pieces": [],
                "plaintext_bytes": piece.bytes,
            }
            by_file[key] = file_meta
            by_collection.setdefault(piece.collection_id, []).append(file_meta)
        file_meta["pieces"].append(_layout_piece(piece))
    return by_collection


def _layout_piece(piece: _PlanPiece) -> LayoutPiece:
    return {
        "collection": piece.collection_id,
        "file_id": piece.file_id,
        "relpath": piece.path,
        "piece_index": piece.part_index,
        "piece_count": piece.part_count,
        "stored_size_bytes": piece.estimated_payload_bytes,
        "sidecar_size_bytes": piece.estimated_sidecar_bytes,
    }


def _planner_file_meta(*, path: str, bytes: int, sha256: str) -> PlannerFileMeta:
    return {
        "relpath": path,
        "sha256": sha256,
        "plaintext_bytes": bytes,
    }


def _write_encrypted_bytes(
    content: bytes,
    path: Path,
    recovery_payload_codec: RecoveryPayloadCodec,
) -> None:
    _write_encrypted_chunks((content,), path, recovery_payload_codec)


def _write_encrypted_bytes_if_missing(
    content: bytes,
    path: Path,
    recovery_payload_codec: RecoveryPayloadCodec,
) -> bool:
    if path.exists():
        return False
    _write_encrypted_bytes(content, path, recovery_payload_codec)
    return True


def _write_encrypted_chunks_if_missing(
    chunks: Iterable[bytes],
    path: Path,
    recovery_payload_codec: RecoveryPayloadCodec,
) -> bool:
    if path.exists():
        return False
    _write_encrypted_chunks(chunks, path, recovery_payload_codec)
    return True


def _write_encrypted_chunks(
    chunks: Iterable[bytes],
    path: Path,
    recovery_payload_codec: RecoveryPayloadCodec,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.unlink(missing_ok=True)
    if isinstance(recovery_payload_codec, CommandAgeBatchpassRecoveryPayloadCodec):
        _write_age_batchpass_stream(chunks, tmp_path, recovery_payload_codec)
    else:
        _write_bytes_atomic(tmp_path, recovery_payload_codec.encrypt(b"".join(chunks)))
    os.replace(tmp_path, path)


def _write_age_batchpass_stream(
    chunks: Iterable[bytes],
    path: Path,
    recovery_payload_codec: CommandAgeBatchpassRecoveryPayloadCodec,
) -> None:
    if not recovery_payload_codec.command:
        raise RecoveryPayloadError("recovery payload age command is empty")
    if not recovery_payload_codec.passphrase:
        raise RecoveryPayloadError("recovery payload passphrase is not configured")
    env = {
        **os.environ,
        "AGE_PASSPHRASE": recovery_payload_codec.passphrase,
        "AGE_PASSPHRASE_WORK_FACTOR": str(recovery_payload_codec.work_factor),
    }
    with path.open("wb") as output:
        try:
            proc = subprocess.Popen(
                [*recovery_payload_codec.command, "-e", "-j", "batchpass"],
                stdin=subprocess.PIPE,
                stdout=output,
                stderr=subprocess.PIPE,
                env=env,
            )
        except OSError as exc:
            raise RecoveryPayloadError("recovery payload encrypt command failed") from exc

        assert proc.stdin is not None
        try:
            for chunk in chunks:
                proc.stdin.write(chunk)
            proc.stdin.close()
            stderr = proc.stderr.read() if proc.stderr is not None else b""
            return_code = proc.wait()
        except Exception:
            proc.kill()
            proc.wait()
            raise

    if return_code != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        if not detail:
            detail = f"age command exited with status {return_code}"
        raise RecoveryPayloadError(f"recovery payload encrypt failed: {detail}")


def _tree_file_bytes(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def _next_finalized_id(reserved_ids: set[str]) -> str:
    candidate_time = datetime.now(UTC).replace(microsecond=0)
    while True:
        candidate = candidate_time.strftime("%Y%m%dT%H%M%SZ")
        if candidate not in reserved_ids:
            return candidate
        candidate_time += timedelta(seconds=1)


def _next_candidate_id(finalized_id: str, reserved_ids: set[str]) -> str:
    base = f"candidate-{finalized_id}"
    candidate_id = base
    ordinal = 2
    while candidate_id in reserved_ids:
        candidate_id = f"{base}-{ordinal}"
        ordinal += 1
    return candidate_id


class CandidatePlanView(TypedDict):
    candidate_id: str
    bytes: int
    target_bytes: int
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
    target_bytes: int
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


def _candidate_plan_view(candidate: PlannedCandidateRecord) -> CandidatePlanView:
    collection_ids = sorted({cp.collection_id for cp in candidate.covered_paths})
    projected_paths = sorted(f"{cp.collection_id}/{cp.path}" for cp in candidate.covered_paths)
    fill = candidate.bytes / candidate.target_bytes if candidate.target_bytes else 0.0
    return {
        "candidate_id": candidate.candidate_id,
        "bytes": candidate.bytes,
        "target_bytes": candidate.target_bytes,
        "fill": fill,
        "files": len(candidate.covered_paths),
        "collections": len(collection_ids),
        "collection_ids": collection_ids,
        "iso_ready": candidate.iso_ready,
        "_bytes": candidate.bytes,
        "_collections": collection_ids,
        "_projected_paths": projected_paths,
    }


def _image_summary_collection_ids(row: ImageOperatorSummaryRecord) -> list[str]:
    if not row.collection_ids_text:
        return []
    return row.collection_ids_text.split("\n")


def _image_summary_collection_clause(collection: str) -> Any:
    collection_ids = ImageOperatorSummaryRecord.collection_ids_text
    return or_(
        collection_ids == collection,
        collection_ids.startswith(f"{collection}\n"),
        collection_ids.endswith(f"\n{collection}"),
        collection_ids.contains(f"\n{collection}\n"),
    )


def _image_summary_order(*, sort: str, order: str) -> list[Any]:
    columns = {
        "finalized_at": (
            ImageOperatorSummaryRecord.image_id,
            ImageOperatorSummaryRecord.filename,
        ),
        "bytes": (
            ImageOperatorSummaryRecord.bytes,
            ImageOperatorSummaryRecord.image_id,
        ),
        "physical_copies_registered": (
            ImageOperatorSummaryRecord.physical_copies_registered,
            ImageOperatorSummaryRecord.image_id,
        ),
    }[sort]
    if order == "desc":
        return [column.desc() for column in columns]
    return [column.asc() for column in columns]


def _finalized_image_view_from_summary(
    row: ImageOperatorSummaryRecord,
) -> FinalizedImageView:
    collection_ids = _image_summary_collection_ids(row)
    fill = row.bytes / row.target_bytes if row.target_bytes else 0.0
    return {
        "id": row.image_id,
        "filename": row.filename,
        "finalized_at": row.finalized_at,
        "bytes": int(row.bytes),
        "target_bytes": int(row.target_bytes),
        "fill": fill,
        "files": int(row.files),
        "collections": int(row.collections),
        "collection_ids": collection_ids,
        "iso_ready": True,
        "physical_protection_state": row.physical_protection_state,
        "physical_copies_required": int(row.physical_copies_required),
        "physical_copies_registered": int(row.physical_copies_registered),
        "physical_copies_verified": int(row.physical_copies_verified),
        "physical_copies_missing": int(row.physical_copies_missing),
        "_bytes": int(row.bytes),
        "_collection_ids": collection_ids,
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
    collection_ids = sorted(
        session.scalars(
            select(FinalizedImageCoveredPathRecord.collection_id)
            .where(FinalizedImageCoveredPathRecord.image_id == image.image_id)
            .distinct()
        ).all()
    )
    files = (
        session.scalar(
            select(func.count())
            .select_from(FinalizedImageCoveredPathRecord)
            .where(FinalizedImageCoveredPathRecord.image_id == image.image_id)
        )
        or 0
    )
    fill = image.bytes / image.target_bytes if image.target_bytes else 0.0
    finalized_at = _image_id_to_finalized_at(image.image_id)
    return {
        "id": image.image_id,
        "filename": image.filename,
        "finalized_at": finalized_at,
        "bytes": image.bytes,
        "target_bytes": image.target_bytes,
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


def _isoformat_z(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_isoformat_z(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


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
    bytes: int | None = None


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
            content_length=image.bytes,
        )
