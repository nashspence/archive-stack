from __future__ import annotations

import hashlib
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi.security import HTTPAuthorizationCredentials
from riverhog_api.auth import require_application, require_permission
from riverhog_application_access import ApplicationAccessError
from riverhog_core.app_permissions import (
    CATALOG_READ,
    COLLECTIONS_CREATE,
    KEYS_MANAGE,
    QUOTAS_MANAGE,
    RETRIEVAL_MANAGE,
    ApplicationAccess,
    ApplicationPrincipal,
)
from riverhog_core.catalog_db import initialize_db, make_session_factory, session_scope
from riverhog_core.catalog_models import (
    AppKeyRecord,
    CollectionAccessGroupMembershipRecord,
    CollectionAccessGroupRecord,
    CollectionFileRecord,
    CollectionRecord,
    KeyDownloadReservationRecord,
    RetrievalJobRecord,
    RetrievalPlanFileRecord,
    RetrievalPlanRecord,
)
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.app_keys import SqlAlchemyAppKeyService
from riverhog_protocol.errors import BadRequest, Forbidden, NotFound, Unauthorized

from tests.unit.db_helpers import sqlite_url

BOOTSTRAP = ApplicationPrincipal(
    app="bootstrap",
    key_id=None,
    access=frozenset(
        {
            ApplicationAccess(KEYS_MANAGE),
            ApplicationAccess(QUOTAS_MANAGE),
        }
    ),
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
    access: tuple[ApplicationAccess, ...] = (ApplicationAccess(CATALOG_READ),),
    grantor: ApplicationPrincipal = BOOTSTRAP,
    expires_in: timedelta | None = None,
) -> dict[str, object]:
    return service.create(
        app=app,
        access=access,
        grantor=grantor,
        expires_in=expires_in,
    )


def seed_group(
    config: RuntimeConfig,
    label: str,
    *,
    collection_id: int | None = None,
) -> str:
    group_id = hashlib.sha256(label.encode()).hexdigest()
    factory = make_session_factory(config.database_url)
    with session_scope(factory) as session:
        session.add(
            CollectionAccessGroupRecord(
                id=group_id,
                creation_idempotency_key=label,
                created_by_app="bootstrap",
                created_by_key_id=None,
                display_label=label,
                status="active",
                authorization_revision=1,
                created_at="2026-07-24T00:00:00.000000Z",
                updated_at="2026-07-24T00:00:00.000000Z",
                collection_count=1 if collection_id is not None else 0,
            )
        )
        if collection_id is not None:
            session.add(
                CollectionRecord(
                    id=collection_id,
                    creation_idempotency_key=f"fixture-{collection_id}",
                    creation_identity_sha256=f"{collection_id:064x}",
                    creation_custody_mode="producer-retained",
                    content_identity="0" * 64,
                    encryption_format="age-v1-scrypt",
                    passphrase_id="fixture-archive-key-v1",
                    inventory_identity="1" * 64,
                    created_by_app="fixture",
                    created_at="2026-07-24T00:00:00.000000Z",
                    file_count=0,
                    file_bytes=0,
                )
            )
            session.add(
                CollectionAccessGroupMembershipRecord(
                    collection_id=collection_id,
                    group_id=group_id,
                    added_by_app="fixture",
                    added_at="2026-07-24T00:00:00.000000Z",
                )
            )
    return group_id


def test_app_key_plaintext_is_returned_once_and_only_its_digest_is_stored(
    tmp_path: Path,
) -> None:
    service, config = app_keys(tmp_path)
    created = create_key(
        service,
        app="review-station",
        access=(ApplicationAccess(CATALOG_READ), ApplicationAccess(RETRIEVAL_MANAGE)),
    )

    assert created["app"] == "review-station"
    assert created["access"] == [
        {"permission": CATALOG_READ, "resource": "*"},
        {"permission": RETRIEVAL_MANAGE, "resource": "*"},
    ]
    assert created["status"] == "active"
    assert str(created["token"]).startswith("rh_app_")
    factory = make_session_factory(config.database_url)
    with session_scope(factory) as session:
        record = session.get(AppKeyRecord, str(created["id"]))
        assert record is not None
        assert (
            record.token_sha256 == hashlib.sha256(str(created["token"]).encode("utf-8")).hexdigest()
        )
        assert not hasattr(record, "token")

    listed = service.list_keys(
        app="review-station",
        page_size=25,
        position=None,
        q=None,
        sort="created_at",
        order="desc",
    )
    assert listed["keys"] == [{key: created[key] for key in created if key != "token"}]


def test_action_specific_group_access_does_not_leak_between_operations(tmp_path: Path) -> None:
    service, config = app_keys(tmp_path)
    group_id = seed_group(config, "camera")
    created = create_key(
        service,
        app="reader",
        access=(ApplicationAccess(CATALOG_READ, f"group:{group_id}"),),
    )
    principal = service.authenticate(str(created["token"]))
    assert principal is not None
    assert principal.allows_group(CATALOG_READ, group_id)
    assert not principal.allows_group(RETRIEVAL_MANAGE, group_id)


