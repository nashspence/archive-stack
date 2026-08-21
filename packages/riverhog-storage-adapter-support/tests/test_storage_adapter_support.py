from __future__ import annotations

import ast
import hashlib
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import httpx
import pytest
from fastapi.testclient import TestClient
from riverhog_storage_adapter_protocol import (
    CompleteUploadRequest,
    ObjectLocator,
    ObjectReceipt,
    ReadRequest,
    ReadStatus,
    StorageAdapterDescriptor,
    StorageAdapterDescriptorPayload,
    StorageProfile,
    StorageProfilePayload,
    UploadDeclaration,
    UploadDeclarationPayload,
    UploadPartReceipt,
    WriteCondition,
)
from riverhog_storage_adapter_support import (
    STORAGE_ADAPTER_SCHEMA_BUNDLE_FORMAT,
    ProviderUpload,
    StorageAdapterClient,
    StorageAdapterService,
    StorageAdapterServiceError,
    StorageDriverError,
    UploadJournal,
    conformance_report,
    create_storage_adapter_app,
    storage_adapter_schema_bundle,
)

SHA_EMPTY = hashlib.sha256(b"").hexdigest()


class MemoryDriver:
    def __init__(self, *, implementation_id: str = "memory.adapter/v1") -> None:
        profile = StorageProfile.seal(
            StorageProfilePayload(
                profile_id="memory.immediate/v1",
                read_mode="immediate",
                egress_accounting_id="memory",
            )
        )
        self._descriptor = StorageAdapterDescriptor.seal(
            StorageAdapterDescriptorPayload(
                implementation_id=implementation_id,
                implementation_version="1",
                source_revision="test",
                profile=profile,
                minimum_nonfinal_part_bytes=1,
                maximum_part_bytes=1024**2,
                maximum_part_count=100,
            )
        )
        self.uploads: dict[str, dict[int, bytes]] = {}
        self.objects: dict[tuple[str, str], tuple[ObjectReceipt, bytes]] = {}

    def descriptor(self) -> StorageAdapterDescriptor:
        return self._descriptor

    def ready(self) -> None:
        return

    def create_upload(self, declaration: UploadDeclaration) -> ProviderUpload:
        upload_id = f"provider:{declaration.transfer_id}"
        self.uploads.setdefault(upload_id, {})
        return ProviderUpload(upload_id)

    def upload_part(
        self,
        *,
        declaration: UploadDeclaration,
        upload: ProviderUpload,
        number: int,
        content: bytes,
        stored_sha256: str,
    ) -> str:
        _ = declaration
        if hashlib.sha256(content).hexdigest() != stored_sha256:
            raise StorageDriverError("integrity_failure", "part digest changed")
        current = self.uploads[upload.upload_id].get(number)
        if current is not None and current != content:
            raise StorageDriverError("upload_conflict", "part changed")
        self.uploads[upload.upload_id][number] = content
        return f"part:{number}:{stored_sha256}"

    def complete_upload(
        self,
        *,
        declaration: UploadDeclaration,
        upload: ProviderUpload,
        completion: CompleteUploadRequest,
    ) -> ObjectReceipt:
        content = b"".join(self.uploads[upload.upload_id][part.number] for part in completion.parts)
        digest = hashlib.sha256(content).hexdigest()
        if len(content) != completion.stored_bytes or digest != completion.stored_sha256:
            raise StorageDriverError("integrity_failure", "completed object changed")
        revision = f"memory:{digest}"
        receipt = ObjectReceipt(
            object_path=declaration.object_path,
            revision=revision,
            content_type=declaration.content_type,
            stored_bytes=len(content),
            stored_sha256=digest,
            completed_at="2026-08-21T00:00:00Z",
        )
        key = (declaration.object_path, revision)
        existing = self.objects.get(key)
        if existing is not None and existing != (receipt, content):
            raise StorageDriverError("revision_conflict", "object changed")
        self.objects[key] = (receipt, content)
        return receipt

    def abort_upload(
        self,
        *,
        declaration: UploadDeclaration,
        upload: ProviderUpload,
    ) -> None:
        _ = declaration
        self.uploads.pop(upload.upload_id, None)

    def verify_object(self, receipt: ObjectReceipt) -> None:
        try:
            observed = self.objects[(receipt.object_path, receipt.revision)][0]
        except KeyError as exc:
            raise StorageDriverError("not_found", "object does not exist") from exc
        if observed != receipt:
            raise StorageDriverError("integrity_failure", "object receipt changed")

    def iter_object_content(
        self,
        locator: ObjectLocator,
        *,
        offset: int | None,
        size: int | None,
    ) -> Iterator[bytes]:
        try:
            content = self.objects[(locator.object_path, locator.revision)][1]
        except KeyError as exc:
            raise StorageDriverError("not_found", "object does not exist") from exc
        start = offset or 0
        end = len(content) if size is None else start + size
        for index in range(start, end, 3):
            yield content[index : min(index + 3, end)]

    def delete_object(self, locator: ObjectLocator) -> None:
        self.objects.pop((locator.object_path, locator.revision), None)

    def delete_prefix(self, object_prefix: str) -> int:
        matches = [key for key in self.objects if key[0].startswith(object_prefix)]
        for key in matches:
            del self.objects[key]
        return len(matches)

    def prepare_read(self, request: ReadRequest) -> ReadStatus:
        for locator in request.objects:
            try:
                self.objects[(locator.object_path, locator.revision)]
            except KeyError as exc:
                raise StorageDriverError("not_found", "object does not exist") from exc
        return ReadStatus(state="ready")

    def read_status(self, request: ReadRequest) -> ReadStatus:
        return self.prepare_read(request)

    def cleanup_read(self, request: ReadRequest) -> None:
        _ = request

    def abort_incomplete_uploads(self, *, initiated_before: str) -> int:
        _ = initiated_before
        return 0

    def verify_part_receipt(
        self,
        *,
        declaration: UploadDeclaration,
        upload: ProviderUpload,
        receipt: UploadPartReceipt,
    ) -> None:
        _ = declaration
        content = self.uploads[upload.upload_id][receipt.number]
        if (
            len(content) != receipt.stored_bytes
            or hashlib.sha256(content).hexdigest() != receipt.stored_sha256
        ):
            raise StorageDriverError("integrity_failure", "part receipt changed")


