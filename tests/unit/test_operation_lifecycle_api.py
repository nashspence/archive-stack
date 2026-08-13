from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from fastapi.testclient import TestClient
from riverhog_api.app import create_app
from riverhog_api.deps import ServiceContainer
from riverhog_api_client.client import ApiClient
from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import initialize_db, make_session_factory
from riverhog_core.collection_access import SqlAlchemyCollectionAccessService
from riverhog_core.proofs import ProofUpgradeResult
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.app_keys import SqlAlchemyAppKeyService
from riverhog_core.services.archive_attestations import SqlAlchemyArchiveAttestationService
from riverhog_core.services.archive_copies import SqlAlchemyArchiveCopyService
from riverhog_core.services.archive_copy_retirements import (
    SqlAlchemyArchiveCopyRetirementService,
)
from riverhog_core.services.archive_maintenance import SqlAlchemyArchiveMaintenanceService
from riverhog_core.services.archive_stores import SqlAlchemyArchiveStoreService
from riverhog_core.services.collection_deletions import SqlAlchemyCollectionDeletionService
from riverhog_core.services.collection_uploads import SqlAlchemyCollectionUploadService
from riverhog_core.services.collections import SqlAlchemyCollectionService
from riverhog_core.services.download_allowances import SqlAlchemyDownloadAllowance
from riverhog_core.services.lifecycle_events import SqlAlchemyLifecycleEventService
from riverhog_core.services.proof_maturations import SqlAlchemyProofMaturationService
from riverhog_core.services.provenance import SqlAlchemyProvenanceService
from riverhog_core.services.retrieval import SqlAlchemyRetrievalService
from riverhog_core.services.search import SqlAlchemySearchService
from riverhog_core.services.tags import SqlAlchemyTagService
from riverhog_protocol.manifest import collection_content_etag
from riverhog_provenance import (
    FileProvenanceBinding,
    build_provenance_archive,
    create_observation_journal,
    validate_journal,
)

from tests.fixtures.crypto import FixtureProofStamper, FixtureProofVerifier
from tests.operation_observer import OperationObserver, TimeoutNeutralTestClient
from tests.unit.archive_object_fixtures import MemoryArchiveStore, archive_store_binding
from tests.unit.db_helpers import sqlite_url


@dataclass(frozen=True, slots=True)
class _FixtureProofUpgrader:
    def upgrade(self, proof_bytes: bytes) -> ProofUpgradeResult:
        return ProofUpgradeResult(proof_bytes=proof_bytes, complete=True)


def _container(tmp_path: Path) -> ServiceContainer:
    database_url = sqlite_url(tmp_path / "catalog.sqlite3")
    baseline = RuntimeConfig(database_url=database_url, archive_scrypt_work_factor=1)
    primary_config = replace(
        baseline.archive_store("archive"),
        name="primary",
        endpoint_url="http://127.0.0.1:9001",
        bucket="primary",
        prefix="primary",
    )
    secondary_config = replace(
        baseline.archive_store("archive"),
        name="secondary",
        endpoint_url="http://127.0.0.1:9002",
        bucket="secondary",
        prefix="secondary",
    )
    config = replace(
        baseline,
        archive_write_store="primary",
        archive_read_order=("primary", "secondary"),
        archive_stores={"primary": primary_config, "secondary": secondary_config},
    )
    initialize_db(database_url)
    session_factory = make_session_factory(database_url)
    stores = ArchiveStoreRegistry(
        {
            "primary": archive_store_binding(MemoryArchiveStore()),
            "secondary": archive_store_binding(MemoryArchiveStore()),
        }
    )
    stamper = FixtureProofStamper()
    verifier = FixtureProofVerifier()
    upgrader = _FixtureProofUpgrader()
    allowances = SqlAlchemyDownloadAllowance(config, session_factory=session_factory)
    return ServiceContainer(
        app_keys=SqlAlchemyAppKeyService(config, session_factory=session_factory),
        collection_access=SqlAlchemyCollectionAccessService(
            config, session_factory=session_factory
        ),
        tags=SqlAlchemyTagService(config, session_factory=session_factory),
        collections=SqlAlchemyCollectionService(config, session_factory=session_factory),
        collection_uploads=SqlAlchemyCollectionUploadService(
            config,
            stores,
            proof_stamper=stamper,
            session_factory=session_factory,
        ),
        provenance=SqlAlchemyProvenanceService(config, session_factory=session_factory),
        collection_deletions=SqlAlchemyCollectionDeletionService(
            config,
            stores,
            None,
            session_factory=session_factory,
        ),
        search=SqlAlchemySearchService(config, session_factory=session_factory),
        archive_maintenance=SqlAlchemyArchiveMaintenanceService(
            config,
            stores,
            session_factory=session_factory,
        ),
        archive_copies=SqlAlchemyArchiveCopyService(
            config,
            stores,
            session_factory=session_factory,
        ),
        proof_maturations=SqlAlchemyProofMaturationService(
            config,
            stores,
            proof_upgrader=upgrader,
            proof_verifier=verifier,
            session_factory=session_factory,
        ),
        archive_attestations=SqlAlchemyArchiveAttestationService(
            config,
            stores,
            proof_stamper=stamper,
            proof_upgrader=upgrader,
            proof_verifier=verifier,
            session_factory=session_factory,
        ),
        archive_copy_retirements=SqlAlchemyArchiveCopyRetirementService(
            config,
            stores,
            session_factory=session_factory,
        ),
        archive_stores=SqlAlchemyArchiveStoreService(
            config,
            download_allowance=allowances,
            session_factory=session_factory,
        ),
        retrieval=SqlAlchemyRetrievalService(
            config,
            stores,
            None,
            download_allowance=allowances,
            session_factory=session_factory,
        ),
        lifecycle_events=SqlAlchemyLifecycleEventService(
            config,
            session_factory=session_factory,
        ),
        download_quotas=allowances,
        session_factory=session_factory,
    )


