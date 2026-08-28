from __future__ import annotations

import hashlib
import os
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any
from uuid import uuid4

import pytest
from riverhog_core.app_permissions import (
    ALL_RESOURCES,
    COLLECTIONS_CREATE,
    COLLECTIONS_DELETE,
    ApplicationAccess,
    ApplicationPrincipal,
)
from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import (
    create_catalog_engine,
    initialize_db,
    make_session_factory,
    session_scope,
)
from riverhog_core.catalog_models import (
    CollectionUploadFileRecord,
    CollectionUploadRecord,
    TagRecord,
)
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.collection_uploads import SqlAlchemyCollectionUploadService
from riverhog_protocol.errors import Conflict
from riverhog_protocol.manifest import collection_content_identity_ordered
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url

from tests.fixtures.crypto import FixtureProofStamper
from tests.unit.archive_object_fixtures import MemoryArchiveStore, archive_store_binding

pytestmark = pytest.mark.integration

_CREATOR = ApplicationPrincipal(
    app="fixture-target",
    key_id="target-key",
    access=frozenset({ApplicationAccess(COLLECTIONS_CREATE, ALL_RESOURCES)}),
)
_OPERATOR = ApplicationPrincipal(
    app="fixture-operator",
    key_id="operator-key",
    access=frozenset({ApplicationAccess(COLLECTIONS_DELETE, ALL_RESOURCES)}),
)
_FILE = {
    "path": "output/artifact.bin",
    "bytes": 0,
    "sha256": hashlib.sha256(b"").hexdigest(),
}


@pytest.fixture
def database_url() -> Iterator[str]:
    value = os.environ.get("RIVERHOG_TEST_POSTGRES_URL", "").strip()
    if not value:
        pytest.skip("RIVERHOG_TEST_POSTGRES_URL is required")
    schema = "riverhog_upload_custody_" + uuid4().hex
    admin = create_catalog_engine(value)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    scoped = (
        make_url(value)
        .update_query_dict({"options": f"-csearch_path={schema}"})
        .render_as_string(hide_password=False)
    )
    initialize_db(scoped)
    with session_scope(make_session_factory(scoped)) as session:
        session.add(
            TagRecord(
                id="derived",
                created_by_app="fixture",
                created_at="2026-08-25T00:00:00.000000Z",
            )
        )
    try:
        yield scoped
    finally:
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def _services(
    database_url: str,
) -> tuple[SqlAlchemyCollectionUploadService, SqlAlchemyCollectionUploadService]:
    base = RuntimeConfig(database_url=database_url, archive_scrypt_work_factor=1)
    archive = replace(base.archive_store("archive"), name="archive")
    config = replace(
        base,
        archive_stores={"archive": archive},
        archive_write_store="archive",
        archive_read_order=("archive",),
    )
    stores = ArchiveStoreRegistry({"archive": archive_store_binding(MemoryArchiveStore())})
    return (
        SqlAlchemyCollectionUploadService(
            config,
            stores,
            proof_stamper=FixtureProofStamper(),
        ),
        SqlAlchemyCollectionUploadService(
            config,
            stores,
            proof_stamper=FixtureProofStamper(),
        ),
    )


def _create(service: SqlAlchemyCollectionUploadService) -> int:
    payload = service.create_or_resume(
        idempotency_key="fixture-execution",
        tags=("derived",),
        ingest_source="transform:fixture",
        archive_store=None,
        initiator=_CREATOR,
        event_context=None,
        provenance_mode="omitted",
        provenance_omission_reason="fixture",
        custody_mode="custody-transfer",
    )
    return int(payload["collection_id"])


def _expire(database_url: str, collection_id: int) -> None:
    with session_scope(make_session_factory(database_url)) as session:
        upload = session.get(CollectionUploadRecord, collection_id)
        assert upload is not None
        upload.lease_expires_at = "2000-01-01T00:00:00.000000Z"


def _race(
    first: Callable[[], Any],
    second: Callable[[], Any],
) -> tuple[Any, Any]:
    barrier = threading.Barrier(2)

    def invoke(call: Callable[[], Any]) -> Any:
        barrier.wait(timeout=10)
        try:
            return call()
        except Exception as exc:  # return exact competing outcome for assertions
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (executor.submit(invoke, first), executor.submit(invoke, second))
        return futures[0].result(timeout=20), futures[1].result(timeout=20)


