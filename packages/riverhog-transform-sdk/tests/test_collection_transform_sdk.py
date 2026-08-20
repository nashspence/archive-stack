from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from riverhog_api_client import ApiClient
from riverhog_api_client.producer import (
    CollectionProducer,
    ProducedCollection,
    ProducerFile,
    ProducerStream,
)
from riverhog_protocol.collection_workflows import (
    DERIVATION_EVIDENCE_PATH,
    PRODUCER_EVIDENCE_PATH,
    ArtifactDisposition,
    CollectionDerivation,
    CollectionRootIdentity,
    OperationIdentity,
    RecipeIdentity,
)
from riverhog_protocol.errors import InvalidState
from riverhog_transform_sdk import (
    ClaimedCollectionReader,
    CollectionTransformRuntime,
    DerivedCollectionReceipt,
    DerivedCollectionSpec,
    DerivedCollectionWriter,
    TransformWorkspace,
)

WORK_ID = "3" * 64
EXECUTION_ID = "4" * 64
CONTROLLER_EVIDENCE = {
    "format": "stove0-controller-evidence/v1",
    "execution_id": EXECUTION_ID,
}
CONTROLLER_EVIDENCE_SHA256 = hashlib.sha256(
    json.dumps(CONTROLLER_EVIDENCE, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()


def _spec() -> DerivedCollectionSpec:
    return DerivedCollectionSpec(
        recipe=RecipeIdentity("camera/v1", 1, "a" * 64),
        operation=OperationIdentity("archive-video/v1", "b" * 64),
        inputs=(CollectionRootIdentity(1, "1" * 64, "2" * 64),),
        output_tags=("archive-camera",),
    )


def _disposition(spec: DerivedCollectionSpec) -> ArtifactDisposition:
    return ArtifactDisposition(
        input_collection_id=1,
        input_manifest_sha256=spec.inputs[0].manifest_sha256,
        input_path="camera/input.mov",
        status="transformed",
        outputs=("video/output.mkv",),
    )


class RetrievalApi:
    def __init__(self, *, changed_root: bool = False) -> None:
        self.data = b"immutable input"
        self.sha256 = hashlib.sha256(self.data).hexdigest()
        self.changed_root = changed_root
        self.acknowledged: list[str] = []
        self.canceled: list[str] = []
        self.restore_policies: list[str] = []

    def get_collection(self, collection_id: int) -> dict[str, Any]:
        return {
            "id": collection_id,
            "manifest_sha256": "3" * 64 if self.changed_root else "1" * 64,
            "content_etag": "2" * 64,
        }

    def search(self, _query: str | None = None, **_kwargs: Any) -> dict[str, Any]:
        return {
            "files": [
                {
                    "collection_id": 1,
                    "path": PRODUCER_EVIDENCE_PATH,
                    "bytes": 2,
                    "sha256": hashlib.sha256(b"{}").hexdigest(),
                },
                {
                    "collection_id": 1,
                    "path": "camera/input.mov",
                    "bytes": len(self.data),
                    "sha256": self.sha256,
                },
            ]
        }

    def _rows(self, files: Sequence[tuple[int, str]]) -> list[dict[str, object]]:
        return [
            {
                "collection_id": collection_id,
                "path": path,
                "bytes": len(self.data),
                "sha256": self.sha256,
            }
            for collection_id, path in files
        ]

    def plan_retrieval(
        self,
        files: Sequence[tuple[int, str]],
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.restore_policies.append(str(kwargs["restore_policy"]))
        return {"etag": "9" * 64, "files": self._rows(files)}

    def create_retrieval_job(
        self,
        files: Sequence[tuple[int, str]],
        *,
        plan_etag: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.restore_policies.append(str(kwargs["restore_policy"]))
        return {
            "id": "retrieval-1",
            "state": "ready",
            "plan_etag": plan_etag,
            "files": self._rows(files),
        }

    def get_retrieval_job(self, job_id: str) -> dict[str, Any]:
        raise AssertionError(f"unexpected retrieval poll: {job_id}")

    def renew_retrieval_job(self, job_id: str, *, lease_seconds: int) -> dict[str, Any]:
        return {"id": job_id, "state": "ready", "lease_seconds": lease_seconds}

    def acknowledge_retrieval_job(self, job_id: str) -> dict[str, Any]:
        self.acknowledged.append(job_id)
        return {"id": job_id, "state": "completed"}

    def cancel_retrieval_job(self, job_id: str) -> dict[str, Any]:
        self.canceled.append(job_id)
        return {"id": job_id, "state": "canceled"}

    def download_retrieval_file(
        self,
        _job_id: str,
        *,
        output: Path,
        **_kwargs: Any,
    ) -> int:
        output.write_bytes(self.data)
        return len(self.data)

    @contextmanager
    def stream_retrieval_file(
        self,
        _job_id: str,
        *,
        start: int = 0,
        end: int | None = None,
        **_kwargs: Any,
    ) -> Iterator[Iterator[bytes]]:
        resolved_end = len(self.data) if end is None else end
        yield iter((self.data[start:resolved_end],))


def test_claimed_reader_verifies_roots_filters_control_and_reads_ranges(tmp_path: Path) -> None:
    api = RetrievalApi()
    reader = ClaimedCollectionReader(
        api,  # type: ignore[arg-type]
        inputs=_spec().inputs,
        work_id=WORK_ID,
        claim_id="claim-1",
        fence=1,
    )

    inventory = reader.inventory()

    assert [item.path for item in inventory] == ["camera/input.mov"]
    with reader.prepare(inventory, poll_seconds=0.01) as retrieval:
        assert retrieval.read_bytes(inventory[0], maximum_bytes=1024) == api.data
        with retrieval.stream(inventory[0], start=2, end=7) as chunks:
            assert b"".join(chunks) == api.data[2:7]
        output = tmp_path / "input.mov"
        assert retrieval.download(inventory[0], output) == len(api.data)
        assert output.read_bytes() == api.data

    assert api.acknowledged == ["retrieval-1"]
    assert not api.canceled
    assert api.restore_policies == ["available-only", "available-only"]


def test_claimed_reader_fails_closed_when_root_changed() -> None:
    reader = ClaimedCollectionReader(
        RetrievalApi(changed_root=True),  # type: ignore[arg-type]
        inputs=_spec().inputs,
        work_id=WORK_ID,
        claim_id="claim-1",
        fence=1,
    )

    with pytest.raises(RuntimeError, match="root changed"):
        reader.inventory()


class UploadApi:
    def __init__(self) -> None:
        self.registered: list[dict[str, Any]] = []
        self.uploaded = b""
        self.completion_content_etag = ""
        self.committed = False
        self.discovery_closed = False
        self.volume_list_calls = 0

    def create_or_resume_collection_upload_session(
        self,
        *_args: Any,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        return {
            "collection_id": 7,
            "state": "open",
            "layout": {
                "pack_member_bytes": 1024,
                "raw_part_plaintext_bytes": 65536,
            },
        }

    def register_collection_upload_session_files(
        self,
        _collection_id: int,
        files: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        self.registered = [dict(item) for item in files]
        return {"state": "uploading"}

    def list_collection_upload_session_volumes(self, _collection_id: int) -> dict[str, Any]:
        self.volume_list_calls += 1
        if not self.discovery_closed:
            return {"volumes": []}
        sources = [
            {
                "path": item["path"],
                "offset": 0,
                "bytes": item["bytes"],
                "sha256": item["sha256"],
            }
            for item in self.registered
        ]
        return {
            "volumes": [
                {
                    "volume_id": "pack-000000000000",
                    "plan_sha256": "8" * 64,
                    "units": [
                        {
                            "unit": 0,
                            "payload_bytes": sum(int(item["bytes"]) for item in sources),
                            "sources": sources,
                            "state": "committed" if self.committed else "pending",
                        }
                    ],
                }
            ]
        }

    def put_collection_upload_session_unit(
        self,
        _collection_id: int,
        _volume_id: str,
        _unit: int,
        *,
        content: bytes,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        self.uploaded = content
        self.committed = True
        return {"state": "committed"}

    def get_collection_upload_session_unit(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"state": "committed" if self.committed else "pending"}

    def complete_collection_upload_session(
        self,
        _collection_id: int,
        *,
        content_etag: str,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        self.completion_content_etag = content_etag
        self.discovery_closed = True
        return {
            "state": "uploading",
            "content_etag": content_etag,
        }

    def get_collection_upload_session(self, _collection_id: int) -> dict[str, Any]:
        assert self.committed
        return {
            "state": "finalized",
            "content_etag": self.completion_content_etag,
            "collection": {
                "id": 7,
                "manifest_sha256": "7" * 64,
                "content_etag": self.completion_content_etag,
            },
        }

    def put_collection_upload_session_provenance_journal(
        self,
        *_args: Any,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        raise AssertionError("fixture does not publish provenance journals")

    def spawn(self) -> UploadApi:
        return self


def test_producer_stream_has_no_shared_filesystem_and_is_snapshot_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RIVERHOG_UPLOAD_FILE_CONCURRENCY", "1")
    content = b"generated output"
    api = UploadApi()
    stream = ProducerStream(
        path="video/output.mkv",
        bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        read_range=lambda offset, size: content[offset : offset + size],
    )

    receipt = CollectionProducer(
        api,  # type: ignore[arg-type]
        producer_app="stove0-worker",
        adapter_id="test-transform/v1",
        adapter_version="1",
        ingest_source="transform:test",
        tags=("archive/camera",),
    ).publish_inputs((stream,), source_event_id="event-1")

    assert receipt.collection_id == 7
    assert api.volume_list_calls == 2
    assert api.completion_content_etag == receipt.content_etag
    uploaded_by_path = {
        str(item["path"]): api.uploaded[
            sum(int(previous["bytes"]) for previous in api.registered[:index]) : sum(
                int(previous["bytes"]) for previous in api.registered[: index + 1]
            )
        ]
        for index, item in enumerate(api.registered)
    }
    assert uploaded_by_path["video/output.mkv"] == content


def test_producer_stream_rejects_mutation_between_hash_and_upload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RIVERHOG_UPLOAD_FILE_CONCURRENCY", "1")
    content = b"generated output"
    calls = 0

    def mutable(offset: int, size: int) -> bytes:
        nonlocal calls
        calls += 1
        value = content if calls == 1 else b"X" * len(content)
        return value[offset : offset + size]

    stream = ProducerStream(
        path="video/output.mkv",
        bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        read_range=mutable,
    )

    with pytest.raises(RuntimeError, match="changed during upload"):
        CollectionProducer(
            UploadApi(),  # type: ignore[arg-type]
            producer_app="stove0-worker",
            adapter_id="test-transform/v1",
            adapter_version="1",
            ingest_source="transform:test",
            tags=("archive/camera",),
        ).publish_inputs((stream,), source_event_id="event-1")


def test_producer_file_rejects_symlink_sources(tmp_path: Path) -> None:
    source = tmp_path / "output.mkv"
    source.write_bytes(b"generated output")
    link = tmp_path / "linked.mkv"
    link.symlink_to(source)

    with pytest.raises(ValueError, match="symlink"):
        ProducerFile(source=link, path="video/output.mkv")


def test_producer_file_rejects_mutation_between_hash_and_upload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("RIVERHOG_UPLOAD_FILE_CONCURRENCY", "1")
    source = tmp_path / "output.mkv"
    source.write_bytes(b"generated output")

    class MutatingUploadApi(UploadApi):
        def list_collection_upload_session_volumes(
            self,
            collection_id: int,
        ) -> dict[str, Any]:
            source.write_bytes(b"X" * len(b"generated output"))
            return super().list_collection_upload_session_volumes(collection_id)

    with pytest.raises(RuntimeError, match="source changed during upload verification"):
        CollectionProducer(
            MutatingUploadApi(),  # type: ignore[arg-type]
            producer_app="stove0-worker",
            adapter_id="test-transform/v1",
            adapter_version="1",
            ingest_source="transform:test",
            tags=("archive/camera",),
        ).publish(
            (ProducerFile(source=source, path="video/output.mkv"),),
            source_event_id="event-1",
        )


def test_derived_writer_binds_outputs_to_dispositions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec()
    output = b"derived"
    stream = ProducerStream(
        path="video/output.mkv",
        bytes=len(output),
        sha256=hashlib.sha256(output).hexdigest(),
        read_range=lambda offset, size: output[offset : offset + size],
    )
    captured: dict[str, Any] = {}

    class StubProducer:
        def __init__(self, _api: object, **kwargs: Any) -> None:
            captured["init"] = kwargs

        def publish_inputs(self, files: object, **kwargs: Any) -> ProducedCollection:
            captured["files"] = files
            captured["publish"] = kwargs
            return ProducedCollection(44, "e" * 64, "f" * 64, {"state": "finalized"})

    import riverhog_transform_sdk.writer as module

    monkeypatch.setattr(module, "CollectionProducer", StubProducer)
    writer = DerivedCollectionWriter(
        object(),
        spec=spec,
        claim_id="claim-1",
        fence=1,
        work_id=WORK_ID,
        execution_id=EXECUTION_ID,
        controller_evidence=CONTROLLER_EVIDENCE,
        producer_app="fixture-transform",
    )

    receipt = writer.publish(
        (stream,),
        execution_envelope_sha256="c" * 64,
        execution_sha256="d" * 64,
        dispositions=(_disposition(spec),),
        source_context={"claim_id": "spoofed", "target": "fixture"},
    )

    assert receipt.collection_id == 44
    assert receipt.derivation.execution_id == EXECUTION_ID
    assert captured["publish"]["source_context"] == {
        "claim_id": "claim-1",
        "fence": 1,
        "work_id": WORK_ID,
        "execution_id": EXECUTION_ID,
        "execution_envelope_sha256": "c" * 64,
        "execution_sha256": "d" * 64,
        "target": "fixture",
    }
    evidence = captured["publish"]["inline_evidence"]
    assert evidence[DERIVATION_EVIDENCE_PATH] == receipt.derivation.to_json_bytes()

    with pytest.raises(ValueError, match="referenced exactly"):
        writer.publish(
            (
                ProducerStream(
                    path="video/unreferenced.mkv",
                    bytes=len(output),
                    sha256=hashlib.sha256(output).hexdigest(),
                    read_range=lambda offset, size: output[offset : offset + size],
                ),
            ),
            execution_envelope_sha256="c" * 64,
            execution_sha256="d" * 64,
            dispositions=(_disposition(spec),),
        )


def test_runtime_requires_complete_input_dispositions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec()
    api = RetrievalApi()
    runtime = CollectionTransformRuntime(
        api,  # type: ignore[arg-type]
        spec=spec,
        claim_id="claim-1",
        fence=1,
        work_id=WORK_ID,
        execution_id=EXECUTION_ID,
        controller_evidence=CONTROLLER_EVIDENCE,
        producer_app="fixture-transform",
    )
    output = b"derived"
    stream = ProducerStream(
        path="video/output.mkv",
        bytes=len(output),
        sha256=hashlib.sha256(output).hexdigest(),
        read_range=lambda offset, size: output[offset : offset + size],
    )
    derivation = CollectionDerivation(
        execution_id=EXECUTION_ID,
        claim_id="claim-1",
        fence=1,
        recipe=spec.recipe,
        operation=spec.operation,
        inputs=spec.inputs,
        output_tags=spec.output_tags,
        execution_envelope_sha256="c" * 64,
        execution_sha256="d" * 64,
        controller_evidence=CONTROLLER_EVIDENCE,
        controller_evidence_sha256=CONTROLLER_EVIDENCE_SHA256,
        dispositions=(_disposition(spec),),
    )
    expected_receipt = DerivedCollectionReceipt(44, "e" * 64, "f" * 64, derivation)
    monkeypatch.setattr(runtime.writer, "publish", lambda *_args, **_kwargs: expected_receipt)

    with pytest.raises(ValueError, match="every claimed input"):
        runtime.publish(
            (stream,),
            execution_envelope_sha256="c" * 64,
            execution_sha256="d" * 64,
            dispositions=(),
        )

    assert (
        runtime.publish(
            (stream,),
            execution_envelope_sha256="c" * 64,
            execution_sha256="d" * 64,
            dispositions=(_disposition(spec),),
        )
        == expected_receipt
    )


def test_finalized_receipt_is_not_revoked_by_a_late_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec()
    checks = 0

    def cancellation_check() -> None:
        nonlocal checks
        checks += 1
        if checks > 2:
            raise RuntimeError("canceled after publication")

    runtime = CollectionTransformRuntime(
        RetrievalApi(),  # type: ignore[arg-type]
        spec=spec,
        claim_id="claim-1",
        fence=1,
        work_id=WORK_ID,
        execution_id=EXECUTION_ID,
        controller_evidence=CONTROLLER_EVIDENCE,
        producer_app="fixture-transform",
        cancellation_check=cancellation_check,
    )
    output = b"derived"
    stream = ProducerStream(
        path="video/output.mkv",
        bytes=len(output),
        sha256=hashlib.sha256(output).hexdigest(),
        read_range=lambda offset, size: output[offset : offset + size],
    )
    derivation = CollectionDerivation(
        execution_id=EXECUTION_ID,
        claim_id="claim-1",
        fence=1,
        recipe=spec.recipe,
        operation=spec.operation,
        inputs=spec.inputs,
        output_tags=spec.output_tags,
        execution_envelope_sha256="c" * 64,
        execution_sha256="d" * 64,
        controller_evidence=CONTROLLER_EVIDENCE,
        controller_evidence_sha256=CONTROLLER_EVIDENCE_SHA256,
        dispositions=(_disposition(spec),),
    )
    expected = DerivedCollectionReceipt(44, "e" * 64, "f" * 64, derivation)
    monkeypatch.setattr(runtime.writer, "publish", lambda *_args, **_kwargs: expected)

    assert (
        runtime.publish(
            (stream,),
            execution_envelope_sha256="c" * 64,
            execution_sha256="d" * 64,
            dispositions=(_disposition(spec),),
        )
        == expected
    )
    assert checks == 2


def test_workspace_requires_explicit_protected_storage(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir(mode=0o700)
    root.chmod(0o700)

    with TransformWorkspace.open(
        root,
        execution_id=EXECUTION_ID,
        assurance="ephemeral",
    ) as workspace:
        output = workspace.resolve("video/output.mkv")
        output.parent.mkdir(parents=True)
        output.write_bytes(b"derived")
        assert output.is_file()
        escaped = workspace.root / "escaped"
        escaped.symlink_to(tmp_path, target_is_directory=True)
        with pytest.raises(ValueError, match="symlinks"):
            workspace.resolve("escaped/outside.bin")
        workspace.release()

    assert not (root / EXECUTION_ID).exists()


def test_capability_client_refreshes_workers_without_closing_active_delegate() -> None:
    from riverhog_transform_sdk import CapabilityApiClient

    class Client:
        def __init__(self, value: str) -> None:
            self.value = value
            self.closed = False

        def identity(self) -> str:
            return self.value

        def close(self) -> None:
            self.closed = True

    first = Client("first")
    second = Client("second")
    root = CapabilityApiClient(first, owns_client=True)
    worker = root.spawn()

    assert worker.identity() == "first"
    root.replace(second, owns_client=True)

    assert worker.identity() == "second"
    assert not first.closed
    root.close()
    assert first.closed
    assert second.closed


def test_runtime_registry_applies_refresh_arriving_before_target_start() -> None:
    from riverhog_transform_sdk import TransformRuntimeRegistry

    class Runtime:
        def __init__(self) -> None:
            self.tokens: list[str] = []
            self.closed = False

        def refresh_capability(self, token: str) -> None:
            self.tokens.append(token)

        def close(self) -> None:
            self.closed = True

    registry = TransformRuntimeRegistry()
    runtime = Runtime()
    registry.refresh("job-1", "replacement")

    with registry.bind("job-1", runtime):  # type: ignore[arg-type]
        assert runtime.tokens == ["replacement"]
        registry.refresh("job-1", "newer")
        assert runtime.tokens == ["replacement", "newer"]

    registry.discard("job-1")
    assert not runtime.closed


def test_runtime_rejects_empty_capability_without_environment_fallback() -> None:
    spec = _spec()
    with pytest.raises(ValueError, match="nonempty"):
        CollectionTransformRuntime.from_capability(
            base_url="https://riverhog.invalid",
            capability_token="  ",
            spec=spec,
            claim_id="claim-1",
            fence=1,
            work_id=WORK_ID,
            execution_id=EXECUTION_ID,
            controller_evidence=CONTROLLER_EVIDENCE,
            producer_app="fixture-transform",
        )


def test_runtime_registry_cleans_up_failed_pending_refresh() -> None:
    from riverhog_transform_sdk import TransformRuntimeRegistry

    class FailingRuntime:
        def refresh_capability(self, _token: str) -> None:
            raise RuntimeError("refresh rejected")

        def close(self) -> None:
            pass

    class WorkingRuntime:
        def refresh_capability(self, _token: str) -> None:
            pass

        def close(self) -> None:
            pass

    registry = TransformRuntimeRegistry()
    registry.refresh("job-1", "replacement")
    with pytest.raises(RuntimeError, match="refresh rejected"):
        with registry.bind("job-1", FailingRuntime()):  # type: ignore[arg-type]
            raise AssertionError("unreachable")

    with registry.bind("job-1", WorkingRuntime()):  # type: ignore[arg-type]
        pass


def test_api_client_streams_verified_full_and_range_content() -> None:
    content = b"0123456789"
    digest = hashlib.sha256(content).hexdigest()

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.headers["Accept-Encoding"] == "identity"
        byte_range = request.headers.get("Range")
        if byte_range is None:
            return httpx.Response(
                200,
                headers={
                    "Content-Length": str(len(content)),
                    "ETag": f'"{digest}"',
                },
                content=content,
            )
        assert byte_range == "bytes=2-6"
        selected = content[2:7]
        return httpx.Response(
            206,
            headers={
                "Content-Length": str(len(selected)),
                "Content-Range": f"bytes 2-6/{len(content)}",
                "ETag": f'"{digest}"',
            },
            content=selected,
        )

    api = ApiClient(base_url="https://riverhog.invalid", token="scoped")
    api._download_client = httpx.Client(  # type: ignore[attr-defined]
        base_url="https://riverhog.invalid",
        transport=httpx.MockTransport(handle),
    )
    try:
        with api.stream_retrieval_file(
            "job-1",
            collection_id=1,
            path="camera/input.mov",
            expected_bytes=len(content),
            expected_sha256=digest,
            chunk_size=3,
        ) as chunks:
            assert b"".join(chunks) == content
        with api.stream_retrieval_file(
            "job-1",
            collection_id=1,
            path="camera/input.mov",
            expected_bytes=len(content),
            expected_sha256=digest,
            start=2,
            end=7,
            chunk_size=2,
        ) as chunks:
            assert b"".join(chunks) == content[2:7]
    finally:
        api.close()


def test_api_client_stream_requires_complete_consumption() -> None:
    content = b"0123456789"
    digest = hashlib.sha256(content).hexdigest()
    api = ApiClient(base_url="https://riverhog.invalid", token="scoped")
    api._download_client = httpx.Client(  # type: ignore[attr-defined]
        base_url="https://riverhog.invalid",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                headers={
                    "Content-Length": str(len(content)),
                    "ETag": f'"{digest}"',
                },
                content=content,
            )
        ),
    )
    try:
        with pytest.raises(InvalidState, match="ended before"):
            with api.stream_retrieval_file(
                "job-1",
                collection_id=1,
                path="camera/input.mov",
                expected_bytes=len(content),
                expected_sha256=digest,
                chunk_size=2,
            ) as chunks:
                next(chunks)
    finally:
        api.close()