def _api(
    test_client: TestClient,
    token: str,
    *,
    observer: OperationObserver | None = None,
) -> ApiClient:
    api = ApiClient(
        base_url="http://testserver",
        token=token,
        allow_insecure_http=True,
    )
    bound = TimeoutNeutralTestClient(
        TestClient(test_client.app, headers={"Authorization": f"Bearer {token}"}),
        observer=observer,
    )
    api._request_client = bound  # type: ignore[assignment]
    api._download_client = bound  # type: ignore[assignment]
    return api


def _unit_content(root: Path, unit: dict[str, Any]) -> bytes:
    content = bytearray()
    for source in unit["sources"]:
        payload = (root / source["path"]).read_bytes()
        start = int(source["offset"])
        content.extend(payload[start : start + int(source["bytes"])])
    assert len(content) == unit["payload_bytes"]
    return bytes(content)


def test_riverhog_official_client_positive_disposable_lifecycle(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("RIVERHOG_BOOTSTRAP_TOKEN", "qualification-bootstrap")
    container = _container(tmp_path)
    application = create_app(container=container)
    observer = OperationObserver.install(application, application="riverhog")
    transport = TestClient(application)
    bootstrap = _api(transport, "qualification-bootstrap", observer=observer)

    operator_key = bootstrap.create_app_key(
        "qualification-operator",
        access=[{"permission": "*", "resource": "*"}],
    )
    bootstrap.set_app_key_download_quota(
        "qualification-operator",
        str(operator_key["id"]),
        monthly_bytes=1024 * 1024,
    )
    operator_token = str(operator_key["token"])
    operator_headers = {"Authorization": f"Bearer {operator_token}"}
    operator = _api(transport, operator_token, observer=observer)

    assert transport.get("/health/live").json() == {"service": "riverhog", "status": "ok"}
    assert transport.get("/health/ready").json() == {"service": "riverhog", "status": "ok"}

    delegated = operator.create_app_key(
        "qualification-reader",
        access=[{"permission": "catalog:read", "resource": "*"}],
    )
    delegated_id = str(delegated["id"])
    assert operator.list_apps(q="qualification", all_items=True)["total"] == 2
    assert operator.list_app_keys("qualification-reader", all_items=True)["total"] == 1
    assert operator.list_app_key_access(key_id=delegated_id, all_items=True)["total"] == 1
    operator.replace_app_key_access(
        "qualification-reader",
        delegated_id,
        access=[{"permission": "catalog:read", "resource": "*"}],
    )
    operator.add_app_key_access(
        "qualification-reader",
        delegated_id,
        permission="events:read",
        resource="*",
    )
    operator.remove_app_key_access(
        "qualification-reader",
        delegated_id,
        permission="events:read",
        resource="*",
    )
    operator.set_app_key_download_quota(
        "qualification-reader",
        delegated_id,
        monthly_bytes=1024,
    )
    assert (
        operator.list_download_quotas(app="qualification-reader", all_items=True)["quotas"][0][
            "monthly_bytes"
        ]
        == 1024
    )
    assert operator.get_download_quota()["app"] == "qualification-operator"
    rotated = operator.rotate_app_key("qualification-reader", delegated_id)
    operator.revoke_app_key("qualification-reader", str(rotated["id"]))

    operator.create_tag("docs")
    operator.create_tag("temporary")
    assert operator.get_tag("docs")["id"] == "docs"
    assert operator.list_tags(q="doc", all_items=True)["total"] == 1
    tag_plan = operator.plan_tag_deletion("temporary")
    operator.delete_tag("temporary", challenge=str(tag_plan["challenge"]))

    source_root = tmp_path / "source"
    source_root.mkdir()
    source = source_root / "document.txt"
    source.write_bytes(b"qualified archive content\n")
    journal = create_observation_journal(
        source,
        relative_path="document.txt",
        host_id="urn:uuid:00000000-0000-4000-8000-000000000469",
        agent_name="riverhog-operation-qualification",
        agent_version="1.0.0",
    )
    journal_summary = validate_journal(journal)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    binding = FileProvenanceBinding(
        path="document.txt",
        bytes=source.stat().st_size,
        sha256=digest,
        status="captured",
        journal_id=journal_summary.journal_id,
        current_state_id=journal_summary.current_state_id,
    )
    provenance = build_provenance_archive(
        bindings=(binding,),
        journals={journal_summary.journal_id: journal},
    )
    opened = operator.create_or_resume_collection_upload_session(
        "qualification-upload",
        ["docs"],
        ingest_source="disposable-test",
        archive_store="primary",
    )
    collection_id = int(opened["collection_id"])
    assert operator.list_collection_upload_sessions(all_items=True)["total"] == 1
    operator.put_collection_upload_session_provenance_journal(
        collection_id,
        journal_summary.journal_id,
        content=journal,
        sha256=journal_summary.journal_sha256,
    )
    operator.register_collection_upload_session_files(
        collection_id,
        [
            {
                "path": binding.path,
                "bytes": binding.bytes,
                "sha256": binding.sha256,
                "provenance": {
                    "status": "captured",
                    "journal_id": binding.journal_id,
                    "current_state_id": binding.current_state_id,
                },
            }
        ],
    )
    assert (
        operator.list_collection_upload_session_files(collection_id, all_items=True)["total"] == 1
    )
    operator.complete_collection_upload_session(
        collection_id,
        files_total=1,
        content_etag=collection_content_etag([(binding.path, binding.bytes, binding.sha256)]),
        provenance_etag=provenance.identity,
    )
    volume_page = operator.list_collection_upload_session_volumes(collection_id)
    for volume in volume_page["volumes"]:
        shown = operator.get_collection_upload_session_volume(
            collection_id, str(volume["volume_id"])
        )
        for unit in shown["units"]:
            current = operator.get_collection_upload_session_unit(
                collection_id,
                str(volume["volume_id"]),
                int(unit["unit"]),
            )
            operator.put_collection_upload_session_unit(
                collection_id,
                str(volume["volume_id"]),
                int(unit["unit"]),
                plan_sha256=str(volume["plan_sha256"]),
                content=_unit_content(source_root, current),
            )
    assert container.collection_uploads.process_due_finalizations() == 1
    assert operator.get_collection_upload_session(collection_id)["state"] == "finalized"
    assert operator.get_collection(collection_id)["id"] == collection_id
    assert operator.list_collections(tag="docs", all_items=True)["total"] == 1
    assert operator.search("document", collection=str(collection_id), all_items=True)["total"] == 1
    assert operator.get_collection_tags(collection_id)["tags"] == ["docs"]
    operator.create_tag("reviewed")
    operator.add_collection_tag(collection_id, "reviewed")
    operator.remove_collection_tag(collection_id, "docs")
    operator.replace_collection_tags(collection_id, ["docs", "reviewed"])

    assert operator.list_collection_provenance(collection_id, all_items=True)["total"] == 1
    assert (
        operator.get_collection_file_provenance(collection_id, "document.txt")["journal"][
            "journal_id"
        ]
        == journal_summary.journal_id
    )
    assert operator.trace_collection_file_provenance(collection_id, "document.txt")["journals"]
    assert (
        operator.export_collection_provenance_journal(collection_id, journal_summary.journal_id)
        == journal
    )
    assert operator.verify_collection_provenance(collection_id)["valid"] is True

    resourcesync_documents = [
        transport.get(path, headers=operator_headers)
        for path in (
            "/.well-known/resourcesync",
            "/resourcesync/capabilitylist.xml",
            "/resourcesync/resourcelist.xml",
            "/resourcesync/resourcelist/1.xml",
            "/resourcesync/changelist.xml",
        )
    ]
    assert all(response.status_code == 200 for response in resourcesync_documents)
    roots = [ElementTree.fromstring(response.content) for response in resourcesync_documents]
    assert [root.tag.rsplit("}", 1)[-1] for root in roots] == [
        "urlset",
        "urlset",
        "sitemapindex",
        "urlset",
        "urlset",
    ]
    referenced_locations = [
        str(node.text) for node in roots[3].iter() if node.tag.rsplit("}", 1)[-1] == "loc"
    ]
    assert referenced_locations == [
        f"http://testserver/v1/catalog/collections/{collection_id}/manifest"
    ]
    assert int(roots[4].attrib["data-cursor"]) > 0
    manifest = transport.get(
        f"/v1/catalog/collections/{collection_id}/manifest",
        headers=operator_headers,
    )
    assert manifest.status_code == 200
    assert manifest.json()["collection"] == collection_id

    stores = operator.list_archive_stores(all_items=True)
    assert {item["store"] for item in stores["stores"]} == {"primary", "secondary"}
    assert operator.get_archive_store("primary")["store"] == "primary"
    assert operator.retrieval_cache_status()["configured"] is False
    assert operator.list_retrieval_cache_objects(all_items=True)["total"] == 0

    plan = operator.plan_retrieval([(collection_id, "document.txt")], restore_policy="never")
    job = operator.create_retrieval_job(
        [(collection_id, "document.txt")],
        plan_etag=str(plan["etag"]),
        restore_policy="never",
    )
    job_id = str(job["id"])
    assert operator.get_retrieval_job(job_id)["state"] == "ready"
    assert operator.renew_retrieval_job(job_id, lease_seconds=3600)["state"] == "ready"
    head = transport.head(
        f"/v1/retrieval-jobs/{job_id}/content",
        params={"collection_id": collection_id, "path": "document.txt"},
        headers=operator_headers,
    )
    assert head.status_code == 200
    assert head.headers["etag"] == f'"{binding.sha256}"'
    output = tmp_path / "retrieved.txt"
    operator.download_retrieval_file(
        job_id,
        collection_id=collection_id,
        path="document.txt",
        output=output,
        expected_bytes=binding.bytes,
        expected_sha256=binding.sha256,
    )
    assert output.read_bytes() == source.read_bytes()
    assert operator.acknowledge_retrieval_job(job_id)["state"] == "completed"
    cancel_plan = operator.plan_retrieval([(collection_id, "document.txt")], restore_policy="never")
    cancel_job = operator.create_retrieval_job(
        [(collection_id, "document.txt")],
        plan_etag=str(cancel_plan["etag"]),
        restore_policy="never",
    )
    assert operator.cancel_retrieval_job(str(cancel_job["id"]))["state"] == "canceled"

    canceled_upload = operator.create_or_resume_collection_upload_session(
        "qualification-canceled-upload",
        ["docs"],
        provenance_mode="omitted",
        provenance_omission_reason="qualification cancellation",
    )
    assert (
        operator.cancel_collection_upload_session(int(canceled_upload["collection_id"]))["state"]
        == "canceled"
    )

    copy = operator.create_or_resume_archive_copy(
        collection_id,
        destination_store="secondary",
        source_store="primary",
    )
    assert (
        operator.get_archive_copy_job(collection_id, destination_store="secondary")["state"]
        == "requested"
    )
    assert operator.list_archive_copy_jobs(all_items=True)["total"] == 1
    assert (
        operator.cancel_archive_copy_job(collection_id, destination_store="secondary")["state"]
        == "canceled"
    )
    assert copy["destination_store"] == "secondary"

    events = operator.list_lifecycle_events(limit=100)
    assert events.events
    assert int(events.next_cursor) > 0
    resumed_events = operator.list_lifecycle_events(after=events.next_cursor, limit=100)
    assert resumed_events.events == []
    assert resumed_events.next_cursor == events.next_cursor
    restarted_container = _container(tmp_path)
    restarted_transport = TestClient(create_app(container=restarted_container))
    restarted_operator = _api(restarted_transport, operator_token)
    restarted_events = restarted_operator.list_lifecycle_events(
        after=events.next_cursor,
        limit=100,
    )
    assert restarted_events.events == []
    assert restarted_events.next_cursor == events.next_cursor
    restarted_transport.close()
    restarted_container.close()
    deletion = operator.plan_collection_deletion(collection_id)
    deleted = operator.delete_collection(
        collection_id,
        challenge=str(deletion["challenge"]),
    )
    assert deleted["status"] == "deleted"

    from scripts.operation_qualification import operation_matrix

    observer.require(
        operation.operation_id
        for operation in operation_matrix()
        if operation.application == "riverhog" and operation.provider_evidence is None
    )

    transport.close()
    container.close()
