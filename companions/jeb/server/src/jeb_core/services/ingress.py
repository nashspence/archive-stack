"""Durable Jeb TUS publication lifecycle."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from riverhog_provenance import ProvenanceValidationError

from jeb_core.domain.models import JebConfig
from jeb_core.ingress import (
    JebIngressAuthenticationError,
    JebIngressCollisionError,
    JebIngressError,
    JebLandingPublisher,
    PreparedTusUpload,
    authenticate_tus_source,
    prepare_tus_upload,
    validate_tus_upload,
)
from jeb_core.persistence.source_registry import SourceRegistry
from jeb_core.persistence.sqlite_state import SQLiteJebStore
from jeb_core.provenance import (
    finalize_ingress_provenance,
    load_provenance_set,
    publish_ingress_provenance,
    put_ingress_binding,
    put_ingress_journal,
    remove_staged_ingress_provenance,
)
from jeb_core.services.files import file_sha256, terminate_tus_upload

LOG = logging.getLogger("jeb")


class JebIngressPublicationService:
    def __init__(
        self,
        config: JebConfig,
        store: SQLiteJebStore,
        registry: SourceRegistry,
    ) -> None:
        self.config = config
        self.store = store
        self.registry = registry

    def prepare(
        self,
        *,
        authorization: str | None,
        metadata: Mapping[str, object],
        size: object,
    ) -> PreparedTusUpload:
        prepared = prepare_tus_upload(
            self.config.ingress,
            self.registry,
            authorization=authorization,
            metadata=metadata,
            size=size,
        )
        self.store.create_ingress_publication(
            upload_id=prepared.upload_id,
            source_id=prepared.source,
            relative_path=prepared.relative_path,
            declared_bytes=prepared.size,
            payload_sha256=prepared.payload_sha256,
            provenance_sha256=prepared.provenance_sha256,
        )
        return prepared

    def put_journal(
        self,
        *,
        authorization: str | None,
        upload_id: str,
        journal_id: str,
        content: bytes,
        expected_sha256: str,
    ) -> dict[str, object]:
        source_id = self._authorized_publication(upload_id, authorization)
        return put_ingress_journal(
            self.config.ingress,
            upload_id=upload_id,
            source_id=source_id,
            journal_id=journal_id,
            content=content,
            expected_sha256=expected_sha256,
        )

    def put_binding(
        self,
        *,
        authorization: str | None,
        upload_id: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        source_id = self._authorized_publication(upload_id, authorization)
        return put_ingress_binding(
            self.config.ingress,
            upload_id=upload_id,
            source_id=source_id,
            payload=payload,
        )

    def receipt(self, upload_id: str, *, authorization: str | None) -> dict[str, object]:
        self._authorized_publication(upload_id, authorization, require_pending=False)
        return self._receipt(dict(self.store.ingress_publication(upload_id)))

    def publish(self, upload: Mapping[str, object]) -> dict[str, object]:
        upload_id = str(upload.get("ID") or "")
        publication_started = False
        try:
            row = self.store.ingress_publication(upload_id)
        except KeyError as exc:
            raise JebIngressError("Jeb TUS upload has no durable publication intent") from exc
        if str(row["status"]) != "pending":
            self._cleanup_terminal(upload_id)
            return self._receipt(dict(row))
        try:
            if self.registry.is_removing(str(row["source_id"])):
                raise JebIngressAuthenticationError("Jeb source removal has quiesced ingress")
            validated = validate_tus_upload(
                self.config.ingress,
                self.registry,
                upload=upload,
            )
            self._require_record_match(dict(row), validated)
            self._require_destination_available(validated.source, validated.destination)
            provenance = finalize_ingress_provenance(
                self.config.ingress,
                upload_id=validated.upload_id,
                source_id=validated.source_id,
                source=validated.source,
                target_path=f"{validated.source_id}/{validated.relative_path}",
                expected_provenance_sha256=validated.provenance_sha256,
            )
            if provenance.binding.sha256 != validated.payload_sha256:
                raise ProvenanceValidationError(
                    "Jeb TUS payload identity does not match its signed metadata"
                )
            publisher = JebLandingPublisher(self.config.ingress)
            publisher.publish(
                upload_id=validated.upload_id,
                source=validated.source,
                info=validated.info,
                destination=validated.destination,
                size=validated.size,
            )
            publication_started = True
            publish_ingress_provenance(validated.destination, provenance)
            self._verify_publication(validated.destination, validated.payload_sha256, provenance)
            identity = hashlib.sha256(provenance.index_bytes).hexdigest()
            accepted = self.store.accept_ingress_publication(
                validated.upload_id,
                destination_path=f"{validated.source_id}/{validated.relative_path}",
                provenance_identity=identity,
            )
            self._cleanup_accepted(validated.source, validated.info, validated.upload_id)
            return self._receipt(dict(accepted))
        except JebIngressCollisionError as exc:
            return self._reject(upload_id, "destination_collision", str(exc))
        except ProvenanceValidationError as exc:
            if publication_started:
                raise JebIngressError(
                    "Jeb ingress publication remains pending after a provenance write failure"
                ) from exc
            return self._reject(upload_id, "provenance_rejected", str(exc))
        except JebIngressAuthenticationError as exc:
            return self._reject(upload_id, "source_rejected", str(exc))
        except JebIngressError as exc:
            if publication_started:
                raise
            return self._reject(upload_id, "invalid_upload", str(exc))

    def reconcile(self, *, now: float | None = None) -> dict[str, int]:
        observed_at = time.time() if now is None else now
        result = {"pending": 0, "accepted": 0, "rejected": 0, "failed": 0}
        for row in self.store.pending_ingress_publications():
            upload_id = str(row["upload_id"])
            info_path = self.config.ingress.tus_staging_dir / f"{upload_id}.info"
            age = observed_at - self._created_timestamp(str(row["created_at"]))
            if info_path.is_file():
                try:
                    upload = self._upload_record(info_path, upload_id)
                    complete = self._integer(upload.get("Offset"), default=0) == self._integer(
                        upload.get("Size"), default=-1
                    )
                except (JebIngressError, json.JSONDecodeError, TypeError, ValueError) as exc:
                    if age < self.config.ingress.tus_incomplete_max_age_seconds:
                        LOG.exception(
                            "Jeb ingress upload record is not yet recoverable: %s",
                            upload_id,
                        )
                        result["failed"] += 1
                        continue
                    if not self._terminate_for_rejection(upload_id):
                        result["failed"] += 1
                        continue
                    self._reject(
                        upload_id,
                        "invalid_upload_record",
                        f"unrecoverable TUS upload record: {exc}",
                    )
                    result["rejected"] += 1
                    continue
                except OSError:
                    LOG.exception(
                        "Jeb ingress upload record could not be read: %s",
                        upload_id,
                    )
                    result["failed"] += 1
                    continue
                if complete:
                    try:
                        receipt = self.publish(upload)
                    except Exception:
                        LOG.exception(
                            "Jeb ingress publication reconciliation failed for %s",
                            upload_id,
                        )
                        result["failed"] += 1
                    else:
                        result[str(receipt["status"])] += 1
                    continue
            if age < self.config.ingress.tus_incomplete_max_age_seconds:
                result["pending"] += 1
                continue
            if not self._terminate_for_rejection(upload_id):
                result["failed"] += 1
                continue
            self._reject(upload_id, "upload_expired", "incomplete TUS upload expired")
            result["rejected"] += 1
        self._cleanup_terminal_staging()
        return result

    def cancel_source(self, source_id: str) -> None:
        for row in self.store.pending_ingress_publications(source_id=source_id):
            upload_id = str(row["upload_id"])
            terminate_tus_upload(self.config.ingress, upload_id)
            self._reject(upload_id, "source_removed", "source removal canceled ingress")

    def _terminate_for_rejection(self, upload_id: str) -> bool:
        try:
            terminate_tus_upload(self.config.ingress, upload_id)
        except Exception:
            LOG.exception("Jeb could not terminate rejected TUS upload %s", upload_id)
            return False
        return True

    def _authorized_publication(
        self,
        upload_id: str,
        authorization: str | None,
        *,
        require_pending: bool = True,
    ) -> str:
        source_id = authenticate_tus_source(self.registry, authorization)
        try:
            row = self.store.ingress_publication(upload_id)
        except KeyError as exc:
            raise JebIngressAuthenticationError("invalid Jeb ingress publication") from exc
        if str(row["source_id"]) != source_id:
            raise JebIngressAuthenticationError("invalid Jeb ingress publication")
        if require_pending and str(row["status"]) != "pending":
            raise JebIngressError("Jeb ingress publication is already terminal")
        return source_id

    @staticmethod
    def _require_record_match(row: Mapping[str, Any], validated: Any) -> None:
        expected = (
            str(row["source_id"]),
            str(row["relative_path"]),
            int(row["declared_bytes"]),
            str(row["payload_sha256"]),
            str(row["provenance_sha256"]),
        )
        actual = (
            validated.source_id,
            validated.relative_path,
            validated.size,
            validated.payload_sha256,
            validated.provenance_sha256,
        )
        if actual != expected:
            raise JebIngressAuthenticationError(
                "Jeb TUS upload does not match its durable publication intent"
            )

    @staticmethod
    def _require_destination_available(source: Path, destination: Path) -> None:
        if not destination.exists():
            return
        try:
            if os.path.samefile(source, destination):
                return
        except FileNotFoundError:
            pass
        raise JebIngressCollisionError(f"Jeb landing path already exists: {destination.name}")

    @staticmethod
    def _verify_publication(destination: Path, payload_sha256: str, provenance: Any) -> None:
        if file_sha256(destination) != payload_sha256:
            raise JebIngressError("published Jeb payload identity changed")
        published = load_provenance_set(
            destination.with_name(f".{destination.name}.riverhog-provenance") / "index.json"
        )
        if (
            published.index_bytes != provenance.index_bytes
            or published.journals != provenance.journals
        ):
            raise JebIngressError("published Jeb provenance identity changed")

    def _cleanup_accepted(self, source: Path, info: Path, upload_id: str) -> None:
        try:
            JebLandingPublisher.cleanup_staging(source, info)
            remove_staged_ingress_provenance(self.config.ingress, upload_id)
        except OSError:
            LOG.warning("accepted Jeb ingress staging cleanup remains pending: %s", upload_id)

    def _reject(self, upload_id: str, code: str, message: str) -> dict[str, object]:
        rejected = self.store.reject_ingress_publication(
            upload_id,
            code=code,
            message=message,
        )
        self._cleanup_terminal(upload_id)
        return self._receipt(dict(rejected))

    def _cleanup_terminal_staging(self) -> None:
        try:
            candidates = {
                path.name.removesuffix(".info")
                for path in self.config.ingress.tus_staging_dir.glob("*.info")
            }
            candidates.update(
                path.name
                for path in self.config.ingress.tus_staging_dir.iterdir()
                if path.is_file() and not path.name.endswith(".info")
            )
        except FileNotFoundError:
            return
        for upload_id in sorted(candidates):
            try:
                row = self.store.ingress_publication(upload_id)
            except KeyError:
                continue
            if str(row["status"]) in {"accepted", "rejected"}:
                self._cleanup_terminal(upload_id)

    def _cleanup_terminal(self, upload_id: str) -> None:
        source = self.config.ingress.tus_staging_dir / upload_id
        info = self.config.ingress.tus_staging_dir / f"{upload_id}.info"
        self._cleanup_accepted(source, info, upload_id)

    @staticmethod
    def _receipt(row: Mapping[str, Any]) -> dict[str, object]:
        receipt: dict[str, object] = {
            "format": "jeb-ingress-publication/v1",
            "status": str(row["status"]),
            "upload_id": str(row["upload_id"]),
            "path": str(row["relative_path"]),
            "bytes": int(row["declared_bytes"]),
            "payload_sha256": str(row["payload_sha256"]),
        }
        if str(row["status"]) == "accepted":
            receipt["provenance_identity"] = str(row["provenance_identity"])
        elif str(row["status"]) == "rejected":
            receipt["error"] = {
                "code": str(row["error_code"]),
                "message": str(row["error_message"]),
            }
        return receipt

    def _upload_record(self, info_path: Path, upload_id: str) -> dict[str, object]:
        value = json.loads(info_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or str(value.get("ID") or "") != upload_id:
            raise JebIngressError("Jeb TUS upload record is invalid")
        value.setdefault(
            "Storage",
            {
                "Type": "filestore",
                "Path": str(self.config.ingress.tus_staging_dir / upload_id),
                "InfoPath": str(info_path),
            },
        )
        return value

    @staticmethod
    def _created_timestamp(value: str) -> float:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()

    @staticmethod
    def _integer(value: object, *, default: int) -> int:
        if value is None:
            return default
        return int(str(value))


__all__ = ["JebIngressPublicationService"]