def test_exact_concurrent_registration_is_one_append_only_planner_step(
    database_url: str,
) -> None:
    first, second = _services(database_url)
    collection_id = _create(first)

    results = _race(
        lambda: first.register_files(collection_id, (_FILE,)),
        lambda: second.register_files(collection_id, (_FILE,)),
    )

    assert all(not isinstance(result, Exception) for result in results)
    with session_scope(make_session_factory(database_url)) as session:
        upload = session.get(CollectionUploadRecord, collection_id)
        assert upload is not None
        assert (
            session.scalar(
                select(func.count(CollectionUploadFileRecord.path)).where(
                    CollectionUploadFileRecord.collection_id == collection_id
                )
            )
            == 1
        )
        assert upload.planner_checkpoint_json is not None
        assert '"next_file_order":1' in upload.planner_checkpoint_json


def test_heartbeat_and_expiry_serialize_without_losing_resumable_custody(
    database_url: str,
) -> None:
    first, second = _services(database_url)
    collection_id = _create(first)
    first.register_files(collection_id, (_FILE,))
    _expire(database_url, collection_id)

    heartbeat, reaped = _race(
        lambda: first.heartbeat(collection_id),
        lambda: second.reap_expired_custody_transfers(),
    )
    state = str(first.get(collection_id)["state"])

    assert state in {"open", "orphaned"}
    if state == "open":
        assert not isinstance(heartbeat, Exception)
        assert reaped == 0
    else:
        assert isinstance(heartbeat, Conflict)
        assert reaped == 1
        resumed = first.create_or_resume(
            idempotency_key="fixture-execution",
            tags=("derived",),
            ingest_source="transform:fixture",
            archive_store=None,
            initiator=_CREATOR,
            event_context=None,
            provenance_mode="omitted",
            provenance_omission_reason="fixture",
            custody_mode="custody-transfer",
        )
        assert resumed["collection_id"] == collection_id
        assert resumed["state"] == "open"
    assert first.list_files(collection_id, page=1, per_page=100)["total"] == 1


def test_completion_and_expiry_have_one_serial_terminal_intent(
    database_url: str,
) -> None:
    first, second = _services(database_url)
    collection_id = _create(first)
    first.register_files(collection_id, (_FILE,))
    _expire(database_url, collection_id)
    identity = collection_content_identity_ordered(
        ((_FILE["path"], _FILE["bytes"], _FILE["sha256"]),)  # type: ignore[arg-type]
    )

    completed, reaped = _race(
        lambda: first.complete(collection_id, files_total=1, content_identity=identity),
        lambda: second.reap_expired_custody_transfers(),
    )
    state = str(first.get(collection_id)["state"])

    if state == "orphaned":
        assert isinstance(completed, Conflict)
        assert reaped == 1
        _create(first)
        resumed = first.complete(collection_id, files_total=1, content_identity=identity)
        assert resumed["state"] in {"closing", "uploading", "finalizing", "finalized"}
    else:
        assert not isinstance(completed, Exception)
        assert reaped == 0
        assert state in {"closing", "uploading", "finalizing", "finalized"}


def test_guarded_discard_and_exact_resume_cannot_both_win_for_old_custody(
    database_url: str,
) -> None:
    first, second = _services(database_url)
    collection_id = _create(first)
    first.register_files(collection_id, (_FILE,))
    _expire(database_url, collection_id)
    assert first.reap_expired_custody_transfers() == 1
    plan = first.plan_orphan_discard(collection_id)
    challenge = str(plan["challenge"])

    resumed, discarded = _race(
        lambda: _create(first),
        lambda: second.discard_orphan(collection_id, challenge=challenge),
    )

    if isinstance(discarded, Conflict):
        assert resumed == collection_id
        assert first.get(collection_id)["state"] == "open"
    else:
        assert discarded["status"] == "discarded"
        assert isinstance(resumed, Conflict)
        replacement = _create(first)
        assert replacement != collection_id
        assert first.get(replacement)["state"] == "open"
