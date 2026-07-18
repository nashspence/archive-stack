from __future__ import annotations

import hashlib
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from riverhog_api.external_auth import require_external_app
from riverhog_core.catalog_db import initialize_db, make_session_factory, session_scope
from riverhog_core.catalog_models import AppKeyRecord
from riverhog_core.domain.errors import BadRequest
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.app_keys import SqlAlchemyAppKeyService
from tests.unit.db_helpers import sqlite_url


def app_keys(tmp_path: Path) -> tuple[SqlAlchemyAppKeyService, RuntimeConfig]:
    config = RuntimeConfig(database_url=sqlite_url(tmp_path / "catalog.sqlite3"))
    initialize_db(config.database_url)
    return SqlAlchemyAppKeyService(config), config


def test_app_key_plaintext_is_returned_once_and_only_its_digest_is_stored(
    tmp_path: Path,
) -> None:
    service, config = app_keys(tmp_path)

    created = service.create(app="Review-Station")

    assert created["app"] == "review-station"
    assert created["status"] == "active"
    assert str(created["token"]).startswith("rh_app_")
    factory = make_session_factory(config.database_url)
    with session_scope(factory) as session:
        record = session.get(AppKeyRecord, str(created["id"]))
        assert record is not None
        assert record.token_sha256 == hashlib.sha256(
            str(created["token"]).encode("utf-8")
        ).hexdigest()
        assert not hasattr(record, "token")

    listed = service.list_keys(
        app="review-station",
        page=1,
        per_page=25,
        q=None,
        sort="created_at",
        order="desc",
    )
    assert listed["keys"] == [{key: created[key] for key in created if key != "token"}]


def test_app_keys_rotate_revoke_expire_and_authenticate_without_restart(tmp_path: Path) -> None:
    service, config = app_keys(tmp_path)
    first = service.create(app="local")
    second = service.create(app="local")

    assert service.authenticate(str(first["token"])) == "local"
    assert service.authenticate(str(second["token"])) == "local"
    assert service.authenticate("wrong") is None

    revoked = service.revoke(app="local", key_id=str(first["id"]))
    assert revoked["status"] == "revoked"
    assert service.authenticate(str(first["token"])) is None
    assert service.authenticate(str(second["token"])) == "local"

    factory = make_session_factory(config.database_url)
    with session_scope(factory) as session:
        record = session.get(AppKeyRecord, str(second["id"]))
        assert record is not None
        record.expires_at = "2000-01-01T00:00:00.000000Z"

    assert service.authenticate(str(second["token"])) is None
    inactive = service.list_keys(
        app="local",
        page=1,
        per_page=25,
        q=None,
        sort="id",
        order="asc",
        active=False,
    )
    assert {key["status"] for key in inactive["keys"]} == {"expired", "revoked"}


def test_app_list_aggregates_and_filters_in_the_database(tmp_path: Path) -> None:
    service, _config = app_keys(tmp_path)
    alpha_first = service.create(app="alpha")
    service.create(app="alpha")
    service.create(app="beta")
    service.revoke(app="alpha", key_id=str(alpha_first["id"]))

    payload = service.list_apps(
        page=1,
        per_page=1,
        q="a",
        sort="keys",
        order="desc",
        active=True,
    )

    assert payload["total"] == 2
    assert payload["pages"] == 2
    assert payload["apps"][0]["name"] == "alpha"
    assert payload["apps"][0]["keys"] == 2
    assert payload["apps"][0]["active_keys"] == 1


def test_app_names_and_expiry_are_validated(tmp_path: Path) -> None:
    service, _config = app_keys(tmp_path)

    with pytest.raises(BadRequest, match="app name"):
        service.create(app="not/an/app")
    with pytest.raises(BadRequest, match="expiry"):
        service.create(app="local", expires_in=timedelta(0))


def test_external_app_authentication_fails_closed() -> None:
    class Keys:
        def authenticate(self, token: str) -> str | None:
            return "local" if token == "valid" else None

    container = SimpleNamespace(app_keys=Keys())
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="valid")
    assert require_external_app(credentials, container) == "local"

    with pytest.raises(HTTPException) as missing:
        require_external_app(None, container)
    assert missing.value.status_code == 401

    with pytest.raises(HTTPException) as invalid:
        require_external_app(
            HTTPAuthorizationCredentials(scheme="Bearer", credentials="invalid"),
            container,
        )
    assert invalid.value.status_code == 401
