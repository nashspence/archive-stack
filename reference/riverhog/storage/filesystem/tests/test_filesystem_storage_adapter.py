from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from riverhog_storage_adapter_filesystem import (
    FilesystemStorageAdapter,
    FilesystemStorageAdapterConfig,
)
from riverhog_storage_adapter_protocol import (
    CompletedWriteLookupRequest,
    DeleteObjectRequest,
    DeletePrefixRequest,
    ObjectHeadRequest,
    ObjectLocator,
    ObjectReadRequest,
    ReadPreparationRequest,
    SmallObjectWriteRequest,
    StorageAdapterRejection,
    WriteCompleteRequest,
    WriteStartRequest,
)

SEGMENT_BYTES = 64 * 1024


def _adapter(
    root: Path,
    *,
    minimum_free_bytes: int = 0,
) -> FilesystemStorageAdapter:
    return FilesystemStorageAdapter(
        FilesystemStorageAdapterConfig(
            root=root,
            segment_bytes=SEGMENT_BYTES,
            read_chunk_bytes=SEGMENT_BYTES,
            minimum_free_bytes=minimum_free_bytes,
        )
    )


def _start(path: str, size: int):
    return WriteStartRequest(
        object_path=path,
        expected_bytes=size,
        content_type="application/octet-stream",
        required_identity_assertions={"riverhog-object": path},
        placement="immediate",
    )


def test_descriptor_is_immediate_and_fixed_slot(tmp_path: Path) -> None:
    with _adapter(tmp_path / "store") as adapter:
        descriptor = adapter.descriptor()
        assert descriptor.implementation_id == "riverhog.filesystem/v1"
        assert descriptor.read_mode == "immediate"
        assert descriptor.minimum_nonfinal_segment_bytes == SEGMENT_BYTES
        assert descriptor.maximum_segment_bytes == SEGMENT_BYTES
        assert descriptor.maximum_segment_count is None
        adapter.readiness()


def test_resumable_write_survives_restart_and_supports_exact_reads(tmp_path: Path) -> None:
    root = tmp_path / "store"
    payload = bytes(index % 251 for index in range(SEGMENT_BYTES + 12345))
    request = _start("collections/1/objects/payload.age", len(payload))

    adapter = _adapter(root)
    session = adapter.begin_write(request)
    assert adapter.begin_write(request) == session
    first = adapter.write_segment(
        session=session,
        number=1,
        stored_bytes=SEGMENT_BYTES,
        content=(payload[:1000], payload[1000:SEGMENT_BYTES]),
    )
    adapter.close()

    adapter = _adapter(root)
    try:
        resumed = adapter.begin_write(request)
        assert resumed == session
        assert adapter.list_segments(resumed).segments == (first,)
        second = adapter.write_segment(
            session=resumed,
            number=2,
            stored_bytes=len(payload) - SEGMENT_BYTES,
            content=payload[SEGMENT_BYTES:],
        )
        receipt = adapter.complete_write(
            WriteCompleteRequest(
                session=resumed,
                segments=(first, second),
                expected_bytes=len(payload),
                expected_content_type=request.content_type,
                required_identity_assertions=request.required_identity_assertions,
                expected_placement="immediate",
            )
        )
        assert receipt.stored_bytes == len(payload)
        assert receipt.revision
        assert receipt.entity_token == receipt.revision

        recovered = adapter.find_completed_write(
            CompletedWriteLookupRequest(
                object_path=request.object_path,
                expected_bytes=len(payload),
                expected_content_type=request.content_type,
                required_identity_assertions=request.required_identity_assertions,
                expected_placement="immediate",
            )
        )
        assert recovered == receipt

        head = adapter.head_object(
            ObjectHeadRequest(
                object=ObjectLocator(object_path=request.object_path),
                expected_placement="immediate",
            )
        )
        assert head is not None
        assert head.stored_sha256 is None

        with adapter.read_object(
            ObjectReadRequest(
                object=ObjectLocator(object_path=request.object_path),
                expected_bytes=len(payload),
            )
        ) as stream:
            assert b"".join(stream.content) == payload

        with adapter.read_object(
            ObjectReadRequest(
                object=ObjectLocator(object_path=request.object_path, revision=receipt.revision),
                expected_bytes=len(payload),
                offset=70000,
                size=4096,
            )
        ) as stream:
            assert b"".join(stream.content) == payload[70000:74096]
            assert stream.receipt.offset == 70000
            assert stream.receipt.read_bytes == 4096
    finally:
        adapter.close()


