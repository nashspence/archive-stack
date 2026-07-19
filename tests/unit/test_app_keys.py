from __future__ import annotations

import hashlib
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from riverhog_api.auth import require_application, require_permission
from riverhog_core.app_permissions import (
    CATALOG_READ,
    COLLECTIONS_UPLOAD,
    KEYS_MANAGE,
    RETRIEVAL_MANAGE,
    ApplicationPrincipal,
)
from riverhog_core.catalog_db import initialize_db, make_session_factory, session_scope
from riverhog_core.catalog_models import AppKeyRecord
from riverhog_core.domain.errors import BadRequest, Forbidden
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.app_keys import SqlAlchemyAppKeyService
from tests.unit.db_helpers import sqlite_url

BOOTSTRAP = ApplicationPrincipal(
    app="bootstrap",
    key_id=None,
    permissions=frozenset({KEYS_MANAGE}),
    unrestricted_delegation=True,
)


def app_keys(tmp_path: Path) -> tuple[SqlAlchemyAppKeyService, RuntimeConfig]:
    config = RuntimeConfig(database_url=sqlite_url(tmp_path / "catalog.sqlite3"))
    initialize_db(config.database_url)
    return SqlAlchemyAppKeyService(config), config


def create_key(
    service: SqlAlchemyAppKeyService,
    *,
    app: str,
    permissions: tuple[str, ...] = (CATALOG_READ,),
    grantor: ApplicationPrincipal = BOOTSTRAP,
    expires_in: timedelta | None = None,
) -> dict[str, object]:
    return service.create(
        app=app,
        permissions=permissions,
        grantor=grantor,
        expires_in=expires_in,
    )


def test_app_key_plaintext_is_returned_once_and_only_its_digest_is_stored(
    tmp_path: Path,
) -> None:
    service, config = app_keys(tmp_path)

    created = create_key(
        service,
        app="Review-Station",
        permissions=(RETRIEVAL_MANAGE, CATALOG_READ),
    )

    assert created["app"] == "review-station"
    assert created["permissions"] == [CATALOG_READ, RETRIEVAL_MANAGE]
    assert created["status"] == "active"
    assert str(created["token"]).startswith("rh_app_")
    factory = make_session_factory(config.database_url)
    with session_scope(factory) as session:
        record = session.get(AppKeyRecord, str(created["id"]))
        assert record is not None
        assert record.token_sha256 == hashlib.sha256(
            str(created["token"]).encode("utf-8")
        ).hexdigest()
        assert record.permissions_json == '["catalog:read","retrieval:manage"]'
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
    first = create_key(service, app="local", permissions=(CATALOG_READ,))
    second = create_key(service, app="local", permissions=(RETRIEVAL_MANAGE,))

    first_principal = service.authenticate(str(first["token"]))
    second_principal = service.authenticate(str(second["token"]))
    assert first_principal is not None
    assert first_principal.app == "local"
    assert first_principal.permissions == frozenset({CATALOG_READ})
    assert second_principal is not None
    assert second_principal.permissions == frozenset({RETRIEVAL_MANAGE})
    assert service.authenticate("wrong") is None

    revoked = service.revoke(app="local", key_id=str(first["id"]))
    assert revoked["status"] == "revoked"
    assert service.authenticate(str(first["token"])) is None
    assert service.authenticate(str(second["token"])) is not None

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


def test_key_delegation_cannot_exceed_the_grantor(tmp_path: Path) -> None:
    service, _config = app_keys(tmp_path)
    manager = ApplicationPrincipal(
        app="manager",
        key_id="manager-key",
        permissions=frozenset({KEYS_MANAGE, CATALOG_READ}),
    )

    delegated = create_key(
        service,
        app="reader",
        permissions=(CATALOG_READ,),
        grantor=manager,
    )
    assert delegated["permissions"] == [CATALOG_READ]
    with pytest.raises(Forbidden, match="cannot grant"):
        create_key(
            service,
            app="uploader",
            permissions=(COLLECTIONS_UPLOAD,),
            grantor=manager,
        )


def test_app_list_aggregates_and_filters_in_the_database(tmp_path: Path) -> None:
    service, _config = app_keys(tmp_path)
    alpha_first = create_key(service, app="alpha")
    create_key(service, app="alpha")
    create_key(service, app="beta")
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


def test_app_names_permissions_and_expiry_are_validated(tmp_path: Path) -> None:
    service, _config = app_keys(tmp_path)

    with pytest.raises(BadRequest, match="app name"):
        create_key(service, app="not/an/app")
    with pytest.raises(BadRequest, match="unknown application permission"):
        create_key(service, app="local", permissions=("unknown:permission",))
    with pytest.raises(BadRequest, match="expiry"):
        create_key(service, app="local", expires_in=timedelta(0))


def test_application_authentication_distinguishes_missing_and_forbidden_permissions() -> None:
    principal = ApplicationPrincipal(
        app="local",
        key_id="key",
        permissions=frozenset({CATALOG_READ}),
    )

    class Keys:
        def authenticate(self, token: str) -> ApplicationPrincipal | None:
            return principal if token == "valid" else None

    container = SimpleNamespace(app_keys=Keys())
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="valid")
    assert require_application(credentials, container) == principal
    assert require_permission(CATALOG_READ)(credentials, container) == principal

    with pytest.raises(HTTPException) as missing:
        require_application(None, container)
    assert missing.value.status_code == 401

    with pytest.raises(HTTPException) as forbidden:
        require_permission(COLLECTIONS_UPLOAD)(credentials, container)
    assert forbidden.value.status_code == 403
