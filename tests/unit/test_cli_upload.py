from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from pathlib import Path

import httpx
import pytest
from riverhog_api_client import put_collection_upload_unit
from riverhog_cli import main as riverhog_main
from riverhog_cli.upload_progress import CollectionUploadProgressState, format_upload_progress_line
from riverhog_protocol import (
    CollectionUploadUnitAssignmentDocument,
    CollectionUploadUnitWorkDocument,
    CollectionUploadWorkBatchDocument,
)
from riverhog_protocol.raw_ingress import ordered_raw_part_commitment
from typer.testing import CliRunner

from tests.provenance_observer import native_provenance_observer

RUNNER = CliRunner()
COLLECTION_ID = 1
REGISTRATION_CONSTRAINTS = {
    "pack_member_bytes": 8,
    "raw_part_plaintext_bytes": 5 * 1024 * 1024,
}


def test_upload_runtime_settings_have_explicit_parsers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RIVERHOG_UPLOAD_FILE_LOG_BYTES", "0")
    monkeypatch.setenv("RIVERHOG_UPLOAD_FINALIZE_POLL_SECONDS", "0.25")
    monkeypatch.setenv("RIVERHOG_UPLOAD_FINALIZE_TIMEOUT_SECONDS", "12.5")

    assert riverhog_main._upload_file_log_bytes() == 0
    assert riverhog_main._upload_finalize_poll_seconds() == 0.25
    assert riverhog_main._upload_finalize_timeout_seconds() == 12.5


