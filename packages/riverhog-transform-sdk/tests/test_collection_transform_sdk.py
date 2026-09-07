from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from riverhog_api_client import ApiClient
from riverhog_api_client.producer import (
    CollectionProducer,
    ProducedCollection,
    ProducerArtifactIdentity,
    ProducerFile,
    ProducerStream,
)
from riverhog_protocol import (
    CollectionUploadUnitAssignmentDocument,
    CollectionUploadUnitWorkDocument,
    CollectionUploadWorkBatchDocument,
    ImmutableFileIdentityDocument,
    PortableCollectionHeader,
    PortableCollectionInventoryAuthority,
    PortableCollectionInventoryPage,
)
from riverhog_protocol.collection_upload_transport import collection_upload_path_order_key
from riverhog_protocol.collection_workflow_transport import (
    ArtifactDispositionOutputPageDocument,
    ArtifactDispositionPageDocument,
)
from riverhog_protocol.collection_workflows import (
    DERIVATION_EVIDENCE_PATH,
    PRODUCER_EVIDENCE_PATH,
    ArtifactDispositionSetIdentity,
    CollectionDerivation,
    CollectionRootIdentity,
    OperationIdentity,
    RecipeIdentity,
)
from riverhog_protocol.errors import InvalidState, NotFound
from riverhog_protocol.manifest import collection_content_identity_ordered
from riverhog_provenance import (
    create_derivative_journal_from_identity,
    create_observation_journal,
    validate_journal,
)
from riverhog_transform_sdk import (
    ClaimedCollectionReader,
    CollectionTransformRuntime,
    DerivedCollectionReceipt,
    DerivedCollectionSpec,
    DerivedCollectionWriter,
    TransformWorkspace,
)

from tests.provenance_observer import native_provenance_observer

