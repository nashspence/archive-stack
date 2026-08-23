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
    ProducerArtifactIdentity,
    ProducerFile,
    ProducerProvenance,
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
from riverhog_protocol.errors import InvalidState, NotFound
from riverhog_provenance import (
    create_derivative_journal_from_identity,
    create_observation_journal,
    validate_journal,
    validate_journal_set,
)
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
            "content_identity": "2" * 64,
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

    def list_collection_provenance(
        self,
        collection_id: int,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        assert collection_id == 1
        return {
            "files": [
                {
                    "collection_id": 1,
                    "path": "camera/input.mov",
                    "bytes": len(self.data),
                    "sha256": self.sha256,
                    "provenance": {
                        "status": "omitted",
                        "omission_reason": "fixture omitted provenance explicitly",
                    },
                }
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
        self.registration_batches: list[list[dict[str, Any]]] = []
        self.uploaded = b""
        self.completion_content_identity = ""
        self.committed = False
        self.discovery_closed = False
        self.volume_list_calls = 0
        self.session_calls = 0

    def create_or_resume_collection_upload_session(
        self,
        *_args: Any,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        self.session_calls += 1
        return {
            "collection_id": 7,
            "resumed": self.session_calls > 1,
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
        batch = [dict(item) for item in files]
        self.registration_batches.append(batch)
        existing = {str(item["path"]): item for item in self.registered}
        for item in batch:
            prior = existing.get(str(item["path"]))
            if prior is not None:
                assert prior == item
                continue
            self.registered.append(item)
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
        content_identity: str,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        self.completion_content_identity = content_identity
        self.discovery_closed = True
        return {
            "state": "uploading",
            "content_identity": content_identity,
        }

    def get_collection_upload_session(self, _collection_id: int) -> dict[str, Any]:
        assert self.committed
        return {
            "state": "finalized",
            "content_identity": self.completion_content_identity,
            "collection": {
                "id": 7,
                "manifest_sha256": "7" * 64,
                "content_identity": self.completion_content_identity,
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


class ProvenanceTransformApi(UploadApi):
    def __init__(self, source_journals: Mapping[str, bytes]) -> None:
        super().__init__()
        self.source_contents = {
            "camera/a.mov": b"source a",
            "camera/b.mov": b"source b",
        }
        self.source_journals = dict(source_journals)
        self.staged_journals: dict[str, bytes] = {}
        self.staged_export_calls = 0
        self.fail_registration_once = True

    def get_collection(self, collection_id: int) -> dict[str, Any]:
        assert collection_id == 1
        return {
            "id": 1,
            "manifest_sha256": "1" * 64,
            "content_identity": "2" * 64,
        }

    def search(self, _query: str | None = None, **_kwargs: Any) -> dict[str, Any]:
        return {
            "files": [
                {
                    "collection_id": 1,
                    "path": path,
                    "bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
                for path, content in self.source_contents.items()
            ]
        }

    def list_collection_provenance(
        self,
        collection_id: int,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        assert collection_id == 1
        summaries = {
            validate_journal(content).current_path: validate_journal(content)
            for content in self.source_journals.values()
        }
        return {
            "files": [
                {
                    "collection_id": 1,
                    "path": path,
                    "bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "provenance": {
                        "status": "captured",
                        "journal_id": summaries[path].journal_id,
                        "current_state_id": summaries[path].current_state_id,
                    },
                }
                for path, content in self.source_contents.items()
            ]
        }

    def export_collection_provenance_journal(
        self,
        collection_id: int,
        journal_id: str,
    ) -> bytes:
        assert collection_id == 1
        return self.source_journals[journal_id]

    def export_collection_upload_session_provenance_journal(
        self,
        collection_id: int,
        journal_id: str,
    ) -> bytes:
        assert collection_id == 7
        self.staged_export_calls += 1
        try:
            return self.staged_journals[journal_id]
        except KeyError as exc:
            raise NotFound("staged provenance journal not found") from exc

    def put_collection_upload_session_provenance_journal(
        self,
        collection_id: int,
        journal_id: str,
        *,
        content: bytes,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        assert collection_id == 7
        existing = self.staged_journals.get(journal_id)
        if existing is not None and existing != content:
            raise AssertionError("retry changed staged provenance bytes")
        self.staged_journals[journal_id] = content
        return {"journal_id": journal_id}

    def register_collection_upload_session_files(
        self,
        collection_id: int,
        files: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        if self.fail_registration_once:
            self.fail_registration_once = False
            raise RuntimeError("simulated lost producer progress after journal staging")
        return super().register_collection_upload_session_files(collection_id, files)


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
    assert api.completion_content_identity == receipt.content_identity
    uploaded_by_path = {
        str(item["path"]): api.uploaded[
            sum(int(previous["bytes"]) for previous in api.registered[:index]) : sum(
                int(previous["bytes"]) for previous in api.registered[: index + 1]
            )
        ]
        for index, item in enumerate(api.registered)
    }
    assert uploaded_by_path["video/output.mkv"] == content


def test_producer_batches_large_exact_manifests_without_limiting_collection_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RIVERHOG_UPLOAD_FILE_CONCURRENCY", "1")
    api = UploadApi()
    streams = tuple(
        ProducerStream(
            path=f"audio/item-{index:04}.wav",
            bytes=1,
            sha256=hashlib.sha256(bytes([index % 251])).hexdigest(),
            read_range=lambda offset, size, value=bytes([index % 251]): value[
                offset : offset + size
            ],
        )
        for index in range(128)
    )

    receipt = CollectionProducer(
        api,  # type: ignore[arg-type]
        producer_app="stove0-worker",
        adapter_id="test-transform/v1",
        adapter_version="1",
        ingest_source="transform:test",
        tags=("archive/audio",),
    ).publish_inputs(streams, source_event_id="event-many")

    assert receipt.collection_id == 7
    assert [len(batch) for batch in api.registration_batches] == [16] * 8 + [1]
    assert len(api.registered) == 129


def test_producer_builds_provenance_after_exact_stream_verification(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("RIVERHOG_UPLOAD_FILE_CONCURRENCY", "1")
    content = b"generated output"
    observed = tmp_path / "output.mkv"
    observed.write_bytes(content)
    journal = create_observation_journal(
        observed,
        relative_path="video/output.mkv",
        host_id="urn:uuid:00000000-0000-4000-8000-000000000567",
        agent_name="fixture-target",
        agent_version="1.0.0",
    )
    summary = validate_journal(journal)

    class ProvenanceUploadApi(UploadApi):
        def __init__(self) -> None:
            super().__init__()
            self.journals: dict[str, bytes] = {}

        def put_collection_upload_session_provenance_journal(
            self,
            _collection_id: int,
            journal_id: str,
            *,
            content: bytes,
            **_kwargs: Any,
        ) -> dict[str, Any]:
            self.journals[journal_id] = content
            return {"journal_id": journal_id}

    api = ProvenanceUploadApi()
    calls = 0

    def read_range(offset: int, size: int) -> bytes:
        nonlocal calls
        calls += 1
        return content[offset : offset + size]

    def build(
        collection_id: int,
        resumed: bool,
        artifacts: tuple[ProducerArtifactIdentity, ...],
    ) -> ProducerProvenance:
        assert collection_id == 7
        assert resumed is False
        assert artifacts == (
            ProducerArtifactIdentity(
                path="video/output.mkv",
                bytes=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
            ),
        )
        return ProducerProvenance(
            bindings={
                "video/output.mkv": {
                    "status": "captured",
                    "journal_id": summary.journal_id,
                    "current_state_id": summary.current_state_id,
                }
            },
            journals={summary.journal_id: journal},
        )

    CollectionProducer(
        api,  # type: ignore[arg-type]
        producer_app="fixture-transform",
        adapter_id="test-transform/v1",
        adapter_version="1",
        ingest_source="transform:test",
        tags=("archive/camera",),
    ).publish_inputs(
        (
            ProducerStream(
                path="video/output.mkv",
                bytes=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
                read_range=read_range,
            ),
        ),
        source_event_id="event-1",
        provenance_builder=build,
    )

    registered = {item["path"]: item for item in api.registered}
    assert registered["video/output.mkv"]["provenance"] == {
        "status": "captured",
        "journal_id": summary.journal_id,
        "current_state_id": summary.current_state_id,
    }
    assert api.journals == {summary.journal_id: journal}
    assert calls == 2


def test_transform_provenance_fans_out_fans_in_and_recovers_staged_journals(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("RIVERHOG_UPLOAD_FILE_CONCURRENCY", "1")
    source_journals: dict[str, bytes] = {}
    for relative, content in (("camera/b.mov", b"source b"),):
        source = tmp_path / relative.replace("/", "-")
        source.write_bytes(content)
        journal = create_observation_journal(
            source,
            relative_path=relative,
            host_id="urn:uuid:00000000-0000-4000-8000-000000000567",
            agent_name="riverhog-client",
            agent_version="1.0.0",
        )
        source_journals[validate_journal(journal).journal_id] = journal
    original_a = tmp_path / "original-a.mov"
    original_a.write_bytes(b"original a")
    original_a_journal = create_observation_journal(
        original_a,
        relative_path="original/a.mov",
        host_id="urn:uuid:00000000-0000-4000-8000-000000000567",
        agent_name="riverhog-client",
        agent_version="1.0.0",
    )
    continued_a_journal = create_derivative_journal_from_identity(
        relative_path="camera/a.mov",
        byte_count=len(b"source a"),
        sha256=hashlib.sha256(b"source a").hexdigest(),
        source_journals=(original_a_journal,),
        agent_name="fixture-target",
        agent_version="1.0.0",
        event_label="fixture.prior-transform/v1",
        started_at="2026-08-09T01:00:00Z",
        ended_at="2026-08-09T01:01:00Z",
    )
    source_journals.update(
        {
            validate_journal(original_a_journal).journal_id: original_a_journal,
            validate_journal(continued_a_journal).journal_id: continued_a_journal,
        }
    )
    api = ProvenanceTransformApi(source_journals)
    spec = _spec()
    output_contents = {
        "derived/a-one.bin": b"a derivative one",
        "derived/a-two.bin": b"a derivative two",
        "derived/joined.bin": b"a and b joined",
    }
    outputs = tuple(
        ProducerStream(
            path=path,
            bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            read_range=lambda offset, size, value=content: value[offset : offset + size],
        )
        for path, content in output_contents.items()
    )
    dispositions = (
        ArtifactDisposition(
            input_collection_id=1,
            input_manifest_sha256=spec.inputs[0].manifest_sha256,
            input_path="camera/a.mov",
            status="transformed",
            outputs=(
                "derived/a-one.bin",
                "derived/a-two.bin",
                "derived/joined.bin",
            ),
        ),
        ArtifactDisposition(
            input_collection_id=1,
            input_manifest_sha256=spec.inputs[0].manifest_sha256,
            input_path="camera/b.mov",
            status="transformed",
            outputs=("derived/joined.bin",),
        ),
    )

    def runtime() -> CollectionTransformRuntime:
        return CollectionTransformRuntime(
            api,  # type: ignore[arg-type]
            spec=spec,
            claim_id="claim-1",
            fence=1,
            work_id=WORK_ID,
            execution_id=EXECUTION_ID,
            controller_evidence=CONTROLLER_EVIDENCE,
            producer_app="fixture-transform",
            producer_version="1.0.0",
        )

    with pytest.raises(RuntimeError, match="simulated lost producer progress"):
        runtime().publish(
            outputs,
            execution_envelope_sha256="c" * 64,
            execution_sha256="d" * 64,
            dispositions=dispositions,
            poll_seconds=0.01,
        )
    first_staged = dict(api.staged_journals)
    assert api.staged_export_calls == 0

    receipt = runtime().publish(
        outputs,
        execution_envelope_sha256="c" * 64,
        execution_sha256="d" * 64,
        dispositions=dispositions,
        poll_seconds=0.01,
    )

    assert receipt.collection_id == 7
    assert api.staged_export_calls == len(output_contents)
    assert api.staged_journals == first_staged
    summaries = validate_journal_set(api.staged_journals)
    assert len(summaries) == 6
    outputs_by_path = {
        summary.current_path: summary
        for summary in summaries.values()
        if summary.current_path in output_contents
    }
    assert set(outputs_by_path) == set(output_contents)
    assert len(outputs_by_path["derived/a-one.bin"].external_states) == 1
    assert len(outputs_by_path["derived/a-two.bin"].external_states) == 1
    assert len(outputs_by_path["derived/joined.bin"].external_states) == 2
    registered = {item["path"]: item for item in api.registered}
    assert all(registered[path]["provenance"]["status"] == "captured" for path in output_contents)


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
        if checks > 3:
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
    assert checks == 3


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