def test_exact_admission_is_removed_by_abort(tmp_path: Path) -> None:
    root = tmp_path / "store"
    with _adapter(root) as adapter:
        first = adapter.begin_write(_start("one", SEGMENT_BYTES))
        assert tuple((root / "writes").rglob("payload.data"))
        adapter.abort_write(first)
        assert not tuple((root / "writes").rglob("payload.data"))
        second = adapter.begin_write(_start("two", 2048))
        assert second.expected_bytes == 2048


def test_ambiguous_begin_is_idempotent_and_conflicting_begin_is_rejected(
    tmp_path: Path,
) -> None:
    with _adapter(tmp_path / "store") as adapter:
        exact = _start("same/path", SEGMENT_BYTES)
        session = adapter.begin_write(exact)
        assert adapter.begin_write(exact) == session
        with pytest.raises(StorageAdapterRejection) as rejected:
            adapter.begin_write(_start("same/path", SEGMENT_BYTES + 1))
        assert rejected.value.code == "identity_conflict"


def test_failed_segment_is_not_catalogued_and_can_be_retried(tmp_path: Path) -> None:
    payload = b"x" * SEGMENT_BYTES
    with _adapter(tmp_path / "store") as adapter:
        session = adapter.begin_write(_start("retry", SEGMENT_BYTES))
        with pytest.raises(StorageAdapterRejection) as rejected:
            adapter.write_segment(
                session=session,
                number=1,
                stored_bytes=SEGMENT_BYTES,
                content=payload[:-1],
            )
        assert rejected.value.code == "integrity_failure"
        assert adapter.list_segments(session).segments == ()
        accepted = adapter.write_segment(
            session=session,
            number=1,
            stored_bytes=SEGMENT_BYTES,
            content=payload,
        )
        assert adapter.list_segments(session).segments == (accepted,)


def test_small_object_create_replace_revision_and_delete(tmp_path: Path) -> None:
    with _adapter(tmp_path / "store") as adapter:
        first_payload = b"first"
        first_request = SmallObjectWriteRequest(
            object_path="metadata/root.json",
            content_type="application/json",
            required_identity_assertions={"authority": "first"},
            placement="immediate",
            mode="create_only",
            stored_bytes=len(first_payload),
            stored_sha256=hashlib.sha256(first_payload).hexdigest(),
        )
        first = adapter.put_small_object(first_request, first_payload)
        assert adapter.put_small_object(first_request, first_payload) == first

        different_create = first_request.model_copy(
            update={
                "required_identity_assertions": {"authority": "second"},
                "stored_sha256": hashlib.sha256(b"second").hexdigest(),
                "stored_bytes": len(b"second"),
            }
        )
        with pytest.raises(StorageAdapterRejection) as rejected:
            adapter.put_small_object(different_create, b"second")
        assert rejected.value.code == "identity_conflict"

        second_request = different_create.model_copy(
            update={
                "mode": "replace_current",
                "expected_current_stored_sha256": first.stored_sha256,
            }
        )
        second = adapter.put_small_object(second_request, b"second")
        assert second.revision != first.revision

        with pytest.raises(StorageAdapterRejection) as stale_write:
            adapter.put_small_object(
                second_request.model_copy(
                    update={
                        "required_identity_assertions": {"authority": "stale"},
                        "stored_bytes": 5,
                        "stored_sha256": hashlib.sha256(b"stale").hexdigest(),
                    }
                ),
                b"stale",
            )
        assert stale_write.value.code == "identity_conflict"
        with pytest.raises(StorageAdapterRejection) as stale_delete:
            adapter.delete_object(
                DeleteObjectRequest(
                    object=ObjectLocator(object_path=second.object_path),
                    mode="current",
                    expected_current_stored_sha256=first.stored_sha256,
                )
            )
        assert stale_delete.value.code == "identity_conflict"

        old = adapter.head_object(
            ObjectHeadRequest(
                object=ObjectLocator(
                    object_path=first.object_path,
                    revision=first.revision,
                ),
                expected_placement="immediate",
            )
        )
        assert old is not None and old.stored_sha256 == first.stored_sha256

        adapter.delete_object(
            DeleteObjectRequest(
                object=ObjectLocator(
                    object_path=first.object_path,
                    revision=first.revision,
                ),
                mode="exact_revision",
            )
        )
        assert (
            adapter.head_object(
                ObjectHeadRequest(
                    object=ObjectLocator(
                        object_path=first.object_path,
                        revision=first.revision,
                    ),
                    expected_placement="immediate",
                )
            )
            is None
        )
        assert (
            adapter.head_object(
                ObjectHeadRequest(
                    object=ObjectLocator(object_path=second.object_path),
                    expected_placement="immediate",
                )
            )
            is not None
        )