WORK_ID = "3" * 64
EXECUTION_ID = "4" * 64
CONTROLLER_EVIDENCE = {
    "format": "stove0-controller-evidence/v1",
    "execution_id": EXECUTION_ID,
}
CONTROLLER_EVIDENCE_SHA256 = hashlib.sha256(
    json.dumps(CONTROLLER_EVIDENCE, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()


def _portable_page(files: Sequence[Mapping[str, Any]]) -> PortableCollectionInventoryPage:
    ordered = [
        ImmutableFileIdentityDocument(
            path=str(file["path"]),
            bytes=int(file["bytes"]),
            sha256=str(file["sha256"]),
        )
        for file in sorted(files, key=lambda item: str(item["path"]).encode("utf-8"))
    ]
    return PortableCollectionInventoryPage(
        authority=PortableCollectionInventoryAuthority(
            header=PortableCollectionHeader(
                collection=1,
                content_identity="2" * 64,
                encryption_format="age-v1-scrypt",
                passphrase_id="fixture-archive-key-v1",
                provenance_mode="omitted",
            ),
            inventory_identity="9" * 64,
            file_count=len(ordered),
            file_bytes=sum(file.bytes for file in ordered),
        ),
        files=ordered,
        complete=True,
    )


def _spec() -> DerivedCollectionSpec:
    return DerivedCollectionSpec(
        recipe=RecipeIdentity("camera/v1", 1, "a" * 64),
        operation=OperationIdentity("archive-video/v1", "b" * 64),
        inputs=(CollectionRootIdentity(1, "1" * 64, "2" * 64),),
    )


def _disposition_set(
    *,
    disposition_count: int = 1,
    output_edge_count: int = 1,
    output_artifact_count: int = 1,
) -> ArtifactDispositionSetIdentity:
    return ArtifactDispositionSetIdentity(
        disposition_count=disposition_count,
        output_edge_count=output_edge_count,
        output_artifact_count=output_artifact_count,
        sha256="6" * 64,
    )


def _processing_claim() -> SimpleNamespace:
    return SimpleNamespace(
        plan=SimpleNamespace(
            execution_id=EXECUTION_ID,
            inputs=SimpleNamespace(sha256="7" * 64),
            artifacts=SimpleNamespace(sha256="8" * 64),
        )
    )


def _derivation(spec: DerivedCollectionSpec) -> CollectionDerivation:
    return CollectionDerivation(
        execution_id=EXECUTION_ID,
        claim_id="claim-1",
        fence=1,
        recipe=spec.recipe,
        operation=spec.operation,
        input_set_sha256="7" * 64,
        artifact_set_sha256="8" * 64,
        execution_envelope_sha256="c" * 64,
        execution_sha256="d" * 64,
        controller_evidence=CONTROLLER_EVIDENCE,
        controller_evidence_sha256=CONTROLLER_EVIDENCE_SHA256,
        disposition_set=_disposition_set(),
    )


class RetrievalApi:
    def __init__(self, *, changed_root: bool = False) -> None:
        self.data = b"immutable input"
        self.sha256 = hashlib.sha256(self.data).hexdigest()
        self.changed_root = changed_root
        self.acknowledged: list[str] = []
        self.canceled: list[str] = []
        self.restore_policies: list[str] = []

    def get_processing_claim(self, claim_id: str) -> SimpleNamespace:
        assert claim_id == "claim-1"
        return _processing_claim()

    def get_collection(self, collection_id: int) -> dict[str, Any]:
        return {
            "id": collection_id,
            "archive_root_sha256": "3" * 64 if self.changed_root else "1" * 64,
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

    def get_portable_collection_inventory(
        self, collection_id: int, **kwargs: Any
    ) -> PortableCollectionInventoryPage:
        assert collection_id == 1
        assert kwargs["cursor"] is None
        return _portable_page(self.search()["files"])

    def list_collection_provenance(
        self,
        collection_id: int,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        assert collection_id == 1
        return {
            "page_size": 25,
            "next_page_token": None,
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
            ],
        }

    @contextmanager
    def stream_collection_provenance(self, collection_id: int, **kwargs: Any) -> Iterator[Any]:
        yield iter(self.list_collection_provenance(collection_id, **kwargs)["files"])

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
        self.planned_files = self._rows(files)
        return {
            "id": "plan-1",
            "etag": "9" * 64,
            "file_count": len(self.planned_files),
        }

    def list_retrieval_plan_files(
        self,
        plan_id: str,
        *,
        plan_etag: str,
        start_ordinal: int = 0,
        page_size: int = 100,
    ) -> dict[str, Any]:
        assert plan_id == "plan-1"
        assert plan_etag == "9" * 64
        assert start_ordinal == 0
        assert page_size == 100
        return {
            "plan_id": plan_id,
            "etag": plan_etag,
            "start_ordinal": start_ordinal,
            "files": self.planned_files,
            "complete": True,
            "next_ordinal": None,
        }

    def create_retrieval_job(
        self,
        plan_id: str,
        *,
        plan_etag: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        assert plan_id == "plan-1"
        return {
            "id": "retrieval-1",
            "plan_id": plan_id,
            "state": "ready",
            "plan_etag": plan_etag,
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
    assert api.restore_policies == ["available-only"]


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
        self.work_calls = 0
        self.session_calls = 0
        self.derivation_identity = _disposition_set()

    def get_processing_claim(self, claim_id: str) -> SimpleNamespace:
        assert claim_id == "claim-1"
        return _processing_claim()

    def list_processing_claim_dispositions(
        self,
        claim_id: str,
        *,
        authority_sha256: str,
        start_ordinal: int = 0,
    ) -> ArtifactDispositionPageDocument:
        assert claim_id == "claim-1"
        identity = self.derivation_identity
        assert authority_sha256 == identity.sha256 and start_ordinal == 0
        return ArtifactDispositionPageDocument.model_validate(
            {
                "authority": identity.as_dict(),
                "start_ordinal": 0,
                "dispositions": [
                    {
                        "input": {
                            "collection_id": 1,
                            "archive_root_sha256": "1" * 64,
                            "path": f"camera/input-{index:04d}.mov",
                        },
                        "status": "transformed",
                    }
                    for index in range(identity.disposition_count)
                ],
            }
        )

    def list_processing_claim_disposition_outputs(
        self,
        claim_id: str,
        *,
        authority_sha256: str,
        start_ordinal: int = 0,
    ) -> ArtifactDispositionOutputPageDocument:
        assert claim_id == "claim-1"
        identity = self.derivation_identity
        assert authority_sha256 == identity.sha256 and start_ordinal == 0
        return ArtifactDispositionOutputPageDocument.model_validate(
            {
                "authority": identity.as_dict(),
                "start_ordinal": 0,
                "outputs": [
                    {
                        "input": {
                            "collection_id": 1,
                            "archive_root_sha256": "1" * 64,
                            "path": f"camera/input-{index:04d}.mov",
                        },
                        "output_path": f"derived/output-{index:04d}.bin",
                    }
                    for index in range(identity.output_edge_count)
                ],
            }
        )

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
            "registration_constraints": {
                "pack_member_bytes": 1024,
                "raw_part_plaintext_bytes": 65536,
            },
        }

    def register_collection_upload_session_files(
        self,
        _collection_id: int,
        files: Sequence[Mapping[str, Any]],
        *,
        registration_constraints: object,
    ) -> dict[str, Any]:
        assert registration_constraints.raw_part_plaintext_bytes == 65536
        batch = [dict(item) for item in files]
        self.registration_batches.append(batch)
        existing = {str(item["path"]): item for item in self.registered}
        for item in batch:
            prior = existing.get(str(item["path"]))
            if prior is not None:
                assert prior == item
                continue
            self.registered.append(item)
        by_path = {str(item["path"]): item for item in self.registered}
        return {
            "state": "open",
            "files": [dict(by_path[str(item["path"])]) for item in batch],
            "volumes": [],
        }

    def list_collection_upload_session_files(
        self,
        _collection_id: int,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        return {
            "page_size": 100,
            "next_page_token": None,
            "files": [dict(item) for item in self.registered],
        }

    def heartbeat_collection_upload_session(self, _collection_id: int) -> dict[str, Any]:
        return {"state": "open"}

    def acquire_collection_upload_session_work(
        self,
        collection_id: int,
        *,
        limit: int = 16,
    ) -> CollectionUploadWorkBatchDocument:
        self.work_calls += 1
        assignment = self._assignment()
        work = [] if assignment is None else [assignment]
        return CollectionUploadWorkBatchDocument(
            collection_id=collection_id,
            planning_complete=self.discovery_closed,
            complete=self.discovery_closed and not work,
            committed_payload_bytes=len(self.uploaded),
            work=work[:limit],
        )

    def _assignment(self) -> CollectionUploadUnitAssignmentDocument | None:
        if not self.discovery_closed or self.committed:
            return None
        sources = [
            {
                "path": item["path"],
                "offset": 0,
                "bytes": item["bytes"],
                "artifact_sha256": item["sha256"],
            }
            for item in self.registered
        ]
        total_bytes = sum(int(item["bytes"]) for item in sources)
        return CollectionUploadUnitAssignmentDocument.model_validate(
            {
                "volume": {
                    "volume_id": "pack-" + "0" * 64,
                    "sequence": 0,
                    "kind": "pack",
                },
                "plan_sha256": "8" * 64,
                "unit": {
                    "unit": 0,
                    "payload_bytes": total_bytes,
                    "plaintext_bytes": total_bytes,
                    "sources": sources,
                    "state": "pending",
                },
            }
        )

    def put_collection_upload_session_unit(
        self,
        _collection_id: int,
        _volume_id: str,
        _unit: int,
        *,
        content: bytes,
        **_kwargs: Any,
    ) -> CollectionUploadUnitWorkDocument:
        self.uploaded = content
        self.committed = True
        return CollectionUploadUnitWorkDocument.model_validate(
            {
                **self._unit_payload(),
                "state": "committed",
            }
        )

    def get_collection_upload_session_unit(
        self,
        collection_id: int,
        _volume_id: str,
        _unit: int,
    ) -> CollectionUploadUnitWorkDocument:
        del collection_id
        return CollectionUploadUnitWorkDocument.model_validate(
            {
                **self._unit_payload(),
                "state": "committed" if self.committed else "pending",
            }
        )

    def _unit_payload(self) -> dict[str, object]:
        sources = [
            {
                "path": item["path"],
                "offset": 0,
                "bytes": item["bytes"],
                "artifact_sha256": item["sha256"],
            }
            for item in self.registered
        ]
        total_bytes = sum(int(item["bytes"]) for item in sources)
        return {
            "unit": 0,
            "payload_bytes": total_bytes,
            "plaintext_bytes": total_bytes,
            "sources": sources,
        }

    def complete_collection_upload_session(
        self,
        _collection_id: int,
    ) -> dict[str, Any]:
        ordered = sorted(
            self.registered,
            key=lambda item: collection_upload_path_order_key(str(item["path"])),
        )
        content_identity = collection_content_identity_ordered(
            (str(item["path"]), int(item["bytes"]), str(item["sha256"])) for item in ordered
        )
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
                "archive_root_sha256": "7" * 64,
                "content_identity": self.completion_content_identity,
            },
        }

    def upload_collection_upload_session_provenance_journal(
        self,
        *_args: Any,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        raise AssertionError("fixture does not publish provenance journals")

    def spawn(self) -> UploadApi:
        return self

    def close(self) -> None:
        pass


class ProvenanceTransformApi(UploadApi):
    def __init__(self, source_journals: Mapping[str, bytes]) -> None:
        super().__init__()
        self.source_contents = {
            "camera/a.mov": b"source a",
            "camera/b.mov": b"source b",
        }
        self.source_journals = dict(source_journals)
        self.staged_journals: dict[str, bytes] = {}
        self.staged_status_calls = 0
        self.fail_registration_once = True

    def get_collection(self, collection_id: int) -> dict[str, Any]:
        assert collection_id == 1
        return {
            "id": 1,
            "archive_root_sha256": "1" * 64,
            "content_identity": "2" * 64,
        }

    def search(self, _query: str | None = None, **_kwargs: Any) -> dict[str, Any]:
        files = [
            {
                "collection_id": 1,
                "path": path,
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            for path, content in self.source_contents.items()
        ]
        return {
            "page_size": 25,
            "next_page_token": None,
            "files": files,
        }

    def get_portable_collection_inventory(
        self, collection_id: int, **kwargs: Any
    ) -> PortableCollectionInventoryPage:
        assert collection_id == 1
        assert kwargs["cursor"] is None
        return _portable_page(self.search()["files"])

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
        files = [
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
        return {
            "page_size": 25,
            "next_page_token": None,
            "files": files,
        }

    @contextmanager
    def stream_collection_provenance(self, collection_id: int, **kwargs: Any) -> Iterator[Any]:
        yield iter(self.list_collection_provenance(collection_id, **kwargs)["files"])

    @contextmanager
    def stream_collection_provenance_journal(
        self,
        collection_id: int,
        journal_id: str,
    ) -> Iterator[Iterator[bytes]]:
        assert collection_id == 1
        yield iter((self.source_journals[journal_id],))

    def get_collection_upload_session_provenance_journal(
        self,
        collection_id: int,
        journal_id: str,
    ) -> dict[str, Any]:
        assert collection_id == 7
        self.staged_status_calls += 1
        try:
            content = self.staged_journals[journal_id]
        except KeyError as exc:
            raise NotFound("staged provenance journal not found") from exc
        summary = validate_journal(content)
        return {
            "journal_id": journal_id,
            "state": "sealed",
            "current_state_id": summary.current_state_id,
            "current_path": summary.current_path,
            "current_bytes": summary.current_bytes,
            "current_sha256": summary.current_sha256,
        }

    def seal_collection_upload_session_provenance_journal(
        self,
        collection_id: int,
        journal_id: str,
    ) -> dict[str, Any]:
        return self.get_collection_upload_session_provenance_journal(
            collection_id,
            journal_id,
        )

    def upload_collection_upload_session_provenance_journal(
        self,
        collection_id: int,
        journal_id: str,
        *,
        content: Iterable[bytes],
        byte_count: int,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        assert collection_id == 7
        body = b"".join(content)
        assert len(body) == byte_count
        existing = self.staged_journals.get(journal_id)
        if existing is not None and existing != body:
            raise AssertionError("retry changed staged provenance bytes")
        self.staged_journals[journal_id] = body
        return {"journal_id": journal_id}

    def register_collection_upload_session_files(
        self,
        collection_id: int,
        files: Sequence[Mapping[str, Any]],
        *,
        registration_constraints: object,
    ) -> dict[str, Any]:
        if self.fail_registration_once:
            self.fail_registration_once = False
            raise RuntimeError("simulated lost producer progress after journal staging")
        return super().register_collection_upload_session_files(
            collection_id,
            files,
            registration_constraints=registration_constraints,
        )


def test_producer_rejects_empty_iterable_before_opening_construction() -> None:
    api = UploadApi()

    with pytest.raises(ValueError, match="at least one source file"):
        CollectionProducer(
            api,  # type: ignore[arg-type]
            producer_app="fixture-transform",
            adapter_id="test-transform/v1",
            adapter_version="1",
            ingest_source="transform:test",
        ).publish_inputs(iter(()), source_event_id="empty")

    assert api.session_calls == 0


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
    ).publish_inputs((stream,), source_event_id="event-1")

    assert receipt.collection_id == 7
    assert api.work_calls == 3
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


def test_producer_streams_bounded_batches_without_limiting_collection_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RIVERHOG_UPLOAD_FILE_CONCURRENCY", "1")
    api = UploadApi()

    def streams() -> Iterator[ProducerStream]:
        for index in range(128):
            if index == 16:
                assert api.registered
            value = bytes([index % 251])
            yield ProducerStream(
                path=f"audio/item-{index:04}.wav",
                bytes=1,
                sha256=hashlib.sha256(value).hexdigest(),
                read_range=lambda offset, size, content=value: content[offset : offset + size],
            )

    receipt = CollectionProducer(
        api,  # type: ignore[arg-type]
        producer_app="stove0-worker",
        adapter_id="test-transform/v1",
        adapter_version="1",
        ingest_source="transform:test",
    ).publish_inputs(streams(), source_event_id="event-many")

    assert receipt.collection_id == 7
    assert all(1 <= len(batch) <= 16 for batch in api.registration_batches)
    assert len(api.registered) == 129


def test_producer_streams_exact_provenance_binding_and_journal(
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
        observer=native_provenance_observer(),
    )
    summary = validate_journal(journal)

    class ProvenanceUploadApi(UploadApi):
        def __init__(self) -> None:
            super().__init__()
            self.journals: dict[str, bytes] = {}

        def upload_collection_upload_session_provenance_journal(
            self,
            _collection_id: int,
            journal_id: str,
            *,
            content: Iterable[bytes],
            byte_count: int,
            **_kwargs: Any,
        ) -> dict[str, Any]:
            body = b"".join(content)
            assert len(body) == byte_count
            self.journals[journal_id] = body
            return {"journal_id": journal_id}

    api = ProvenanceUploadApi()
    calls = 0

    def read_range(offset: int, size: int) -> bytes:
        nonlocal calls
        calls += 1
        return content[offset : offset + size]

    CollectionProducer(
        api,  # type: ignore[arg-type]
        producer_app="fixture-transform",
        adapter_id="test-transform/v1",
        adapter_version="1",
        ingest_source="transform:test",
        provenance_mode="captured",
    ).publish_inputs(
        (
            ProducerStream(
                path="video/output.mkv",
                bytes=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
                read_range=read_range,
                provenance={
                    "status": "captured",
                    "journal_id": summary.journal_id,
                    "current_state_id": summary.current_state_id,
                },
            ),
        ),
        source_event_id="event-1",
        provenance_journals=((summary.journal_id, journal),),
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
            observer=native_provenance_observer(),
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
        observer=native_provenance_observer(),
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
    disposition_set = _disposition_set(
        disposition_count=2,
        output_edge_count=4,
        output_artifact_count=3,
    )
    api.derivation_identity = disposition_set

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
            disposition_set=disposition_set,
            poll_seconds=0.01,
        )
    receipt = runtime().publish(
        outputs,
        execution_envelope_sha256="c" * 64,
        execution_sha256="d" * 64,
        disposition_set=disposition_set,
        poll_seconds=0.01,
    )

    assert receipt.collection_id == 7
    registered = {item["path"]: item for item in api.registered}
    assert set(output_contents) <= set(registered)


def test_incremental_transform_recovers_journal_staged_before_registration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("RIVERHOG_UPLOAD_FILE_CONCURRENCY", "1")
    source = tmp_path / "source-a.mov"
    source.write_bytes(b"source a")
    source_journal = create_observation_journal(
        source,
        relative_path="camera/a.mov",
        host_id="urn:uuid:00000000-0000-4000-8000-000000000567",
        agent_name="riverhog-client",
        agent_version="1.0.0",
        observer=native_provenance_observer(),
    )
    api = ProvenanceTransformApi({validate_journal(source_journal).journal_id: source_journal})
    api.source_contents = {"camera/a.mov": b"source a"}
    content = b"incremental derivative"
    identity = ProducerArtifactIdentity(
        path="derived/a.bin",
        bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )
    stream = ProducerStream(
        path=identity.path,
        bytes=identity.bytes,
        sha256=identity.sha256,
        read_range=lambda offset, size: content[offset : offset + size],
    )

    def runtime() -> CollectionTransformRuntime:
        return CollectionTransformRuntime(
            api,  # type: ignore[arg-type]
            spec=_spec(),
            claim_id="claim-1",
            fence=1,
            work_id=WORK_ID,
            execution_id=EXECUTION_ID,
            controller_evidence=CONTROLLER_EVIDENCE,
            producer_app="fixture-transform",
            producer_version="1.0.0",
        )

    first = runtime()
    try:
        writer = first.open_incremental_publication(execution_envelope_sha256="c" * 64)
        with pytest.raises(RuntimeError, match="simulated lost producer progress"):
            writer.append(stream, identity=identity)
    finally:
        first.close()
    resumed = runtime()
    try:
        writer = resumed.open_incremental_publication(execution_envelope_sha256="c" * 64)
        writer.append(stream, identity=identity)
    finally:
        resumed.close()

    assert any(item["path"] == identity.path for item in api.registered)


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
        def acquire_collection_upload_session_work(
            self,
            collection_id: int,
            *,
            limit: int = 16,
        ) -> CollectionUploadWorkBatchDocument:
            source.write_bytes(b"X" * len(b"generated output"))
            return super().acquire_collection_upload_session_work(collection_id, limit=limit)

    with pytest.raises(RuntimeError, match="source changed during upload verification"):
        CollectionProducer(
            MutatingUploadApi(),  # type: ignore[arg-type]
            producer_app="stove0-worker",
            adapter_id="test-transform/v1",
            adapter_version="1",
            ingest_source="transform:test",
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

        def append_inputs(self, files: object) -> None:
            captured["files"] = files

        def append_derivation_evidence(self, path: str, content: bytes) -> None:
            captured.setdefault("derivation_pages", {})[path] = content

        def finish(self, **kwargs: Any) -> ProducedCollection:
            captured["finish"] = kwargs
            return ProducedCollection(44, "e" * 64, "f" * 64, {"state": "finalized"})

    import riverhog_transform_sdk.writer as module

    monkeypatch.setattr(module, "IncrementalCollectionProducer", StubProducer)
    writer = DerivedCollectionWriter(
        UploadApi(),
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
        disposition_set=_disposition_set(),
        source_context={"claim_id": "spoofed", "target": "fixture"},
    )

    assert receipt.collection_id == 44
    assert receipt.derivation.execution_id == EXECUTION_ID
    assert captured["init"]["source_context"] == {
        "claim_id": "claim-1",
        "fence": 1,
        "work_id": WORK_ID,
        "execution_id": EXECUTION_ID,
        "execution_envelope_sha256": "c" * 64,
        "execution_sha256": "d" * 64,
        "target": "fixture",
    }
    assert len(captured["derivation_pages"]) == 2
    evidence = captured["finish"]["terminal_evidence"]
    assert evidence[DERIVATION_EVIDENCE_PATH] == receipt.derivation.to_json_bytes()

    with pytest.raises(ValueError, match="differs from derived outputs"):
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
            disposition_set=_disposition_set(
                output_edge_count=2,
                output_artifact_count=2,
            ),
        )


def test_runtime_passes_the_sealed_disposition_authority_to_publication(
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
    derivation = _derivation(spec)
    expected_receipt = DerivedCollectionReceipt(44, "e" * 64, "f" * 64, derivation)
    captured: dict[str, Any] = {}

    def publish(*_args: object, **kwargs: Any) -> DerivedCollectionReceipt:
        captured.update(kwargs)
        return expected_receipt

    monkeypatch.setattr(runtime.writer, "publish", publish)
    authority = _disposition_set()

    assert (
        runtime.publish(
            (stream,),
            execution_envelope_sha256="c" * 64,
            execution_sha256="d" * 64,
            disposition_set=authority,
        )
        == expected_receipt
    )
    assert captured["disposition_set"] == authority


def test_finalized_receipt_is_not_revoked_by_a_late_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = _spec()
    checks = 0

    def cancellation_check() -> None:
        nonlocal checks
        checks += 1
        if checks > 1:
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
    derivation = _derivation(spec)
    expected = DerivedCollectionReceipt(44, "e" * 64, "f" * 64, derivation)
    monkeypatch.setattr(runtime.writer, "publish", lambda *_args, **_kwargs: expected)

    assert (
        runtime.publish(
            (stream,),
            execution_envelope_sha256="c" * 64,
            execution_sha256="d" * 64,
            disposition_set=_disposition_set(),
        )
        == expected
    )
    assert checks == 1


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
    from riverhog_transform_sdk import ClaimedCollectionRuntimeRegistry

    class Runtime:
        def __init__(self) -> None:
            self.tokens: list[str] = []
            self.closed = False

        def refresh_capability(self, token: str) -> None:
            self.tokens.append(token)

        def close(self) -> None:
            self.closed = True

    registry = ClaimedCollectionRuntimeRegistry()
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
    from riverhog_transform_sdk import ClaimedCollectionRuntimeRegistry

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

    registry = ClaimedCollectionRuntimeRegistry()
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