def test_local_collection_summary_streams_file_hashes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "collection"
    root.mkdir()
    source = root / "clip.bin"
    content = b"0123456789abcdef"
    source.write_bytes(content)

    def forbidden_read_bytes(self: Path) -> bytes:
        raise AssertionError(f"read_bytes should not be used for upload manifests: {self}")

    monkeypatch.setattr(Path, "read_bytes", forbidden_read_bytes)

    assert riverhog_main._local_collection_summary(root) == (
        1,
        len(content),
        [
            {
                "path": "clip.bin",
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        ],
    )


def test_collection_upload_dry_run_hashes_without_opening_an_api_client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "collection"
    root.mkdir()
    (root / "clip.bin").write_bytes(b"video")

    def forbidden_client() -> object:
        raise AssertionError("dry-run must not create an API client")

    monkeypatch.setattr(riverhog_main, "client", forbidden_client)

    result = RUNNER.invoke(
        riverhog_main.app,
        [
            "collection",
            "upload",
            "start",
            str(root),
            "--idempotency-key",
            "test-upload",
            "--archive-store",
            "b2",
            "--dry-run",
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["collection_id"] is None
    assert payload["archive_store"] == "b2"
    assert payload["files_preview"][0]["sha256"] == hashlib.sha256(b"video").hexdigest()
    human = RUNNER.invoke(
        riverhog_main.app,
        [
            "collection",
            "upload",
            "start",
            str(root),
            "--idempotency-key",
            "test-upload",
            "--archive-store",
            "b2",
            "--dry-run",
        ],
    )
    assert human.exit_code == 0
    assert "collection upload dry-run" in human.stdout


def test_large_source_hash_includes_server_layout_part_digests(tmp_path: Path) -> None:
    root = tmp_path / "collection"
    root.mkdir()
    source = root / "large.bin"
    content = (b"a" * 65_536) + (b"b" * 65_536) + b"tail"
    source.write_bytes(content)

    entry = riverhog_main._hash_collection_source(
        root,
        source,
        pack_member_bytes=8,
        raw_part_plaintext_bytes=65_536,
        provenance_observer_factory=native_provenance_observer,
    )

    assert entry["path"] == "large.bin"
    assert entry["bytes"] == len(content)
    assert entry["sha256"] == hashlib.sha256(content).hexdigest()
    expected_parts = (
        hashlib.sha256(b"a" * 65_536).hexdigest(),
        hashlib.sha256(b"b" * 65_536).hexdigest(),
        hashlib.sha256(b"tail").hexdigest(),
    )
    count, commitment = ordered_raw_part_commitment(expected_parts)
    assert entry["raw_parts"] == {
        "part_plaintext_bytes": 65_536,
        "part_count": count,
        "ordered_sha256": commitment,
    }
    spool = entry.pop("raw_digest_spool")
    assert (
        tuple(value for _first, batch in spool.iter_batches() for value in batch) == expected_parts
    )
    spool.close()
    assert entry["provenance"]["status"] == "captured"  # type: ignore[index]
    assert len(entry["provenance_journals"]) == 1


def test_upload_unit_content_concatenates_planned_source_ranges(tmp_path: Path) -> None:
    root = tmp_path / "collection"
    root.mkdir()
    (root / "a.bin").write_bytes(b"alpha")
    (root / "b.bin").write_bytes(b"bravox")

    assert (
        riverhog_main._upload_unit_content(
            root,
            CollectionUploadUnitWorkDocument.model_validate(
                {
                    "unit": 0,
                    "payload_bytes": 7,
                    "plaintext_bytes": 7,
                    "sources": [
                        {
                            "path": "a.bin",
                            "offset": 1,
                            "bytes": 3,
                            "artifact_sha256": hashlib.sha256(b"alpha").hexdigest(),
                        },
                        {
                            "path": "b.bin",
                            "offset": 2,
                            "bytes": 4,
                            "artifact_sha256": hashlib.sha256(b"bravox").hexdigest(),
                        },
                    ],
                    "state": "pending",
                }
            ),
        )
        == b"lphavox"
    )


def test_upload_unit_recovers_when_commit_response_is_lost(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "collection"
    root.mkdir()
    (root / "clip.bin").write_bytes(b"content")
    committed = False
    unit = CollectionUploadUnitWorkDocument.model_validate(
        {
            "unit": 0,
            "payload_bytes": 7,
            "plaintext_bytes": 7,
            "sources": [
                {
                    "path": "clip.bin",
                    "offset": 0,
                    "bytes": 7,
                    "artifact_sha256": hashlib.sha256(b"content").hexdigest(),
                }
            ],
            "state": "pending",
        }
    )
    assignment = CollectionUploadUnitAssignmentDocument(
        volume={
            "volume_id": f"pack-{0:064x}",
            "sequence": 0,
            "kind": "pack",
        },
        plan_sha256="a" * 64,
        unit=unit,
    )

    class Api:
        def put_collection_upload_session_unit(
            self, *_args: object, **kwargs: object
        ) -> CollectionUploadUnitWorkDocument:
            nonlocal committed
            assert kwargs["content"] == b"content"
            committed = True
            raise httpx.ReadError("response lost")

        def get_collection_upload_session_unit(
            self, *_args: object
        ) -> CollectionUploadUnitWorkDocument:
            return unit.model_copy(update={"state": "committed" if committed else "pending"})

    accepted = put_collection_upload_unit(
        Api(),  # type: ignore[arg-type]
        COLLECTION_ID,
        assignment,
        content_for_unit=lambda current: riverhog_main._upload_unit_content(root, current),
        retry_initial_delay_seconds=0,
    )

    assert accepted == 7


def test_direct_collection_upload_registers_plans_and_finalizes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "collection"
    root.mkdir()
    (root / "a.txt").write_bytes(b"alpha")
    (root / "b.txt").write_bytes(b"bravo")
    registered: list[dict[str, object]] = []
    uploaded = bytearray()
    committed = False

    class Api:
        base_url = "https://riverhog.test"
        token = "token"
        host_header = None
        http2 = True

        def create_or_resume_collection_upload_session(
            self,
            idempotency_key: str,
            **_kwargs: object,
        ) -> dict[str, object]:
            assert idempotency_key == "test-upload"
            return {
                "collection_id": COLLECTION_ID,
                "state": "open",
                "registration_constraints": REGISTRATION_CONSTRAINTS,
            }

        def register_collection_upload_session_files(
            self,
            collection_id: int,
            files: list[dict[str, object]],
            *,
            registration_constraints: object,
        ) -> dict[str, object]:
            assert collection_id == COLLECTION_ID
            assert registration_constraints.model_dump() == REGISTRATION_CONSTRAINTS
            registered.extend(files)
            return {"files": files}

        def complete_collection_upload_session(
            self,
            collection_id: int,
        ) -> dict[str, object]:
            assert collection_id == COLLECTION_ID
            return {"collection_id": collection_id, "state": "uploading"}

        def upload_collection_upload_session_provenance_journal(
            self,
            collection_id: int,
            journal_id: str,
            *,
            content: Iterable[bytes],
            byte_count: int,
            sha256: str,
        ) -> dict[str, object]:
            body = b"".join(content)
            assert collection_id == COLLECTION_ID
            assert len(body) == byte_count
            assert hashlib.sha256(body).hexdigest() == sha256
            assert journal_id.startswith("urn:uuid:")
            return {"journal_id": journal_id, "sha256": sha256}

        def acquire_collection_upload_session_work(
            self,
            collection_id: int,
            *,
            limit: int = 16,
        ) -> CollectionUploadWorkBatchDocument:
            assert collection_id == COLLECTION_ID
            work = (
                []
                if committed
                else [
                    {
                        "volume": {
                            "volume_id": f"pack-{0:064x}",
                            "sequence": 0,
                            "kind": "pack",
                        },
                        "plan_sha256": "a" * 64,
                        "unit": {
                            "unit": 0,
                            "payload_bytes": 10,
                            "plaintext_bytes": 10,
                            "sources": [
                                {
                                    "path": "a.txt",
                                    "offset": 0,
                                    "bytes": 5,
                                    "artifact_sha256": hashlib.sha256(b"alpha").hexdigest(),
                                },
                                {
                                    "path": "b.txt",
                                    "offset": 0,
                                    "bytes": 5,
                                    "artifact_sha256": hashlib.sha256(b"bravo").hexdigest(),
                                },
                            ],
                            "state": "pending",
                        },
                    }
                ]
            )
            return CollectionUploadWorkBatchDocument.model_validate(
                {
                    "collection_id": collection_id,
                    "planning_complete": True,
                    "complete": not work,
                    "committed_payload_bytes": 10 if committed else 0,
                    "work": work[:limit],
                }
            )

        def put_collection_upload_session_unit(
            self,
            collection_id: int,
            volume_id: str,
            unit: int,
            *,
            plan_sha256: str,
            content: bytes,
        ) -> CollectionUploadUnitWorkDocument:
            nonlocal committed
            assert (collection_id, volume_id, unit) == (
                COLLECTION_ID,
                f"pack-{0:064x}",
                0,
            )
            assert plan_sha256 == "a" * 64
            accepted = self.acquire_collection_upload_session_work(collection_id).work[0].unit
            uploaded.extend(content)
            committed = True
            return accepted.model_copy(update={"state": "committed"})

        def get_collection_upload_session(self, collection_id: int) -> dict[str, object]:
            assert collection_id == COLLECTION_ID
            return {
                "collection_id": collection_id,
                "state": "finalized",
                "files_total": 2,
                "bytes_total": 10,
                "registration_constraints": None,
            }

    monkeypatch.setenv("RIVERHOG_CLI_PLAIN", "1")
    payload = riverhog_main._upload_collection_via_session(
        Api(),  # type: ignore[arg-type]
        "test-upload",
        root,
        ingest_source=str(root),
        file_concurrency=1,
        json_mode=True,
        provenance_observer_factory=native_provenance_observer,
    )

    assert payload["state"] == "finalized"
    assert sorted(str(item["path"]) for item in registered) == ["a.txt", "b.txt"]
    assert all(item["provenance"]["status"] == "captured" for item in registered)  # type: ignore[index]
    assert bytes(uploaded) == b"alphabravo"


def test_upload_retry_returns_an_already_finalized_collection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "collection"
    root.mkdir()
    (root / "clip.bin").write_bytes(b"video")
    finalized = {
        "collection_id": COLLECTION_ID,
        "state": "finalized",
        "files_total": 1,
        "bytes_total": 5,
        "custody": {"state": "complete"},
        "registration_constraints": None,
    }

    class Api:
        def create_or_resume_collection_upload_session(
            self,
            *_args: object,
            **_kwargs: object,
        ) -> dict[str, object]:
            return finalized

    def forbidden_hash(_path: Path) -> str:
        raise AssertionError("a finalized retry must not hash local files")

    monkeypatch.setattr(riverhog_main, "_file_sha256", forbidden_hash)

    assert (
        riverhog_main._upload_collection_via_session(
            Api(),  # type: ignore[arg-type]
            "test-upload",
            root,
            ingest_source=str(root),
            file_concurrency=2,
        )
        == finalized
    )


def test_finalization_watch_returns_verified_custody(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payloads = [
        {"collection_id": COLLECTION_ID, "state": "finalizing"},
        {"collection_id": COLLECTION_ID, "state": "finalized"},
    ]
    sleeps: list[float] = []
    monkeypatch.setenv("RIVERHOG_UPLOAD_FINALIZE_POLL_SECONDS", "0.01")
    monkeypatch.setattr(riverhog_main.time, "sleep", sleeps.append)

    class Api:
        def get_collection_upload_session(self, collection_id: int) -> dict[str, object]:
            assert collection_id == COLLECTION_ID
            return payloads.pop(0)

    payload, state = riverhog_main._wait_for_finalized_collection(
        Api(),  # type: ignore[arg-type]
        COLLECTION_ID,
        None,
    )

    assert state == "finalized"
    assert payload["state"] == "finalized"
    assert sleeps == [0.01]


def test_plain_upload_progress_describes_direct_transport() -> None:
    line = format_upload_progress_line(
        CollectionUploadProgressState(
            collection_id=COLLECTION_ID,
            phase="uploading",
            files_uploaded=1,
            files_total=2,
            files_hashed=2,
            files_registered=2,
            uploaded_bytes=5,
            bytes_total=10,
            rate_bytes_per_second=5,
            file_concurrency=2,
            chunk_bytes=5 * 1024 * 1024,
        )
    )

    assert "collection upload 1" in line
    assert "1/2 files" in line
    assert "5 B / 10 B" in line
    assert "2 worker(s)" in line


def test_collection_upload_control_commands_have_human_json_parity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def upload(state: str = "finalized") -> dict[str, object]:
        return {
            "collection_id": COLLECTION_ID,
            "state": state,
            "files_total": 2,
            "bytes_total": 10,
            "custody": {"state": "complete"},
            "encryption_format": "age-v1-scrypt",
            "passphrase_id": "fixture-archive-key-v1",
            "created_at": "2026-08-13T00:00:00Z",
        }

    class Api:
        def list_collection_upload_sessions(self, **_kwargs: object) -> dict[str, object]:
            return {
                "page_size": 25,
                "next_page_token": None,
                "uploads": [upload()],
            }

        def get_collection_upload_session(self, collection_id: int) -> dict[str, object]:
            assert collection_id == COLLECTION_ID
            return upload()

        def cancel_collection_upload_session(self, collection_id: int) -> dict[str, object]:
            assert collection_id == COLLECTION_ID
            return upload("canceled")

    monkeypatch.setattr(riverhog_main, "client", Api)

    cases = (
        (["collection", "upload", "list"], "finalized"),
        (["collection", "upload", "show", str(COLLECTION_ID)], "finalized"),
        (["collection", "upload", "cancel", str(COLLECTION_ID)], "canceled"),
        (["collection", "upload", "watch", str(COLLECTION_ID)], "finalized"),
    )
    for arguments, state in cases:
        human = RUNNER.invoke(riverhog_main.app, arguments)
        structured = RUNNER.invoke(riverhog_main.app, [*arguments, "--json"])
        assert human.exit_code == 0, human.output
        assert structured.exit_code == 0, structured.output
        assert str(COLLECTION_ID) in human.stdout
        assert state in human.stdout
        assert "age-v1-scrypt:fixture-archive-key-v1" in human.stdout
        payload = json.loads(structured.stdout)
        if "uploads" in payload:
            payload = payload["uploads"][0]
        assert payload["collection_id"] == COLLECTION_ID
        assert payload["state"] == state


def test_collection_upload_custody_files_and_guarded_discard_have_human_json_parity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    challenge = "discard-upload:fixture"
    file_payload = {
        "path": "video/archive.mkv",
        "bytes": 10,
        "sha256": "a" * 64,
        "provenance": {
            "status": "omitted",
            "omission_reason": "fixture",
        },
        "custody_receipt": {
            "format": "riverhog-artifact-custody-receipt/v1",
            "collection_id": COLLECTION_ID,
            "path": "video/archive.mkv",
            "bytes": 10,
            "sha256": "a" * 64,
            "archive_objects": [{"volume_id": f"pack-{0:064x}", "sealed_receipt_sha256": "b" * 64}],
            "receipt_sha256": "c" * 64,
        },
    }

    class Api:
        def list_collection_upload_session_files(
            self,
            collection_id: int,
            **_kwargs: object,
        ) -> dict[str, object]:
            assert collection_id == COLLECTION_ID
            return {
                "page_size": 1,
                "next_page_token": None,
                "files": [file_payload],
            }

        def plan_collection_upload_discard(self, collection_id: int) -> dict[str, object]:
            assert collection_id == COLLECTION_ID
            return {
                "status": "ready",
                "collection_id": collection_id,
                "state": "orphaned",
                "files": 1,
                "bytes": 10,
                "custody": {"state": "complete"},
                "archive_objects": 1,
                "warning": "This permanently destroys Riverhog-custodied artifacts.",
                "blockers": [],
                "expires_at": "2026-08-25T01:00:00Z",
                "challenge": challenge,
            }

        def discard_collection_upload(
            self,
            collection_id: int,
            *,
            challenge: str,
        ) -> dict[str, object]:
            assert collection_id == COLLECTION_ID
            assert challenge == "discard-upload:fixture"
            return {
                "status": "discarded",
                "collection_id": collection_id,
                "files": 1,
                "bytes": 10,
                "custody": {"state": "complete"},
                "archive_objects": 1,
            }

    monkeypatch.setattr(riverhog_main, "client", Api)
    cases = (
        (["collection", "upload", "files", str(COLLECTION_ID)], "custodied", "files"),
        (
            ["collection", "upload", "discard", str(COLLECTION_ID), "--dry-run"],
            "permanently destroys",
            "status",
        ),
        (
            [
                "collection",
                "upload",
                "discard",
                str(COLLECTION_ID),
                "--confirm",
                challenge,
            ],
            "discarded",
            "status",
        ),
    )
    for arguments, expected_human, structured_key in cases:
        human = RUNNER.invoke(riverhog_main.app, arguments)
        structured = RUNNER.invoke(riverhog_main.app, [*arguments, "--json"])
        assert human.exit_code == structured.exit_code == 0
        assert expected_human in human.stdout
        assert structured_key in json.loads(structured.stdout)