def _service(tmp_path: Path, driver: MemoryDriver | None = None) -> StorageAdapterService:
    return StorageAdapterService(
        driver=driver or MemoryDriver(),
        journal=UploadJournal(tmp_path / "adapter.sqlite3"),
    )


def _client(tmp_path: Path) -> tuple[StorageAdapterClient, MemoryDriver]:
    driver = MemoryDriver()
    app = create_storage_adapter_app(
        service_name="memory-adapter",
        token="secret",
        service=_service(tmp_path, driver),
    )
    transport = TestClient(app)
    return (
        StorageAdapterClient(
            "http://testserver",
            token="secret",
            allow_insecure_http=True,
            client=cast(httpx.Client, transport),
        ),
        driver,
    )


def test_full_http_conformance_and_exact_range(tmp_path: Path) -> None:
    client, driver = _client(tmp_path)
    report = conformance_report(client, object_prefix="conformance")

    assert report["status"] == "ok"
    assert report["object_probe"]["observed_state"] == "ready"  # type: ignore[index]
    assert driver.objects == {}


def test_upload_journal_survives_service_restart(tmp_path: Path) -> None:
    driver = MemoryDriver()
    service = _service(tmp_path, driver)
    content = b"restartable"
    declaration = UploadDeclaration.seal(
        UploadDeclarationPayload(
            transfer_id="restartable",
            object_path="archives/restartable.bin",
            content_type="application/octet-stream",
            stored_bytes=len(content),
            runtime_descriptor_sha256=driver.descriptor().runtime_descriptor_sha256,
            condition=WriteCondition(),
        )
    )
    service.put_upload(declaration)
    part = service.put_part(
        transfer_id=declaration.transfer_id,
        number=1,
        content=content,
        stored_sha256=hashlib.sha256(content).hexdigest(),
    )

    restarted = _service(tmp_path, driver)
    assert restarted.get_upload(declaration.transfer_id).parts == (part,)
    receipt = restarted.complete_upload(
        transfer_id=declaration.transfer_id,
        completion=CompleteUploadRequest(
            parts=(part,),
            stored_bytes=len(content),
            stored_sha256=hashlib.sha256(content).hexdigest(),
        ),
    )
    assert receipt.stored_sha256 == hashlib.sha256(content).hexdigest()


def test_transfer_id_conflict_and_changed_part_fail_closed(tmp_path: Path) -> None:
    service = _service(tmp_path)
    first = UploadDeclaration.seal(
        UploadDeclarationPayload(
            transfer_id="one",
            object_path="archives/one",
            content_type="application/octet-stream",
            stored_bytes=1,
            runtime_descriptor_sha256=service.descriptor().runtime_descriptor_sha256,
        )
    )
    changed = UploadDeclaration.seal(
        UploadDeclarationPayload(
            transfer_id="one",
            object_path="archives/two",
            content_type="application/octet-stream",
            stored_bytes=1,
            runtime_descriptor_sha256=service.descriptor().runtime_descriptor_sha256,
        )
    )
    service.put_upload(first)
    with pytest.raises(Exception, match="another request"):
        service.put_upload(changed)
    service.put_part(
        transfer_id="one",
        number=1,
        content=b"a",
        stored_sha256=hashlib.sha256(b"a").hexdigest(),
    )
    with pytest.raises(Exception, match="different bytes"):
        service.put_part(
            transfer_id="one",
            number=1,
            content=b"b",
            stored_sha256=hashlib.sha256(b"b").hexdigest(),
        )


