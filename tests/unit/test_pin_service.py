from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator
from pathlib import Path

import pytest
from sqlalchemy import select

from riverhog_core.catalog_db import initialize_db, make_session_factory, session_scope
from riverhog_core.catalog_models import (
    CollectionFileRecord,
    CollectionRecord,
    FinalizedImageCoveredPathRecord,
    FinalizedImageRecord,
    ImageCopyRecord,
)
from riverhog_core.domain.errors import Conflict
from riverhog_core.ports.hot_store import HotFileStat
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.pins import SqlAlchemyPinService
from tests.unit.db_helpers import sqlite_url


class _FakeHotStore:
    def __init__(self) -> None:
        self._files: dict[tuple[str, str], bytes] = {}
        self.deleted: list[tuple[str, str]] = []

    def put_collection_file(self, collection_id: str, path: str, content: bytes) -> None:
        self._files[(collection_id, path)] = content

    def put_collection_file_stream(
        self,
        collection_id: str,
        path: str,
        chunks: Iterable[bytes],
        *,
        content_length: int,
        sha256: str | None = None,
    ) -> None:
        _ = sha256
        content = b"".join(chunks)
        assert len(content) == content_length
        self._files[(collection_id, path)] = content

    def get_collection_file(self, collection_id: str, path: str) -> bytes:
        return self._files[(collection_id, path)]

    def iter_collection_file(
        self,
        collection_id: str,
        path: str,
        *,
        offset: int = 0,
        size: int | None = None,
    ) -> Iterator[bytes]:
        content = self.get_collection_file(collection_id, path)
        yield content[offset:] if size is None else content[offset : offset + size]

    def stat_collection_file(self, collection_id: str, path: str) -> HotFileStat | None:
        content = self._files.get((collection_id, path))
        if content is None:
            return None
        return HotFileStat(bytes=len(content), sha256=hashlib.sha256(content).hexdigest())

    def has_collection_file(self, collection_id: str, path: str) -> bool:
        return (collection_id, path) in self._files

    def delete_collection_file(self, collection_id: str, path: str) -> None:
        self.deleted.append((collection_id, path))
        self._files.pop((collection_id, path), None)

    def list_collection_files(self, collection_id: str) -> list[tuple[str, int]]:
        return [
            (path, len(content))
            for (stored_collection_id, path), content in sorted(self._files.items())
            if stored_collection_id == collection_id
        ]


class _FakeUploadStore:
    def create_upload(self, target_path: str, length: int) -> str:
        raise AssertionError("create_upload should not be called")

    def get_offset(self, tus_url: str) -> int:
        raise AssertionError("get_offset should not be called")

    def append_upload_chunk(
        self,
        tus_url: str,
        *,
        offset: int,
        checksum: str,
        content: bytes,
    ) -> tuple[int, str | None]:
        raise AssertionError("append_upload_chunk should not be called")

    def read_target(self, target_path: str) -> bytes:
        raise AssertionError("read_target should not be called")

    def iter_target(
        self,
        target_path: str,
        *,
        offset: int = 0,
        size: int | None = None,
    ) -> Iterator[bytes]:
        _ = target_path, offset, size
        raise AssertionError("iter_target should not be called")

    def delete_target(self, target_path: str) -> None:
        return None

    def cancel_upload(self, tus_url: str) -> None:
        return None


def _config(sqlite_path: Path) -> RuntimeConfig:
    return RuntimeConfig(
        object_store="s3",
        s3_endpoint_url="http://example.invalid:9000",
        s3_region="us-east-1",
        s3_bucket="riverhog",
        s3_access_key_id="test-access",
        s3_secret_access_key="test-secret",
        s3_force_path_style=True,
        tusd_base_url="http://example.invalid:1080/files",
        tusd_hook_secret="hook-secret",
        database_url=sqlite_url(sqlite_path),
    )


def _seed_hot_docs(sqlite_path: Path, hot_store: _FakeHotStore) -> None:
    files = {
        "tax/2022/invoice-123.pdf": b"invoice",
        "tax/2022/receipt-456.pdf": b"receipt",
        "letters/cover.txt": b"cover",
    }
    session_factory = make_session_factory(sqlite_url(sqlite_path))
    with session_scope(session_factory) as session:
        session.add(CollectionRecord(id="docs"))
        for path, content in files.items():
            hot_store.put_collection_file("docs", path, content)
            session.add(
                CollectionFileRecord(
                    collection_id="docs",
                    path=path,
                    bytes=len(content),
                    sha256=hashlib.sha256(content).hexdigest(),
                    hot=True,
                    archived=True,
                )
            )