def test_app_keys_rotate_revoke_expire_and_authenticate_without_restart(tmp_path: Path) -> None:
    service, config = app_keys(tmp_path)
    first = create_key(service, app="local", access=(ApplicationAccess(CATALOG_READ),))
    second = create_key(service, app="local", access=(ApplicationAccess(RETRIEVAL_MANAGE),))

    first_principal = service.authenticate(str(first["token"]))
    second_principal = service.authenticate(str(second["token"]))
    assert first_principal is not None and first_principal.allows(CATALOG_READ)
    assert second_principal is not None and second_principal.allows(RETRIEVAL_MANAGE)
    assert service.authenticate("wrong") is None

    revoked = service.revoke(app="local", key_id=str(first["id"]))
    assert revoked["status"] == "revoked"
    assert service.authenticate(str(first["token"])) is None
    factory = make_session_factory(config.database_url)
    with session_scope(factory) as session:
        record = session.get(AppKeyRecord, str(second["id"]))
        assert record is not None
        record.expires_at = "2000-01-01T00:00:00.000000Z"
    assert service.authenticate(str(second["token"])) is None


def test_key_browsing_crosses_nullable_sort_positions_without_omission(
    tmp_path: Path,
) -> None:
    service, config = app_keys(tmp_path)
    created = [create_key(service, app="nullable") for _ in range(3)]
    factory = make_session_factory(config.database_url)
    expiry_by_id = {
        str(created[0]["id"]): "2026-01-01T00:00:00.000000Z",
        str(created[1]["id"]): "2027-01-01T00:00:00.000000Z",
        str(created[2]["id"]): None,
    }
    with session_scope(factory) as session:
        for key_id, expires_at in expiry_by_id.items():
            record = session.get(AppKeyRecord, key_id)
            assert record is not None
            record.expires_at = expires_at

    def browse(order: str) -> list[str]:
        position: tuple[str | int | bool | bytes | None, ...] | None = None
        ids: list[str] = []
        while True:
            page = service.list_keys(
                app="nullable",
                page_size=1,
                position=position,
                q=None,
                sort="expires_at",
                order=order,
            )
            keys = page["keys"]
            assert isinstance(keys, list)
            for item in keys:
                assert isinstance(item, dict)
                ids.append(str(item["id"]))
            position = cast(
                tuple[str | int | bool | bytes | None, ...] | None,
                page["_next_position"],
            )
            if position is None:
                return ids

    ascending = sorted(
        expiry_by_id,
        key=lambda key_id: (
            expiry_by_id[key_id] is None,
            expiry_by_id[key_id] or "",
            key_id,
        ),
    )
    assert browse("asc") == ascending
    assert browse("desc") == list(reversed(ascending))


def test_key_delegation_cannot_exceed_permission_or_resource(tmp_path: Path) -> None:
    service, config = app_keys(tmp_path)
    collection_id = 1
    docs_group = seed_group(config, "docs", collection_id=collection_id)
    other_group = seed_group(config, "other")
    manager = ApplicationPrincipal(
        app="manager",
        key_id="manager-key",
        access=frozenset(
            {
                ApplicationAccess(KEYS_MANAGE),
                ApplicationAccess(CATALOG_READ, f"group:{docs_group}"),
            }
        ),
    )

    with pytest.raises(Forbidden, match="cannot grant"):
        create_key(
            service,
            app="uploader",
            access=(ApplicationAccess(COLLECTIONS_CREATE),),
            grantor=manager,
        )
    with pytest.raises(Forbidden, match="cannot grant"):
        create_key(
            service,
            app="other-reader",
            access=(ApplicationAccess(CATALOG_READ, f"group:{other_group}"),),
            grantor=manager,
        )


def test_rotation_preserves_access_and_access_lists_are_pipeable(tmp_path: Path) -> None:
    service, config = app_keys(tmp_path)
    photos_group = seed_group(config, "photos")
    created = create_key(
        service,
        app="review",
        access=(
            ApplicationAccess(CATALOG_READ, f"group:{photos_group}"),
            ApplicationAccess(RETRIEVAL_MANAGE, f"group:{photos_group}"),
        ),
    )
    factory = make_session_factory(config.database_url)
    with session_scope(factory) as session:
        record = session.get(AppKeyRecord, str(created["id"]))
        assert record is not None
        record.monthly_download_quota_bytes = 123

    rotated = service.rotate(app="review", key_id=str(created["id"]), grantor=BOOTSTRAP)
    assert rotated["id"] == created["id"]
    assert rotated["access"] == created["access"]
    assert rotated["monthly_download_quota_bytes"] == 123
    assert service.authenticate(str(created["token"])) is None

    listed = list(
        service.iter_access(
            app="review",
            key_id=str(created["id"]),
            q="retrieval",
            sort="permission",
            order="asc",
        )
    )
    assert len(listed) == 1
    assert listed[0]["app"] == "review"
    assert listed[0]["key_id"] == created["id"]
    assert listed[0]["permission"] == "retrieval:manage"
    assert listed[0]["resource"] == f"group:{photos_group}"


