from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from riverhog_api import deps
from riverhog_core.catalog_db import initialize_db, make_session_factory, session_scope
from riverhog_core.catalog_models import CollectionArchiveCopyRecord, CollectionRecord
from riverhog_core.runtime_config import RuntimeConfig, StorageAdapterRegistration

from tests.unit.db_helpers import sqlite_url


def test_default_container_closes_startup_resources_after_adapter_rejection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registration = StorageAdapterRegistration(
        name="archive",
        base_url="http://adapter.example.test",
        token_file=tmp_path / "adapter.token",
        allow_insecure_http=True,
    )
    config = RuntimeConfig(
        database_url="sqlite+pysqlite:///:memory:",
        archive_stores={"archive": registration},
    )
    session_factory = object()
    closed: list[str] = []

    class RejectingAdapter:
        def check_readiness(self) -> None:
            return None

        def descriptor(self) -> Any:
            return SimpleNamespace(
                minimum_nonfinal_part_bytes=config.archive_multipart_part_bytes + 1,
                maximum_part_bytes=config.archive_multipart_part_bytes + 2,
            )

        def close(self) -> None:
            closed.append("adapter")

    monkeypatch.setattr(deps, "load_runtime_config", lambda: config)
    monkeypatch.setattr(deps, "validate_db", lambda _url: None)
    monkeypatch.setattr(deps, "make_session_factory", lambda _url: session_factory)
    monkeypatch.setattr(deps, "dispose_session_factory", lambda _factory: closed.append("db"))
    monkeypatch.setattr(deps, "_adapter_client", lambda _registration: RejectingAdapter())
    monkeypatch.setattr(
        deps,
        "_require_archive_encryption_bindings",
        lambda *_args, **_kwargs: None,
    )
    deps.default_container.cache_clear()

    with pytest.raises(ValueError, match="does not accept the configured multipart size"):
        deps.default_container()

    assert closed == ["adapter", "db"]
    deps.default_container.cache_clear()


def test_startup_rejects_a_persisted_key_id_without_its_secret(tmp_path: Path) -> None:
    database_url = sqlite_url(tmp_path / "catalog.sqlite3")
    initialize_db(database_url)
    factory = make_session_factory(database_url)
    with session_scope(factory) as session:
        session.add(
            CollectionRecord(
                id=1,
                creation_idempotency_key="fixture",
                content_identity="a" * 64,
                encryption_format="age-v1-scrypt",
                passphrase_id="removed-archive-key-v1",
                provenance_mode="omitted",
                provenance_identity=None,
                record_etag="b" * 64,
                metadata_revision=1,
                metadata_updated_at="2026-08-24T00:00:00.000000Z",
                created_by_app="fixture",
                created_at="2026-08-24T00:00:00.000000Z",
            )
        )

    with pytest.raises(ValueError, match="removed-archive-key-v1"):
        deps._require_archive_encryption_bindings(
            RuntimeConfig(database_url=database_url),
            session_factory=factory,
        )


def test_startup_rejects_an_uploaded_copy_without_recovery_descriptor(tmp_path: Path) -> None:
    database_url = sqlite_url(tmp_path / "catalog.sqlite3")
    initialize_db(database_url)
    factory = make_session_factory(database_url)
    with session_scope(factory) as session:
        session.add(
            CollectionRecord(
                id=1,
                creation_idempotency_key="fixture",
                content_identity="a" * 64,
                encryption_format="age-v1-scrypt",
                passphrase_id="riverhog-dev-key-v1",
                provenance_mode="omitted",
                provenance_identity=None,
                record_etag="b" * 64,
                metadata_revision=1,
                metadata_updated_at="2026-08-24T00:00:00.000000Z",
                created_by_app="fixture",
                created_at="2026-08-24T00:00:00.000000Z",
            )
        )
        session.add(
            CollectionArchiveCopyRecord(
                collection_id=1,
                store="archive",
                state="uploaded",
                archive_storage_prefix="archives/fixture",
                last_uploaded_at="2026-08-24T00:00:00.000000Z",
                last_verified_at="2026-08-24T00:00:00.000000Z",
            )
        )

    with pytest.raises(ValueError, match="no recovery descriptor"):
        deps._require_archive_encryption_bindings(
            RuntimeConfig(database_url=database_url),
            session_factory=factory,
        )
