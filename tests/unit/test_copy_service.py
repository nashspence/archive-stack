from __future__ import annotations

from pathlib import Path

from arc_core.catalog_models import (
    CollectionFileRecord,
    CollectionRecord,
    FinalizedImageCoveredPathRecord,
    FinalizedImageRecord,
)
from arc_core.domain.enums import CopyState
from arc_core.runtime_config import RuntimeConfig
from arc_core.services.copies import SqlAlchemyCopyService
from arc_core.sqlite_db import initialize_db, make_session_factory, session_scope
from tests.fixtures.data import DOCS_FILES, IMAGE_ONE_FILES, write_tree


class _FakeHotStore:
    def get_collection_file(self, collection_id: str, path: str) -> bytes:
        assert collection_id == "docs"
        return DOCS_FILES[path]


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
        sqlite_path=sqlite_path,
    )


def _seed_finalized_image(sqlite_path: Path, image_root: Path) -> None:
    session_factory = make_session_factory(str(sqlite_path))
    with session_scope(session_factory) as session:
        session.add(CollectionRecord(id="docs"))
        for relative_path, content in DOCS_FILES.items():
            session.add(
                CollectionFileRecord(
                    collection_id="docs",
                    path=relative_path,
                    bytes=len(content),
                    sha256="a" * 64,
                    hot=True,
                    archived=False,
                )
            )

        session.add(
            FinalizedImageRecord(
                image_id="20260420T040001Z",
                candidate_id="img_2026-04-20_01",
                filename="20260420T040001Z.iso",
                bytes=sum(len(content) for content in DOCS_FILES.values()),
                image_root=str(image_root),
                target_bytes=10_000,
                required_copy_count=2,
                glacier_state="uploaded",
            )
        )
        for relative_path in (
            "tax/2022/invoice-123.pdf",
            "tax/2022/receipt-456.pdf",
        ):
            session.add(
                FinalizedImageCoveredPathRecord(
                    image_id="20260420T040001Z",
                    collection_id="docs",
                    path=relative_path,
                )
            )


def test_marking_one_confirmed_copy_lost_creates_a_fresh_replacement_slot(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "state.sqlite3"
    image_root = tmp_path / "image-root"
    initialize_db(str(sqlite_path))
    write_tree(image_root, IMAGE_ONE_FILES)
    _seed_finalized_image(sqlite_path, image_root)

    service = SqlAlchemyCopyService(_config(sqlite_path), _FakeHotStore())

    initial = service.list_for_image("20260420T040001Z")
    assert [str(copy.id) for copy in initial] == ["20260420T040001Z-1", "20260420T040001Z-2"]

    service.register("20260420T040001Z", "Shelf A1", copy_id="20260420T040001Z-1")
    service.register("20260420T040001Z", "Shelf B1", copy_id="20260420T040001Z-2")

    updated = service.update("20260420T040001Z", "20260420T040001Z-1", state="lost")

    assert updated.state == CopyState.LOST
    assert [entry.event for entry in updated.history] == ["created", "registered", "state_updated"]

    copies = service.list_for_image("20260420T040001Z")
    assert [str(copy.id) for copy in copies] == [
        "20260420T040001Z-1",
        "20260420T040001Z-2",
        "20260420T040001Z-3",
    ]
    assert [copy.state for copy in copies] == [
        CopyState.LOST,
        CopyState.REGISTERED,
        CopyState.NEEDED,
    ]