def test_access_targets_must_exist(tmp_path: Path) -> None:
    service, _config = app_keys(tmp_path)
    with pytest.raises(NotFound, match="collection access group not found"):
        create_key(
            service,
            app="reader",
            access=(ApplicationAccess(CATALOG_READ, f"group:{'f' * 64}"),),
        )


def test_revocation_cancels_key_jobs_and_releases_unused_download_reservations(
    tmp_path: Path,
) -> None:
    service, config = app_keys(tmp_path)
    created = create_key(service, app="review", access=(ApplicationAccess(RETRIEVAL_MANAGE),))
    factory = make_session_factory(config.database_url)
    with session_scope(factory) as session:
        session.add(
            CollectionRecord(
                id=1,
                creation_idempotency_key="revocation-fixture",
                creation_identity_sha256="e" * 64,
                creation_custody_mode="producer-retained",
                content_identity="0" * 64,
                encryption_format="age-v1-scrypt",
                passphrase_id="fixture-archive-key-v1",
                inventory_identity="1" * 64,
                created_by_app="fixture",
                created_at=str(created["created_at"]),
            )
        )
        session.add(
            CollectionFileRecord(collection_id=1, path="video.mp4", bytes=100, sha256="2" * 64)
        )
        session.add(
            RetrievalPlanRecord(
                id="plan-one",
                app="review",
                initiated_by_key_id=str(created["id"]),
                idempotency_key="plan-one",
                creation_identity_sha256="d" * 64,
                state="consumed",
                request_json='[{"collection_id":1,"path":"video.mp4"}]',
                lease_seconds=3600,
                restore_policy="allow",
                created_at=str(created["created_at"]),
                ready_at=None,
                expires_at="2026-08-01T00:00:00.000000Z",
                file_commitment_sha256="b" * 64,
                segment_commitment_sha256="c" * 64,
                etag="a" * 64,
            )
        )
        session.add(
            RetrievalPlanFileRecord(
                plan_id="plan-one",
                file_order=0,
                collection_id=1,
                path="video.mp4",
                bytes=100,
                sha256="2" * 64,
                source_store="fixture",
                requires_restore=False,
            )
        )
        session.add(
            RetrievalJobRecord(
                id="job-one",
                plan_id="plan-one",
                app="review",
                initiated_by_key_id=str(created["id"]),
                event_context_json=None,
                state="requested",
                plan_etag="a" * 64,
                lease_seconds=3600,
                created_at=str(created["created_at"]),
                requested_at=str(created["created_at"]),
                ready_at=None,
                expires_at=None,
                next_poll_at=str(created["created_at"]),
            )
        )
        session.add(
            KeyDownloadReservationRecord(
                id="job:job-one",
                key_id=str(created["id"]),
                job_id="job-one",
                kind="job",
                month_started_at="2026-07-01T00:00:00.000000Z",
                reserved_bytes=100,
                created_at=str(created["created_at"]),
                expires_at="2026-08-01T00:00:00.000000Z",
            )
        )
    service.revoke(app="review", key_id=str(created["id"]))
    with session_scope(factory) as session:
        job = session.get(RetrievalJobRecord, "job-one")
        assert job is not None and job.state == "canceled"
        assert session.get(KeyDownloadReservationRecord, "job:job-one") is None


def test_app_list_aggregates_and_filters_in_the_database(tmp_path: Path) -> None:
    service, _config = app_keys(tmp_path)
    alpha_first = create_key(service, app="alpha")
    create_key(service, app="alpha")
    create_key(service, app="beta")
    service.revoke(app="alpha", key_id=str(alpha_first["id"]))
    payload = service.list_apps(
        page_size=1,
        position=None,
        q="a",
        sort="keys",
        order="desc",
        active=True,
    )
    assert payload["_next_position"] is not None
    assert payload["apps"][0]["name"] == "alpha"
    assert payload["apps"][0]["active_keys"] == 1


def test_app_names_access_and_expiry_are_validated(tmp_path: Path) -> None:
    service, _config = app_keys(tmp_path)
    with pytest.raises(BadRequest, match="app name"):
        create_key(service, app="not/an/app")
    with pytest.raises(ApplicationAccessError, match="unknown application permission"):
        ApplicationAccess("unknown:permission")
    with pytest.raises(BadRequest, match="expiry"):
        create_key(service, app="local", expires_in=timedelta(0))


def test_application_authentication_distinguishes_missing_and_forbidden_permissions() -> None:
    principal = ApplicationPrincipal(
        app="local",
        key_id="key",
        access=frozenset({ApplicationAccess(CATALOG_READ)}),
    )

    class Keys:
        def authenticate(self, token: str) -> ApplicationPrincipal | None:
            return principal if token == "valid" else None

    container = SimpleNamespace(app_keys=Keys())
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="valid")
    assert require_application(credentials, container) == principal
    assert require_permission(CATALOG_READ)(credentials, container) == principal
    with pytest.raises(Unauthorized, match="invalid application token"):
        require_application(None, container)
    with pytest.raises(Forbidden, match="application permission required"):
        require_permission(COLLECTIONS_CREATE)(credentials, container)
