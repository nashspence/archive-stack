"""Provider-neutral storage-adapter service with restartable transfer semantics."""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Iterator
from typing import Literal, cast

from riverhog_storage_adapter_protocol import (
    CompleteUploadRequest,
    MaintenanceResult,
    ObjectLocator,
    ObjectReceipt,
    ReadRequest,
    ReadStatus,
    StorageAdapterDescriptor,
    StorageAdapterErrorCode,
    UploadDeclaration,
    UploadPartReceipt,
    UploadStatus,
)

from riverhog_storage_adapter_support.driver import (
    ProviderUpload,
    StorageAdapterDriver,
    StorageDriverError,
)
from riverhog_storage_adapter_support.journal import JournalUpload, UploadJournal


class StorageAdapterServiceError(RuntimeError):
    def __init__(
        self,
        status: int,
        code: StorageAdapterErrorCode,
        message: str,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class StorageAdapterService:
    """Own wire idempotency while delegating only provider effects."""

    def __init__(self, *, driver: StorageAdapterDriver, journal: UploadJournal) -> None:
        self._driver = driver
        self._journal = journal
        self._lock = threading.RLock()

    def descriptor(self) -> StorageAdapterDescriptor:
        return self._driver.descriptor()

    def ready(self) -> None:
        self._driver.ready()

    def put_upload(self, declaration: UploadDeclaration) -> UploadStatus:
        with self._lock:
            if declaration.runtime_descriptor_sha256 != self.descriptor().runtime_descriptor_sha256:
                raise StorageAdapterServiceError(
                    409,
                    "upload_conflict",
                    "upload declaration pins a different runtime descriptor",
                )
            try:
                current = self._journal.declare(declaration)
            except ValueError as exc:
                raise StorageAdapterServiceError(409, "upload_conflict", str(exc)) from exc
            if current.state == "creating":
                try:
                    upload = self._driver.create_upload(declaration)
                    self._journal.bind_provider_upload(
                        declaration.transfer_id,
                        upload.upload_id,
                    )
                except StorageDriverError as exc:
                    raise _service_error(exc) from exc
                current = self._require_upload(declaration.transfer_id)
            return _status(current)

    def get_upload(self, transfer_id: str) -> UploadStatus:
        return _status(self._require_upload(transfer_id))

    def put_part(
        self,
        *,
        transfer_id: str,
        number: int,
        content: bytes,
        stored_sha256: str,
    ) -> UploadPartReceipt:
        if hashlib.sha256(content).hexdigest() != stored_sha256:
            raise StorageAdapterServiceError(
                422,
                "integrity_failure",
                "upload part SHA-256 does not match its body",
            )
        with self._lock:
            current = self._require_open(transfer_id)
            descriptor = self.descriptor()
            if number < 1 or number > descriptor.maximum_part_count:
                raise StorageAdapterServiceError(
                    422,
                    "upload_conflict",
                    "upload part number is outside the current descriptor",
                )
            if not content or len(content) > descriptor.maximum_part_bytes:
                raise StorageAdapterServiceError(
                    422,
                    "upload_conflict",
                    "upload part length is outside the current descriptor",
                )
            existing = next((part for part in current.parts if part.number == number), None)
            if existing is not None:
                if existing.stored_bytes != len(content) or existing.stored_sha256 != stored_sha256:
                    raise StorageAdapterServiceError(
                        409,
                        "upload_conflict",
                        "upload part number is already bound to different bytes",
                    )
                return existing
            try:
                token = self._driver.upload_part(
                    declaration=current.declaration,
                    upload=_provider_upload(current),
                    number=number,
                    content=content,
                    stored_sha256=stored_sha256,
                )
                receipt = UploadPartReceipt(
                    number=number,
                    part_token=token,
                    stored_bytes=len(content),
                    stored_sha256=stored_sha256,
                )
                self._journal.record_part(transfer_id, receipt)
                return receipt
            except StorageDriverError as exc:
                raise _service_error(exc) from exc
            except ValueError as exc:
                raise StorageAdapterServiceError(409, "upload_conflict", str(exc)) from exc

    def complete_upload(
        self,
        *,
        transfer_id: str,
        completion: CompleteUploadRequest,
    ) -> ObjectReceipt:
        with self._lock:
            current = self._require_upload(transfer_id)
            if current.state == "completed":
                if current.object is None:  # pragma: no cover - journal invariant
                    raise RuntimeError("completed upload omitted its object")
                if current.object.stored_bytes != completion.stored_bytes or (
                    completion.stored_sha256 is not None
                    and current.object.stored_sha256 != completion.stored_sha256
                ):
                    raise StorageAdapterServiceError(
                        409,
                        "upload_conflict",
                        "completed upload identity differs from the request",
                    )
                return current.object
            if current.state != "open":
                raise StorageAdapterServiceError(409, "upload_conflict", "upload is not open")
            if completion.stored_bytes != current.declaration.stored_bytes:
                raise StorageAdapterServiceError(
                    409,
                    "upload_conflict",
                    "completion byte count differs from the declared object",
                )
            if completion.parts != current.parts:
                raise StorageAdapterServiceError(
                    409,
                    "upload_conflict",
                    "completion parts differ from the durable upload journal",
                )
            minimum = self.descriptor().minimum_nonfinal_part_bytes
            if any(part.stored_bytes < minimum for part in completion.parts[:-1]):
                raise StorageAdapterServiceError(
                    422,
                    "upload_conflict",
                    "non-final part is smaller than the current descriptor permits",
                )
            try:
                for part in current.parts:
                    self._driver.verify_part_receipt(
                        declaration=current.declaration,
                        upload=_provider_upload(current),
                        receipt=part,
                    )
                receipt = self._driver.complete_upload(
                    declaration=current.declaration,
                    upload=_provider_upload(current),
                    completion=completion,
                )
            except StorageDriverError as exc:
                raise _service_error(exc) from exc
            if (
                receipt.object_path != current.declaration.object_path
                or receipt.content_type != current.declaration.content_type
                or receipt.stored_bytes != completion.stored_bytes
                or (
                    completion.stored_sha256 is not None
                    and receipt.stored_sha256 != completion.stored_sha256
                )
            ):
                raise StorageAdapterServiceError(
                    502,
                    "integrity_failure",
                    "provider completion receipt differs from the declared object",
                )
            self._journal.complete(transfer_id, receipt)
            return receipt

    def delete_upload(self, transfer_id: str) -> UploadStatus:
        with self._lock:
            current = self._require_upload(transfer_id)
            if current.state == "completed":
                self._journal.acknowledge_terminal(transfer_id)
                return _status(current)
            if current.state == "aborted":
                self._journal.acknowledge_terminal(transfer_id)
                return _status(current)
            if current.provider_upload_id is not None:
                try:
                    self._driver.abort_upload(
                        declaration=current.declaration,
                        upload=_provider_upload(current),
                    )
                except StorageDriverError as exc:
                    raise _service_error(exc) from exc
            self._journal.abort(transfer_id)
            self._journal.acknowledge_terminal(transfer_id)
            return _status(self._require_upload(transfer_id))

    def object_metadata(self, locator: ObjectLocator) -> ObjectReceipt:
        receipt = self._journal.object_receipt(locator.object_path, locator.revision)
        if receipt is None:
            raise StorageAdapterServiceError(404, "not_found", "object does not exist")
        try:
            self._driver.verify_object(receipt)
        except StorageDriverError as exc:
            raise _service_error(exc) from exc
        return receipt

    def iter_object_content(
        self,
        locator: ObjectLocator,
        *,
        offset: int | None,
        size: int | None,
    ) -> Iterator[bytes]:
        try:
            yield from self._driver.iter_object_content(locator, offset=offset, size=size)
        except StorageDriverError as exc:
            raise _service_error(exc) from exc

    def delete_object(self, locator: ObjectLocator) -> None:
        try:
            self._driver.delete_object(locator)
        except StorageDriverError as exc:
            raise _service_error(exc) from exc
        self._journal.remove_object(locator.object_path, locator.revision)

    def delete_prefix(self, object_prefix: str) -> MaintenanceResult:
        try:
            affected = self._driver.delete_prefix(object_prefix)
        except StorageDriverError as exc:
            raise _service_error(exc) from exc
        self._journal.remove_prefix(object_prefix)
        return MaintenanceResult(affected=affected)

    def prepare_read(self, request: ReadRequest) -> ReadStatus:
        try:
            return self._driver.prepare_read(request)
        except StorageDriverError as exc:
            raise _service_error(exc) from exc

    def read_status(self, request: ReadRequest) -> ReadStatus:
        try:
            return self._driver.read_status(request)
        except StorageDriverError as exc:
            raise _service_error(exc) from exc

    def cleanup_read(self, request: ReadRequest) -> None:
        try:
            self._driver.cleanup_read(request)
        except StorageDriverError as exc:
            raise _service_error(exc) from exc

    def abort_incomplete_uploads(self, *, initiated_before: str) -> MaintenanceResult:
        affected = 0
        with self._lock:
            for current in self._journal.open_before(initiated_before):
                if current.provider_upload_id is not None:
                    try:
                        self._driver.abort_upload(
                            declaration=current.declaration,
                            upload=_provider_upload(current),
                        )
                    except StorageDriverError as exc:
                        raise _service_error(exc) from exc
                self._journal.abort(current.declaration.transfer_id)
                affected += 1
            try:
                affected += self._driver.abort_incomplete_uploads(initiated_before=initiated_before)
            except StorageDriverError as exc:
                raise _service_error(exc) from exc
        return MaintenanceResult(affected=affected)

    def _require_upload(self, transfer_id: str) -> JournalUpload:
        current = self._journal.load(transfer_id)
        if current is None:
            raise StorageAdapterServiceError(404, "not_found", "upload does not exist")
        return current

    def _require_open(self, transfer_id: str) -> JournalUpload:
        current = self._require_upload(transfer_id)
        if current.state != "open" or current.provider_upload_id is None:
            raise StorageAdapterServiceError(409, "upload_conflict", "upload is not open")
        if (
            current.declaration.runtime_descriptor_sha256
            != self.descriptor().runtime_descriptor_sha256
        ):
            raise StorageAdapterServiceError(
                409,
                "upload_conflict",
                "open upload runtime descriptor changed",
            )
        return current


def _provider_upload(current: JournalUpload) -> ProviderUpload:
    if current.provider_upload_id is None:
        raise RuntimeError("upload has no provider binding")
    return ProviderUpload(current.provider_upload_id)


def _status(current: JournalUpload) -> UploadStatus:
    state = current.state
    if state == "creating":
        state = "open"
    if state not in {"open", "completed", "aborted"}:
        raise RuntimeError(f"journal contains unknown upload state: {state}")
    return UploadStatus(
        declaration=current.declaration,
        state=cast(Literal["open", "completed", "aborted"], state),
        parts=current.parts,
        object=current.object,
    )


def _service_error(error: StorageDriverError) -> StorageAdapterServiceError:
    status = {
        "unauthorized": 401,
        "not_found": 404,
        "revision_conflict": 409,
        "upload_conflict": 409,
        "invalid_path": 422,
        "invalid_range": 416,
        "read_not_ready": 409,
        "read_expired": 410,
        "integrity_failure": 502,
        "provider_unavailable": 503,
        "internal_failure": 502,
    }[error.code]
    return StorageAdapterServiceError(status, error.code, error.message)


__all__ = ["StorageAdapterService", "StorageAdapterServiceError"]