def test_plain_http_requires_explicit_operator_opt_in() -> None:
    with pytest.raises(ValueError, match="explicit insecure opt-in"):
        StorageAdapterClient("http://adapter.example", token="secret")

    client = StorageAdapterClient(
        "http://adapter.example",
        token="secret",
        allow_insecure_http=True,
        client=httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(500))),
    )
    client.close()


def test_protocol_support_has_no_server_or_provider_dependencies() -> None:
    root = Path(__file__).resolve().parents[1] / "src"
    forbidden = {
        "boto3",
        "botocore",
        "riverhog_api",
        "riverhog_core",
        "riverhog_aws_storage_adapter",
        "riverhog_backblaze_storage_adapter",
        "riverhog_garage_storage_adapter",
    }
    observed: set[str] = set()
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                observed.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                observed.add(node.module.split(".", 1)[0])
    assert observed.isdisjoint(forbidden)


def test_zero_byte_digest_is_well_known() -> None:
    assert SHA_EMPTY == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_schema_bundle_covers_the_complete_public_authoring_and_wire_surface() -> None:
    bundle = storage_adapter_schema_bundle()

    assert bundle["format"] == STORAGE_ADAPTER_SCHEMA_BUNDLE_FORMAT
    assert set(bundle["schemas"]) == {  # type: ignore[arg-type]
        "AbortIncompleteUploadsRequest",
        "CompleteUploadRequest",
        "MaintenanceResult",
        "ObjectDeleteRequest",
        "ObjectLocator",
        "ObjectReceipt",
        "PrefixDeleteRequest",
        "ReadRequest",
        "ReadStatus",
        "StorageAdapterDescriptor",
        "StorageAdapterDescriptorPayload",
        "StorageAdapterError",
        "StorageProfile",
        "StorageProfilePayload",
        "UploadDeclaration",
        "UploadDeclarationPayload",
        "UploadPartReceipt",
        "UploadStatus",
        "WriteCondition",
    }


def test_compatible_runtime_substitution_preserves_finalized_objects_and_fences_open_work(
    tmp_path: Path,
) -> None:
    driver = MemoryDriver(implementation_id="memory.first/v1")
    service = _service(tmp_path, driver)
    first_content = b"finalized"
    first = UploadDeclaration.seal(
        UploadDeclarationPayload(
            transfer_id="finalized-before-substitution",
            object_path="archives/finalized.bin",
            content_type="application/octet-stream",
            stored_bytes=len(first_content),
            runtime_descriptor_sha256=driver.descriptor().runtime_descriptor_sha256,
        )
    )
    service.put_upload(first)
    first_part = service.put_part(
        transfer_id=first.transfer_id,
        number=1,
        content=first_content,
        stored_sha256=hashlib.sha256(first_content).hexdigest(),
    )
    receipt = service.complete_upload(
        transfer_id=first.transfer_id,
        completion=CompleteUploadRequest(
            parts=(first_part,),
            stored_bytes=len(first_content),
            stored_sha256=hashlib.sha256(first_content).hexdigest(),
        ),
    )
    open_declaration = UploadDeclaration.seal(
        UploadDeclarationPayload(
            transfer_id="open-before-substitution",
            object_path="archives/open.bin",
            content_type="application/octet-stream",
            stored_bytes=1,
            runtime_descriptor_sha256=driver.descriptor().runtime_descriptor_sha256,
        )
    )
    service.put_upload(open_declaration)

    original = driver.descriptor()
    driver._descriptor = StorageAdapterDescriptor.seal(
        StorageAdapterDescriptorPayload(
            implementation_id="memory.replacement/v1",
            implementation_version="2",
            source_revision="replacement",
            profile=original.profile,
            minimum_nonfinal_part_bytes=original.minimum_nonfinal_part_bytes,
            maximum_part_bytes=original.maximum_part_bytes,
            maximum_part_count=original.maximum_part_count,
        )
    )

    assert (
        service.object_metadata(
            ObjectLocator(object_path=receipt.object_path, revision=receipt.revision)
        )
        == receipt
    )
    with pytest.raises(StorageAdapterServiceError, match="runtime descriptor changed") as error:
        service.put_part(
            transfer_id=open_declaration.transfer_id,
            number=1,
            content=b"x",
            stored_sha256=hashlib.sha256(b"x").hexdigest(),
        )
    assert error.value.code == "upload_conflict"
