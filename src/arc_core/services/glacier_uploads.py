from __future__ import annotations

from pathlib import Path

from sqlalchemy import or_, select

from arc_core.catalog_models import FinalizedImageRecord, GlacierUploadJobRecord
from arc_core.ports.archive_store import ArchiveStore
from arc_core.runtime_config import RuntimeConfig
from arc_core.services.glacier_reporting import record_glacier_usage_snapshot
from arc_core.services.recovery_sessions import ensure_glacier_recovery_session_for_image
from arc_core.sqlite_db import make_session_factory, session_scope
from arc_core.webhooks import (
    WebhookConfig,
    build_glacier_upload_failed_payload,
    post_webhook,
    utcnow,
)


class SqlAlchemyGlacierUploadService:
    def __init__(self, config: RuntimeConfig, archive_store: ArchiveStore) -> None:
        self._config = config
        self._archive_store = archive_store
        self._session_factory = make_session_factory(str(config.sqlite_path))

    def process_due_uploads(self, *, limit: int = 1) -> int:
        if limit < 1:
            return 0

        current = utcnow()
        current_text = _isoformat_z(current)
        with session_scope(self._session_factory) as session:
            image_ids = session.scalars(
                select(GlacierUploadJobRecord.image_id)
                .where(GlacierUploadJobRecord.completed_at.is_(None))
                .where(GlacierUploadJobRecord.failed_at.is_(None))
                .where(
                    or_(
                        GlacierUploadJobRecord.next_attempt_at.is_(None),
                        GlacierUploadJobRecord.next_attempt_at <= current_text,
                    )
                )
                .order_by(GlacierUploadJobRecord.next_attempt_at, GlacierUploadJobRecord.image_id)
                .limit(limit)
            ).all()

        attempted = 0
        for image_id in image_ids:
            self._process_one(image_id=image_id)
            attempted += 1
        return attempted

    def _process_one(self, *, image_id: str) -> None:
        current = utcnow()
        current_text = _isoformat_z(current)
        with session_scope(self._session_factory) as session:
            job = session.get(GlacierUploadJobRecord, image_id)
            image = session.get(FinalizedImageRecord, image_id)
            if (
                job is None
                or image is None
                or job.completed_at is not None
                or job.failed_at is not None
            ):
                return
            job.attempt_count += 1
            job.last_attempt_at = current_text
            job.next_attempt_at = current_text
            image.glacier_state = "uploading"
            image.glacier_failure = None
            filename = image.filename
            image_root = image.image_root

        try:
            receipt = self._archive_store.upload_finalized_image(
                image_id=image_id,
                filename=filename,
                image_root=Path(image_root),
            )
        except Exception as exc:
            self._record_failure(image_id=image_id, error=_error_text(exc))
            return

        with session_scope(self._session_factory) as session:
            job = session.get(GlacierUploadJobRecord, image_id)
            image = session.get(FinalizedImageRecord, image_id)
            if job is None or image is None:
                return
            job.completed_at = receipt.verified_at or receipt.uploaded_at
            job.next_attempt_at = None
            job.last_error = None
            image.glacier_state = "uploaded"
            image.glacier_object_path = receipt.object_path
            image.glacier_stored_bytes = receipt.stored_bytes
            image.glacier_backend = receipt.backend
            image.glacier_storage_class = receipt.storage_class
            image.glacier_last_uploaded_at = receipt.uploaded_at
            image.glacier_last_verified_at = receipt.verified_at
            image.glacier_failure = None
            ensure_glacier_recovery_session_for_image(
                session,
                config=self._config,
                image_id=image_id,
            )
            record_glacier_usage_snapshot(session, config=self._config)

    def _record_failure(self, *, image_id: str, error: str) -> None:
        current = utcnow()
        current_text = _isoformat_z(current)
        notify_failure = False
        attempt_count = 0

        with session_scope(self._session_factory) as session:
            job = session.get(GlacierUploadJobRecord, image_id)
            image = session.get(FinalizedImageRecord, image_id)
            if job is None or image is None:
                return

            attempt_count = job.attempt_count
            job.last_error = error
            image.glacier_failure = error
            if attempt_count < self._config.glacier_upload_retry_limit:
                job.next_attempt_at = _isoformat_z(
                    current + self._config.glacier_upload_retry_delay
                )
                image.glacier_state = "retrying"
                return

            job.failed_at = current_text
            job.next_attempt_at = None
            image.glacier_state = "failed"
            notify_failure = True

        if notify_failure:
            self._notify_persistent_failure(
                image_id=image_id,
                attempt_count=attempt_count,
                error=error,
                failed_at=current_text,
            )

    def _notify_persistent_failure(
        self,
        *,
        image_id: str,
        attempt_count: int,
        error: str,
        failed_at: str,
    ) -> None:
        if not self._config.glacier_failure_webhook_url:
            return
        payload = build_glacier_upload_failed_payload(
            config=WebhookConfig(
                url=self._config.glacier_failure_webhook_url,
                base_url=self._config.public_base_url or "",
            ),
            image_id=image_id,
            error=error,
            attempts=attempt_count,
            failed_at=failed_at,
        )
        post_webhook(
            config=WebhookConfig(
                url=self._config.glacier_failure_webhook_url,
                base_url=self._config.public_base_url or "",
            ),
            payload=payload,
        )


def enqueue_glacier_upload_job(
    session,
    *,
    image_id: str,
    next_attempt_at: str,
) -> None:
    existing = session.get(GlacierUploadJobRecord, image_id)
    if existing is not None:
        return
    session.add(
        GlacierUploadJobRecord(
            image_id=image_id,
            attempt_count=0,
            next_attempt_at=next_attempt_at,
            last_attempt_at=None,
            last_error=None,
            completed_at=None,
            failed_at=None,
        )
    )


def _error_text(exc: Exception) -> str:
    detail = str(exc).strip()
    return detail or exc.__class__.__name__


def _isoformat_z(value) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")