def test_delete_prefix_and_immediate_read_status(tmp_path: Path) -> None:
    with _adapter(tmp_path / "store") as adapter:
        for path in ("cache/a", "cache/b", "other/c"):
            payload = path.encode()
            adapter.put_small_object(
                SmallObjectWriteRequest(
                    object_path=path,
                    content_type="application/octet-stream",
                    required_identity_assertions={"path": path},
                    placement="immediate",
                    mode="create_only",
                    stored_bytes=len(payload),
                    stored_sha256=hashlib.sha256(payload).hexdigest(),
                ),
                payload,
            )
        assert adapter.delete_prefix(DeletePrefixRequest(object_prefix="cache/")) == 2
        assert (
            adapter.head_object(
                ObjectHeadRequest(
                    object=ObjectLocator(object_path="other/c"),
                    expected_placement="immediate",
                )
            )
            is not None
        )
        read_request = ReadPreparationRequest(objects=(ObjectLocator(object_path="other/c"),))
        assert adapter.prepare_read(read_request).readiness.state == "ready"
        assert adapter.read_status(read_request).readiness.state == "ready"
        adapter.cleanup_read(read_request)


def test_one_process_owns_a_root(tmp_path: Path) -> None:
    root = tmp_path / "store"
    first = _adapter(root)
    try:
        with pytest.raises(RuntimeError, match="already owned"):
            _adapter(root)
    finally:
        first.close()
    with _adapter(root):
        pass


def test_sparse_segment_number_is_reconciled_and_abort_releases_blocks(tmp_path: Path) -> None:
    root = tmp_path / "store"
    request = _start("sparse", SEGMENT_BYTES)
    adapter = _adapter(root)
    session = adapter.begin_write(request)
    sparse = adapter.write_segment(
        session=session,
        number=2,
        stored_bytes=SEGMENT_BYTES,
        content=b"s" * SEGMENT_BYTES,
    )
    adapter.close()

    with _adapter(root) as resumed:
        assert resumed.begin_write(request) == session
        assert resumed.list_segments(session).segments == (sparse,)
        resumed.abort_write(session)
        resumed.begin_write(_start("replacement", SEGMENT_BYTES))


