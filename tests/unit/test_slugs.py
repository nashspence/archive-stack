from __future__ import annotations

from pathlib import Path

from riverhog_core.app_permissions import (
    CATALOG_READ,
    COLLECTIONS_UPLOAD,
    KEYS_MANAGE,
    SLUGS_CREATE,
    ApplicationAccess,
    ApplicationPrincipal,
)
from riverhog_core.catalog_db import initialize_db
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.app_keys import SqlAlchemyAppKeyService
from riverhog_core.services.slugs import SqlAlchemySlugService

from tests.unit.db_helpers import sqlite_url

BOOTSTRAP = ApplicationPrincipal(
    app="bootstrap",
    key_id=None,
    access=frozenset({ApplicationAccess(KEYS_MANAGE)}),
    unrestricted_delegation=True,
)


def _services(path: Path) -> tuple[SqlAlchemyAppKeyService, SqlAlchemySlugService]:
    config = RuntimeConfig(database_url=sqlite_url(path))
    initialize_db(config.database_url)
    return SqlAlchemyAppKeyService(config), SqlAlchemySlugService(config)


def test_slug_creation_grants_only_upload_access_to_the_creator(tmp_path: Path) -> None:
    keys, slugs = _services(tmp_path / "catalog.sqlite3")
    created = keys.create(
        app="munchy",
        access=(ApplicationAccess(SLUGS_CREATE),),
        grantor=BOOTSTRAP,
    )
    creator = keys.authenticate(str(created["token"]))
    assert creator is not None

    slug = slugs.create("camera", creator=creator)
    refreshed = keys.authenticate(str(created["token"]))

    assert slug["id"] == "camera"
    assert refreshed is not None
    assert refreshed.allows_slug(COLLECTIONS_UPLOAD, "camera")
    assert not refreshed.allows_slug(CATALOG_READ, "camera")


def test_slug_list_uses_catalog_scoping_and_standard_page_projection(tmp_path: Path) -> None:
    keys, slugs = _services(tmp_path / "catalog.sqlite3")
    slugs.create("camera", creator=BOOTSTRAP)
    slugs.create("documents", creator=BOOTSTRAP)
    reader_key = keys.create(
        app="reader",
        access=(ApplicationAccess(CATALOG_READ, "slug:camera"),),
        grantor=BOOTSTRAP,
    )
    reader = keys.authenticate(str(reader_key["token"]))
    assert reader is not None

    page = slugs.list(
        page=9,
        per_page=1,
        q="cam",
        sort="id",
        order="asc",
        all_items=True,
        principal=reader,
    )

    assert {key: page[key] for key in ("page", "per_page", "total", "pages")} == {
        "page": 1,
        "per_page": 1,
        "total": 1,
        "pages": 1,
    }
    assert {key: page[key] for key in ("sort", "order", "query")} == {
        "sort": "id",
        "order": "asc",
        "query": "cam",
    }
    row = page["slugs"][0]
    assert row["id"] == "camera"
    assert row["created_by_app"] == "bootstrap"
    assert row["collections"] == 0
    assert row["uploads"] == 0