def _mark_docs_fully_compliant(sqlite_path: Path) -> None:
    session_factory = make_session_factory(sqlite_url(sqlite_path))
    with session_scope(session_factory) as session:
        session.add(
            FinalizedImageRecord(
                image_id="20260530T000000Z",
                candidate_id="candidate-docs",
                filename="20260530T000000Z.iso",
                bytes=1234,
                image_root="/tmp/docs-image",
                target_bytes=50_000_000_000,
                required_copy_count=2,
            )
        )
        for path in {
            "tax/2022/invoice-123.pdf",
            "tax/2022/receipt-456.pdf",
            "letters/cover.txt",
        }:
            session.add(
                FinalizedImageCoveredPathRecord(
                    image_id="20260530T000000Z",
                    collection_id="docs",
                    path=path,
                )
            )
        for ordinal in (1, 2):
            session.add(
                ImageCopyRecord(
                    image_id="20260530T000000Z",
                    copy_id=f"20260530T000000Z-{ordinal}",
                    label_text=f"20260530T000000Z-{ordinal}",
                    location="test shelf",
                    created_at="2026-05-30T00:00:00Z",
                    state="registered",
                    verification_state="verified",
                )
            )


def _hot_paths(sqlite_path: Path) -> set[str]:
    session_factory = make_session_factory(sqlite_url(sqlite_path))
    with session_scope(session_factory) as session:
        return {
            record.path
            for record in session.scalars(select(CollectionFileRecord)).all()
            if record.hot
        }


def test_releasing_one_file_removes_only_that_file(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(sqlite_url(sqlite_path))
    hot_store = _FakeHotStore()
    _seed_hot_docs(sqlite_path, hot_store)
    _mark_docs_fully_compliant(sqlite_path)
    service = SqlAlchemyPinService(_config(sqlite_path), hot_store, _FakeUploadStore())

    service.pin("docs/tax/2022/invoice-123.pdf")
    service.release("docs/tax/2022/invoice-123.pdf")

    assert hot_store.deleted == [("docs", "tax/2022/invoice-123.pdf")]
    assert _hot_paths(sqlite_path) == {
        "tax/2022/receipt-456.pdf",
        "letters/cover.txt",
    }


def test_releasing_broad_pin_preserves_remaining_narrow_pin(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(sqlite_url(sqlite_path))
    hot_store = _FakeHotStore()
    _seed_hot_docs(sqlite_path, hot_store)
    _mark_docs_fully_compliant(sqlite_path)
    service = SqlAlchemyPinService(_config(sqlite_path), hot_store, _FakeUploadStore())

    service.pin("docs/tax/")
    service.pin("docs/tax/2022/invoice-123.pdf")
    service.release("docs/tax/")

    assert hot_store.deleted == [("docs", "tax/2022/receipt-456.pdf")]
    assert _hot_paths(sqlite_path) == {
        "tax/2022/invoice-123.pdf",
        "letters/cover.txt",
    }


def test_releasing_missing_pin_does_not_remove_unrelated_hot_files(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(sqlite_url(sqlite_path))
    hot_store = _FakeHotStore()
    _seed_hot_docs(sqlite_path, hot_store)
    service = SqlAlchemyPinService(_config(sqlite_path), hot_store, _FakeUploadStore())

    service.release("docs/missing/")

    assert hot_store.deleted == []
    assert _hot_paths(sqlite_path) == {
        "tax/2022/invoice-123.pdf",
        "tax/2022/receipt-456.pdf",
        "letters/cover.txt",
    }


def test_releasing_noncompliant_pin_is_refused(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    initialize_db(sqlite_url(sqlite_path))
    hot_store = _FakeHotStore()
    _seed_hot_docs(sqlite_path, hot_store)
    service = SqlAlchemyPinService(_config(sqlite_path), hot_store, _FakeUploadStore())

    service.pin("docs/tax/2022/invoice-123.pdf")

    with pytest.raises(Conflict, match="non-compliant file"):
        service.release("docs/tax/2022/invoice-123.pdf")

    assert hot_store.deleted == []
    assert _hot_paths(sqlite_path) == {
        "tax/2022/invoice-123.pdf",
        "tax/2022/receipt-456.pdf",
        "letters/cover.txt",
    }