def test_segment_replay_and_completion_replay_are_exact(tmp_path: Path) -> None:
    content = b"a" * SEGMENT_BYTES
    request = _start("replay", len(content))
    with _adapter(tmp_path / "store") as adapter:
        session = adapter.begin_write(request)
        segment = adapter.write_segment(
            session=session,
            number=1,
            stored_bytes=len(content),
            content=content,
        )
        assert (
            adapter.write_segment(
                session=session,
                number=1,
                stored_bytes=len(content),
                content=(content[:17], content[17:]),
            )
            == segment
        )
        with pytest.raises(StorageAdapterRejection) as rejected:
            adapter.write_segment(
                session=session,
                number=1,
                stored_bytes=len(content),
                content=b"b" * len(content),
            )
        assert rejected.value.code == "identity_conflict"

        completion = WriteCompleteRequest(
            session=session,
            segments=(segment,),
            expected_bytes=len(content),
            expected_content_type=request.content_type,
            required_identity_assertions=request.required_identity_assertions,
            expected_placement=request.placement,
        )
        receipt = adapter.complete_write(completion)
        assert adapter.complete_write(completion) == receipt


def test_completion_does_not_reread_the_completed_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import os

    content = b"a" * SEGMENT_BYTES
    request = _start("no-completion-reread", len(content))
    with _adapter(tmp_path / "store") as adapter:
        session = adapter.begin_write(request)
        segment = adapter.write_segment(
            session=session,
            number=1,
            stored_bytes=len(content),
            content=content,
        )

        def unexpected_pread(fd: int, length: int, offset: int) -> bytes:
            del fd, length, offset
            raise AssertionError("completion reread the accepted payload")

        monkeypatch.setattr(os, "pread", unexpected_pread)
        receipt = adapter.complete_write(
            WriteCompleteRequest(
                session=session,
                segments=(segment,),
                expected_bytes=len(content),
                expected_content_type=request.content_type,
                required_identity_assertions=request.required_identity_assertions,
                expected_placement=request.placement,
            )
        )
        assert receipt.entity_token == receipt.revision


def test_begin_physically_preallocates_the_exact_payload(tmp_path: Path) -> None:
    root = tmp_path / "store"
    expected = 2 * SEGMENT_BYTES
    with _adapter(root) as adapter:
        adapter.begin_write(_start("reserved", expected))
        payloads = tuple((root / "writes").glob("*/*/*/payload.data"))
        assert len(payloads) == 1
        details = payloads[0].stat()
        assert details.st_size == expected
        # posix_fallocate must reserve blocks rather than creating a sparse promise.
        assert details.st_blocks * 512 >= expected


def test_free_space_rejection_happens_before_small_payload_is_consumed(tmp_path: Path) -> None:
    consumed = False

    def content():
        nonlocal consumed
        consumed = True
        yield b"x"

    with _adapter(tmp_path / "store", minimum_free_bytes=10**30) as adapter:
        request = SmallObjectWriteRequest(
            object_path="small",
            content_type="application/octet-stream",
            required_identity_assertions={"kind": "small"},
            placement="immediate",
            mode="create_only",
            stored_bytes=1,
            stored_sha256=hashlib.sha256(b"x").hexdigest(),
        )
        with pytest.raises(StorageAdapterRejection) as rejected:
            adapter.put_small_object(request, content())
        assert rejected.value.code == "insufficient_storage"
        assert not consumed


def test_all_versions_delete_reclaims_payload(tmp_path: Path) -> None:
    root = tmp_path / "store"
    payload = b"x" * SEGMENT_BYTES
    with _adapter(root) as adapter:
        request = SmallObjectWriteRequest(
            object_path="first",
            content_type="application/octet-stream",
            required_identity_assertions={"kind": "first"},
            placement="immediate",
            mode="create_only",
            stored_bytes=len(payload),
            stored_sha256=hashlib.sha256(payload).hexdigest(),
        )
        adapter.put_small_object(request, payload)
        assert tuple((root / "objects").rglob("payload.data"))
        adapter.delete_object(
            DeleteObjectRequest(
                object=ObjectLocator(object_path="first"),
                mode="all_versions",
            )
        )
        assert not tuple((root / "objects").rglob("payload.data"))


