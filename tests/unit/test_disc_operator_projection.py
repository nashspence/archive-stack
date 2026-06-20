from __future__ import annotations

from pathlib import Path

from riverhog_core.catalog_db import initialize_db, make_session_factory, session_scope
from riverhog_core.catalog_models import (
    DiscOperatorSummaryRecord,
    FinalizedImageRecord,
    ImageCopyEventRecord,
    ImageCopyRecord,
)
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.copies import SqlAlchemyCopyService
from tests.unit.db_helpers import sqlite_url


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


def _seed_image(sqlite_path: Path, *, with_copies: bool = True) -> None:
    session_factory = make_session_factory(sqlite_url(sqlite_path))
    with session_scope(session_factory) as session:
        session.add(
            FinalizedImageRecord(
                image_id="20260616T121459Z",
                candidate_id="cand-1",
                filename="20260616T121459Z.iso",
                bytes=100,
                image_root="/tmp/image",
                target_bytes=200,
                required_copy_count=2,
            )
        )
        if not with_copies:
            return
        session.add_all(
            [
                ImageCopyRecord(
                    image_id="20260616T121459Z",
                    copy_id="20260616T121459Z-1",
                    label_text="20260616T121459Z-1",
                    location="shelf-b",
                    created_at="2026-06-16T12:15:00Z",
                    state="registered",
                    verification_state="pending",
                ),
                ImageCopyRecord(
                    image_id="20260616T121459Z",
                    copy_id="20260616T121459Z-2",
                    label_text="20260616T121459Z-2",
                    location=None,
                    created_at="2026-06-16T12:16:00Z",
                    state="needed",
                    verification_state="pending",
                ),
            ]
        )

    session_factory = make_session_factory(sqlite_url(sqlite_path))
    with session_scope(session_factory) as session:
        session.add(
            ImageCopyEventRecord(
                image_id="20260616T121459Z",
                copy_id="20260616T121459Z-1",
                occurred_at="2026-06-16T12:15:00Z",
                event="registered",
                state="registered",
                verification_state="pending",
                location="shelf-b",
            )
        )


def _summary_row(sqlite_path: Path, copy_id: str) -> DiscOperatorSummaryRecord | None:
    session_factory = make_session_factory(sqlite_url(sqlite_path))
    with session_scope(session_factory) as session:
        return session.get(DiscOperatorSummaryRecord, ("20260616T121459Z", copy_id))


def test_disc_operator_projection_tracks_copy_and_image_changes(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_url(sqlite_path))
    _seed_image(sqlite_path)

    row = _summary_row(sqlite_path, "20260616T121459Z-1")
    assert row is not None
    assert row.filename == "20260616T121459Z.iso"
    assert row.state == "registered"
    assert row.verification_state == "pending"
    assert row.location == "shelf-b"

    session_factory = make_session_factory(sqlite_url(sqlite_path))
    with session_scope(session_factory) as session:
        copy = session.get(ImageCopyRecord, ("20260616T121459Z", "20260616T121459Z-1"))
        assert copy is not None
        copy.state = "verified"
        copy.verification_state = "verified"
        copy.location = "vault-a"
        image = session.get(FinalizedImageRecord, "20260616T121459Z")
        assert image is not None
        image.filename = "renamed.iso"

    row = _summary_row(sqlite_path, "20260616T121459Z-1")
    assert row is not None
    assert row.filename == "renamed.iso"
    assert row.state == "verified"
    assert row.verification_state == "verified"
    assert row.location == "vault-a"

    with session_scope(session_factory) as session:
        copy = session.get(ImageCopyRecord, ("20260616T121459Z", "20260616T121459Z-1"))
        assert copy is not None
        session.delete(copy)

    assert _summary_row(sqlite_path, "20260616T121459Z-1") is None


def test_disc_list_and_show_read_operator_projection(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_url(sqlite_path))
    _seed_image(sqlite_path)

    service = SqlAlchemyCopyService(_config(sqlite_path), hot_store=object())  # type: ignore[arg-type]
    page = service.list_discs(
        page=1,
        per_page=1,
        sort="location",
        order="desc",
        q="shelf",
        image_id=None,
    )
    assert page["total"] == 1
    assert page["pages"] == 1
    assert page["discs"] == [
        {
            "id": "20260616T121459Z-1",
            "image_id": "20260616T121459Z",
            "volume_id": "20260616T121459Z",
            "filename": "20260616T121459Z.iso",
            "label_text": "20260616T121459Z-1",
            "location": "shelf-b",
            "created_at": "2026-06-16T12:15:00Z",
            "state": "registered",
            "verification_state": "pending",
            "history": [],
        }
    ]

    shown = service.get_disc("20260616T121459Z-1")
    assert shown["image_id"] == "20260616T121459Z"
    history = shown["history"]
    assert isinstance(history, list)
    assert history[0]["event"] == "registered"


def test_image_scoped_disc_list_creates_bounded_copy_slots(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "catalog.sqlite3"
    initialize_db(sqlite_url(sqlite_path))
    _seed_image(sqlite_path, with_copies=False)

    service = SqlAlchemyCopyService(_config(sqlite_path), hot_store=object())  # type: ignore[arg-type]
    page = service.list_discs(
        page=1,
        per_page=25,
        sort="id",
        order="asc",
        q=None,
        image_id="20260616T121459Z",
    )

    assert page["total"] == 2
    discs = page["discs"]
    assert isinstance(discs, list)
    assert [disc["id"] for disc in discs if isinstance(disc, dict)] == [
        "20260616T121459Z-1",
        "20260616T121459Z-2",
    ]