def test_zero_byte_small_object_round_trips(tmp_path: Path) -> None:
    empty_sha256 = hashlib.sha256(b"").hexdigest()
    request = SmallObjectWriteRequest(
        object_path="empty",
        content_type="application/octet-stream",
        required_identity_assertions={"kind": "empty"},
        placement="immediate",
        mode="create_only",
        stored_bytes=0,
        stored_sha256=empty_sha256,
    )
    with _adapter(tmp_path / "store") as adapter:
        receipt = adapter.put_small_object(request, b"")
        assert receipt.stored_bytes == 0
        assert receipt.stored_sha256 == empty_sha256
        with adapter.read_object(
            ObjectReadRequest(
                object=ObjectLocator(object_path="empty"),
                expected_bytes=0,
            )
        ) as stream:
            assert b"".join(stream.content) == b""


def test_out_of_order_segments_complete_in_logical_number_order(tmp_path: Path) -> None:
    first_content = b"a" * SEGMENT_BYTES
    final_content = b"final"
    request = _start("out-of-order", len(first_content) + len(final_content))
    with _adapter(tmp_path / "store") as adapter:
        session = adapter.begin_write(request)
        second = adapter.write_segment(
            session=session,
            number=2,
            stored_bytes=len(final_content),
            content=final_content,
        )
        first = adapter.write_segment(
            session=session,
            number=1,
            stored_bytes=len(first_content),
            content=first_content,
        )
        receipt = adapter.complete_write(
            WriteCompleteRequest(
                session=session,
                segments=(first, second),
                expected_bytes=request.expected_bytes,
                expected_content_type=request.content_type,
                required_identity_assertions=request.required_identity_assertions,
                expected_placement=request.placement,
            )
        )
        with adapter.read_object(
            ObjectReadRequest(
                object=ObjectLocator(
                    object_path=request.object_path,
                    revision=receipt.revision,
                ),
                expected_bytes=request.expected_bytes,
            )
        ) as stream:
            assert b"".join(stream.content) == first_content + final_content


def test_concurrent_exact_admission_reserves_complete_files(tmp_path: Path) -> None:
    import concurrent.futures

    root = tmp_path / "store"
    with _adapter(root) as adapter:

        def admit(path: str) -> str:
            try:
                adapter.begin_write(_start(path, SEGMENT_BYTES))
            except StorageAdapterRejection as exc:
                return exc.code
            return "admitted"

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = sorted(executor.map(admit, ("one", "two")))
        assert outcomes == ["admitted", "admitted"]
        payloads = tuple((root / "writes").rglob("payload.data"))
        assert len(payloads) == 2
        assert all(path.stat().st_blocks * 512 >= SEGMENT_BYTES for path in payloads)


def test_physical_allocation_failure_maps_to_507_before_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import errno
    import os

    consumed = False

    def content():
        nonlocal consumed
        consumed = True
        yield b"x"

    def no_space(fd: int, offset: int, length: int) -> None:
        del fd, offset, length
        raise OSError(errno.ENOSPC, "test capacity exhausted")

    with _adapter(tmp_path / "store") as adapter:
        monkeypatch.setattr(os, "posix_fallocate", no_space)
        request = SmallObjectWriteRequest(
            object_path="small-no-space",
            content_type="application/octet-stream",
            required_identity_assertions={"kind": "small"},
            placement="immediate",
            mode="create_only",
            stored_bytes=1,
            stored_sha256=hashlib.sha256(b"x").hexdigest(),
        )
        with pytest.raises(StorageAdapterRejection) as rejected:
            adapter.put_small_object(request, content())
        assert rejected.value.code == "insufficient_storage"
        assert not consumed


def test_symbolic_link_root_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic link"):
        _adapter(linked)


def test_streamed_read_holds_deletion_until_closed(tmp_path: Path) -> None:
    import concurrent.futures
    import time

    payload = b"a" * (2 * SEGMENT_BYTES)
    request = SmallObjectWriteRequest(
        object_path="held-read",
        content_type="application/octet-stream",
        required_identity_assertions={"kind": "held-read"},
        placement="immediate",
        mode="create_only",
        stored_bytes=len(payload),
        stored_sha256=hashlib.sha256(payload).hexdigest(),
    )
    with _adapter(tmp_path / "store") as adapter:
        adapter.put_small_object(request, payload)
        stream = adapter.read_object(
            ObjectReadRequest(
                object=ObjectLocator(object_path=request.object_path),
                expected_bytes=len(payload),
            )
        )
        assert next(stream.content) == payload[:SEGMENT_BYTES]

        def delete() -> None:
            adapter.delete_object(
                DeleteObjectRequest(
                    object=ObjectLocator(object_path=request.object_path),
                    mode="all_versions",
                )
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(delete)
            time.sleep(0.03)
            assert not future.done()
            stream.close()
            future.result(timeout=1)
        assert (
            adapter.head_object(
                ObjectHeadRequest(
                    object=ObjectLocator(object_path=request.object_path),
                    expected_placement="immediate",
                )
            )
            is None
        )


def test_stale_completion_does_not_abort_a_newer_active_session(tmp_path: Path) -> None:
    payload = b"a" * SEGMENT_BYTES
    first_request = _start("same-object", len(payload))
    with _adapter(tmp_path / "store") as adapter:
        first_session = adapter.begin_write(first_request)
        first_segment = adapter.write_segment(
            session=first_session,
            number=1,
            stored_bytes=len(payload),
            content=payload,
        )
        first_completion = WriteCompleteRequest(
            session=first_session,
            segments=(first_segment,),
            expected_bytes=len(payload),
            expected_content_type=first_request.content_type,
            required_identity_assertions=first_request.required_identity_assertions,
            expected_placement=first_request.placement,
        )
        first_receipt = adapter.complete_write(first_completion)

        second_request = first_request.model_copy(
            update={"required_identity_assertions": {"riverhog-object": "replacement"}}
        )
        second_session = adapter.begin_write(second_request)
        assert adapter.complete_write(first_completion) == first_receipt
        assert adapter.list_segments(second_session).segments == ()


def test_restart_finalizes_revision_installed_before_current_pointer(tmp_path: Path) -> None:
    root = tmp_path / "store"
    payload = b"restart-window" * 4096
    request = _start("crash/finalize.bin", len(payload))

    adapter = _adapter(root)
    session = adapter.begin_write(request)
    segment = adapter.write_segment(
        session=session,
        number=1,
        stored_bytes=len(payload),
        content=payload,
    )
    completion = WriteCompleteRequest(
        session=session,
        segments=(segment,),
        expected_bytes=len(payload),
        expected_content_type=request.content_type,
        required_identity_assertions=request.required_identity_assertions,
        expected_placement=request.placement,
    )
    original_write_text_atomic = adapter._write_text_atomic

    def fail_current_pointer(path: Path, value: str) -> None:
        if path.name == "current":
            raise OSError("simulated crash before current-pointer publication")
        original_write_text_atomic(path, value)

    adapter._write_text_atomic = fail_current_pointer  # type: ignore[method-assign]
    try:
        with pytest.raises(OSError, match="simulated crash"):
            adapter.complete_write(completion)
    finally:
        adapter.close()

    with _adapter(root) as recovered:
        completed = recovered.find_completed_write(
            CompletedWriteLookupRequest(
                object_path=request.object_path,
                expected_bytes=len(payload),
                expected_content_type=request.content_type,
                required_identity_assertions=request.required_identity_assertions,
                expected_placement=request.placement,
            )
        )
        assert completed is not None
        assert completed.revision == session.write_token
        with recovered.read_object(
            ObjectReadRequest(
                object=ObjectLocator(
                    object_path=request.object_path,
                    revision=completed.revision,
                ),
                expected_bytes=len(payload),
            )
        ) as stream:
            assert b"".join(stream.content) == payload
        with pytest.raises(StorageAdapterRejection, match="write session"):
            recovered.list_segments(session)
