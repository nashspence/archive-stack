from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from xml.etree import ElementTree

import pytest
from fastapi.testclient import TestClient
from riverhog_api.app import create_app
from riverhog_api.deps import ServiceContainer
from riverhog_api_client.client import ApiClient
from riverhog_core.archive_store_registry import ArchiveStoreRegistry
from riverhog_core.catalog_db import initialize_db, make_session_factory, session_scope
from riverhog_core.catalog_models import CollectionUploadRecord
from riverhog_core.collection_access import SqlAlchemyCollectionAccessService
from riverhog_core.runtime_config import RuntimeConfig
from riverhog_core.services.app_keys import SqlAlchemyAppKeyService
from riverhog_core.services.archive_copies import SqlAlchemyArchiveCopyService
from riverhog_core.services.archive_copy_retirements import (
    SqlAlchemyArchiveCopyRetirementService,
)
from riverhog_core.services.archive_maintenance import SqlAlchemyArchiveMaintenanceService
from riverhog_core.services.archive_stores import SqlAlchemyArchiveStoreService
from riverhog_core.services.collection_deletions import SqlAlchemyCollectionDeletionService
from riverhog_core.services.collection_uploads import SqlAlchemyCollectionUploadService
from riverhog_core.services.collection_workflows import (
    SqlAlchemyCollectionWorkflowService,
)
from riverhog_core.services.collections import SqlAlchemyCollectionService
from riverhog_core.services.download_allowances import SqlAlchemyDownloadAllowance
from riverhog_core.services.lifecycle_events import SqlAlchemyLifecycleEventService
from riverhog_core.services.provenance import SqlAlchemyProvenanceService
from riverhog_core.services.retrieval import SqlAlchemyRetrievalService
from riverhog_core.services.search import SqlAlchemySearchService
from riverhog_core.services.tags import SqlAlchemyTagService
from riverhog_protocol import CollectionUploadUnitWorkDocument
from riverhog_protocol.collection_upload_transport import collection_upload_path_order_key
from riverhog_protocol.collection_workflows import (
    DERIVATION_EVIDENCE_PATH,
    PRODUCER_EVIDENCE_PATH,
    ArtifactDisposition,
    ArtifactDispositionOutput,
    ArtifactDispositionSetIdentity,
    CollectionDerivation,
    CollectionRootIdentity,
    OperationIdentity,
    ProducerEvidence,
    RecipeIdentity,
    canonical_json_sha256,
)
from riverhog_protocol.errors import Forbidden
from riverhog_protocol.manifest import collection_content_identity
from riverhog_provenance import (
    FileProvenanceBinding,
    create_derivative_journal_from_identity,
    create_observation_journal,
    validate_journal,
)

from tests.operation_observer import OperationObserver, TimeoutNeutralTestClient
from tests.provenance_observer import native_provenance_observer
from tests.unit.archive_object_fixtures import MemoryArchiveStore, archive_store_binding
from tests.unit.db_helpers import sqlite_url


def _container(tmp_path: Path) -> ServiceContainer:
    database_url = sqlite_url(tmp_path / "catalog.sqlite3")
    baseline = RuntimeConfig(database_url=database_url, archive_scrypt_work_factor=1)
    primary_config = replace(
        baseline.archive_store("archive"),
        name="primary",
        base_url="http://127.0.0.1:9001",
    )
    secondary_config = replace(
        baseline.archive_store("archive"),
        name="secondary",
        base_url="http://127.0.0.1:9002",
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
            session_factory=session_factory,
        ),
        collection_workflows=SqlAlchemyCollectionWorkflowService(
            config, session_factory=session_factory
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
        archive_copy_retirements=SqlAlchemyArchiveCopyRetirementService(
            config,
            stores,
            session_factory=session_factory,
        ),
        archive_stores=SqlAlchemyArchiveStoreService(
            config,
            stores,
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


def _unit_content(root: Path, unit: CollectionUploadUnitWorkDocument) -> bytes:
    content = bytearray()
    for source in unit.sources:
        payload = (root / source.path).read_bytes()
        content.extend(payload[source.offset : source.offset + source.bytes])
    assert len(content) == unit.payload_bytes
    return bytes(content)


def _finalize_upload(
    container: ServiceContainer,
    api: ApiClient,
    collection_id: int,
) -> None:
    for _ in range(256):
        if api.get_collection_upload_session(collection_id)["state"] == "finalized":
            return
        assert container.collection_uploads.process_due_finalizations() == 1
    raise AssertionError("bounded collection finalization did not terminate")


def _complete_provenance_verification(
    container: ServiceContainer,
    api: ApiClient,
    collection_id: int,
) -> dict[str, object]:
    for _ in range(256):
        current = api.get_collection_provenance_verification(collection_id)
        if current["state"] == "succeeded":
            return current
        if current["state"] in {"failed", "canceled"}:
            raise AssertionError(f"provenance verification terminated unsuccessfully: {current}")
        assert container.provenance.process_due_verifications() == 1
    raise AssertionError("bounded provenance verification did not terminate")


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

    missing_archive_source = transport.post(
        "/v1/archive/copies",
        headers=operator_headers,
        json={"collection_id": 999, "destination_store": "secondary"},
    )
    assert missing_archive_source.status_code == 404
    assert missing_archive_source.json()["error"]["code"] == "not_found"
    archive_copy_errors = application.openapi()["paths"]["/v1/archive/copies"]["post"]["responses"]
    assert "not_found" in archive_copy_errors["404"]["x-riverhog-error-codes"]
    provenance_verify_errors = application.openapi()["paths"][
        "/v1/collections/{collection_id}/provenance/verification"
    ]["post"]["responses"]
    assert "conflict" not in {
        code
        for response in provenance_verify_errors.values()
        for code in response.get("x-riverhog-error-codes", [])
    }

    assert transport.get("/health/live").json() == {"service": "riverhog", "status": "ok"}
    assert transport.get("/health/ready").json() == {"service": "riverhog", "status": "ok"}

    delegated = operator.create_app_key(
        "qualification-reader",
        access=[{"permission": "catalog:read", "resource": "*"}],
    )
    delegated_id = str(delegated["id"])
    assert len(operator.list_apps(q="qualification", page_size=100, page_token=None)["apps"]) == 2
    assert (
        len(operator.list_app_keys("qualification-reader", page_size=100, page_token=None)["keys"])
        == 1
    )
    assert (
        len(
            operator.list_app_key_access(key_id=delegated_id, page_size=100, page_token=None)[
                "access"
            ]
        )
        == 1
    )
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
        operator.list_download_quotas(app="qualification-reader", page_size=100, page_token=None)[
            "quotas"
        ][0]["monthly_bytes"]
        == 1024
    )
    assert operator.get_download_quota()["app"] == "qualification-operator"
    rotated = operator.rotate_app_key("qualification-reader", delegated_id)
    operator.revoke_app_key("qualification-reader", str(rotated["id"]))

    operator.create_tag("docs")
    operator.create_tag("temporary")
    assert operator.get_tag("docs")["id"] == "docs"
    assert len(operator.list_tags(q="doc", page_size=100, page_token=None)["tags"]) == 1
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
        observer=native_provenance_observer(),
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
    opened = operator.create_or_resume_collection_upload_session(
        "qualification-upload",
        ["docs"],
        ingest_source="disposable-test",
        archive_store="primary",
    )
    assert opened["resumed"] is False
    collection_id = int(opened["collection_id"])
    operator.create_tag("staged")
    operator.add_collection_upload_session_tag(collection_id, "staged")
    operator.remove_collection_upload_session_tag(collection_id, "staged")
    assert (
        len(operator.list_collection_upload_sessions(page_size=100, page_token=None)["uploads"])
        == 1
    )
    staged = operator.upload_collection_upload_session_provenance_journal(
        collection_id,
        journal_summary.journal_id,
        content=(journal,),
        byte_count=len(journal),
        sha256=journal_summary.journal_sha256,
    )
    while staged.state != "sealed":
        assert staged.state == "validating"
        assert container.collection_uploads.process_due_provenance_journal_validations() == 1
        staged = operator.get_collection_upload_session_provenance_journal(
            collection_id, journal_summary.journal_id
        )
    assert staged.accepted_bytes == len(journal)
    assert staged.sha256 == journal_summary.journal_sha256
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
        registration_constraints=opened["registration_constraints"],
    )
    assert (
        len(
            operator.list_collection_upload_session_files(
                collection_id, page_size=100, page_token=None
            )["files"]
        )
        == 1
    )
    operator.complete_collection_upload_session(
        collection_id,
        files_total=1,
        content_identity=collection_content_identity(
            [(binding.path, binding.bytes, binding.sha256)]
        ),
    )
    while work := operator.acquire_collection_upload_session_work(collection_id).work:
        for assignment in work:
            operator.put_collection_upload_session_unit(
                collection_id,
                assignment.volume.volume_id,
                assignment.unit.unit,
                plan_sha256=assignment.plan_sha256,
                content=_unit_content(source_root, assignment.unit),
            )
    assert (
        len(
            operator.list_collection_upload_session_files(
                collection_id, page_size=100, page_token=None
            )["files"]
        )
        == 1
    )
    _finalize_upload(container, operator, collection_id)
    assert operator.get_collection_upload_session(collection_id)["state"] == "finalized"
    assert operator.get_collection(collection_id)["id"] == collection_id
    assert (
        len(operator.get_collection_tags(collection_id, page_size=100, page_token=None)["tags"])
        == 1
    )
    assert (
        len(
            operator.list_collection_archive_copies(collection_id, page_size=100, page_token=None)[
                "copies"
            ]
        )
        == 1
    )
    assert (
        len(operator.list_collections(tag="docs", page_size=100, page_token=None)["collections"])
        == 1
    )
    assert (
        len(
            operator.search("document", collection=collection_id, page_size=100, page_token=None)[
                "files"
            ]
        )
        == 1
    )
    assert operator.get_collection_tags(collection_id, page_size=100, page_token=None)["tags"] == [
        "docs"
    ]
    operator.create_tag("reviewed")
    operator.add_collection_tag(collection_id, "reviewed")
    operator.remove_collection_tag(collection_id, "docs")
    operator.replace_collection_tags(collection_id, ["docs", "reviewed"])

    assert (
        len(
            operator.list_collection_provenance(collection_id, page_size=100, page_token=None)[
                "files"
            ]
        )
        == 1
    )
    assert (
        operator.get_collection_file_provenance(collection_id, "document.txt")["journal"][
            "journal_id"
        ]
        == journal_summary.journal_id
    )
    assert operator.trace_collection_file_provenance(collection_id, "document.txt")["items"]
    assert (
        len(
            operator.list_collection_provenance_journal_agents(
                collection_id,
                journal_summary.journal_id,
                page_size=100,
                page_token=None,
            )["agents"]
        )
        >= 1
    )
    with operator.stream_collection_provenance_journal(
        collection_id,
        journal_summary.journal_id,
    ) as chunks:
        assert b"".join(chunks) == journal
    assert operator.request_collection_provenance_verification(collection_id)["state"] == "queued"
    verification = _complete_provenance_verification(container, operator, collection_id)
    assert verification["state"] == "succeeded"
    assert verification["result"]["valid"] is True
    assert operator.cancel_collection_provenance_verification(collection_id)["state"] == "succeeded"

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
        f"http://testserver/v1/catalog/collections/{collection_id}/inventory"
    ]
    assert int(roots[4].attrib["data-cursor"]) > 0
    inventory = operator.get_portable_collection_inventory(collection_id)
    assert inventory.authority.header.collection == collection_id
    assert len(inventory.files) == 1
    assert inventory.complete is True

    assert {
        item["store"]
        for item in operator.list_archive_stores(page_size=100, page_token=None)["stores"]
    } == {"primary", "secondary"}
    assert operator.get_archive_store("primary")["store"] == "primary"
    assert operator.retrieval_cache_status()["configured"] is False
    assert operator.list_retrieval_cache_objects(page_size=100, page_token=None)["objects"] == []

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

    orphaned_upload = operator.create_or_resume_collection_upload_session(
        "qualification-orphaned-upload",
        ["docs"],
        provenance_mode="omitted",
        provenance_omission_reason="qualification orphan discard",
        custody_mode="custody-transfer",
    )
    orphaned_id = int(orphaned_upload["collection_id"])
    assert operator.heartbeat_collection_upload_session(orphaned_id)["state"] == "open"
    with session_scope(container.session_factory) as database:
        record = database.get(CollectionUploadRecord, orphaned_id)
        assert record is not None
        record.lease_expires_at = "2020-01-01T00:00:00.000000Z"
    assert container.collection_uploads.reap_expired_custody_transfers() == 1
    discard_plan = operator.plan_collection_upload_discard(orphaned_id)
    assert discard_plan["status"] == "ready"
    assert (
        operator.discard_collection_upload(
            orphaned_id,
            challenge=str(discard_plan["challenge"]),
        )["status"]
        == "discarded"
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
    assert len(operator.list_archive_copy_jobs(page_size=100, page_token=None)["copies"]) == 1
    assert (
        operator.cancel_archive_copy_job(collection_id, destination_store="secondary")["state"]
        == "canceled"
    )
    assert copy["destination_store"] == "secondary"

    source_collection = operator.get_collection(collection_id)
    source_identity = CollectionRootIdentity(
        collection_id=collection_id,
        archive_root_sha256=str(source_collection["archive_root_sha256"]),
        content_identity=str(source_collection["content_identity"]),
    )
    source_artifact = {
        "collection": source_identity.as_dict(),
        "path": binding.path,
        "bytes": binding.bytes,
        "sha256": binding.sha256,
    }

    abandoned_work = {
        "format": "qualification-work/v1",
        "kind": "abandonment-witness",
        "inputs": [source_identity.as_dict()],
    }
    abandoned_work_id = canonical_json_sha256(abandoned_work)
    abandoned_claim = operator.create_or_resume_processing_claim(
        work_id=abandoned_work_id,
        work_document=abandoned_work,
        work_document_sha256=abandoned_work_id,
        inputs=[source_identity.as_dict()],
    )
    abandoned_claim_id = str(abandoned_claim["id"])
    abandoned_fence = int(abandoned_claim["fence"])
    read_capability = operator.create_transform_capability(
        abandoned_claim_id,
        fence=abandoned_fence,
        audience="qualification.observer/v1",
        actions=("read-inputs",),
        artifacts=(source_artifact,),
    )
    restarted = operator.restart_processing_claim(
        abandoned_claim_id,
        fence=abandoned_fence,
        lease_seconds=1800,
    )
    abandoned_fence = int(restarted["fence"])
    assert abandoned_fence == 2
    assert restarted["plan"] is None
    assert (
        transport.get(
            f"/v1/collections/{collection_id}",
            headers={"Authorization": f"Bearer {read_capability['token']}"},
        ).status_code
        == 401
    )
    abandonment_reason = "qualification: explicit terminal no-output work"
    abandoned = operator.abandon_processing_claim(
        abandoned_claim_id,
        fence=abandoned_fence,
        reason=abandonment_reason,
    )
    assert abandoned["state"] == "abandoned"
    assert abandoned["abandonment_reason"] == abandonment_reason
    assert (
        operator.abandon_processing_claim(
            abandoned_claim_id,
            fence=abandoned_fence,
            reason=abandonment_reason,
        )["state"]
        == "abandoned"
    )

    operation_identity = OperationIdentity(
        "qualification-transform/v1",
        hashlib.sha256(b"qualification-operation-contract").hexdigest(),
    )
    recipe_identity = RecipeIdentity(
        "qualification-recipe/v1",
        1,
        hashlib.sha256(b"qualification-recipe-contract").hexdigest(),
    )
    multi_output_work = {
        "format": "qualification-multi-output-work/v1",
        "inputs": [source_identity.as_dict()],
    }
    multi_output_work_id = canonical_json_sha256(multi_output_work)
    outcome_claim = operator.create_or_resume_processing_claim(
        work_id=multi_output_work_id,
        work_document=multi_output_work,
        work_document_sha256=multi_output_work_id,
        inputs=[source_identity.as_dict()],
        purpose="qualification-multi-output/v1",
    )
    outcome_claim_id = str(outcome_claim["id"])
    outcome_fence = int(outcome_claim["fence"])
    work_document = {
        "format": "qualification-work/v1",
        "recipe": recipe_identity.as_dict(),
        "inputs": [source_identity.as_dict()],
    }
    work_id = canonical_json_sha256(work_document)
    claim = operator.create_or_resume_processing_claim(
        work_id=work_id,
        work_document=work_document,
        work_document_sha256=work_id,
        inputs=[source_identity.as_dict()],
    )
    claim_id = str(claim["id"])
    claim_fence = int(claim["fence"])
    assert operator.get_processing_claim(claim_id)["work_id"] == work_id
    assert len(operator.list_processing_claims(page_size=100, page_token=None).claims) == 3
    assert (
        operator.renew_processing_claim(
            claim_id,
            fence=claim_fence,
            lease_seconds=1800,
        )["state"]
        == "active"
    )

    execution_id = hashlib.sha256(b"qualification-execution-envelope").hexdigest()
    controller_evidence = {
        "format": "qualification-controller-evidence/v1",
        "claim": {"id": claim_id, "fence": claim_fence},
        "work_id": work_id,
        "execution_id": execution_id,
    }
    controller_evidence_sha256 = canonical_json_sha256(controller_evidence)
    sealed = operator.seal_processing_claim_plan(
        claim_id,
        fence=claim_fence,
        execution_id=execution_id,
        controller_evidence=controller_evidence,
        controller_evidence_sha256=controller_evidence_sha256,
        operation_id=operation_identity.id,
        operation_sha256=operation_identity.sha256,
        input_artifacts=(source_artifact,),
        output_tags=("reviewed",),
        retirement_policy="retain",
    )
    assert sealed["plan"]["execution_id"] == execution_id
    sealed_plan = sealed["plan"]
    assert (
        operator.list_processing_claim_inputs(
            claim_id,
            authority_sha256=str(sealed["inputs"]["authority"]["sha256"]),
        )
        .inputs[0]
        .collection_id
        == collection_id
    )
    assert (
        operator.list_processing_claim_artifacts(
            claim_id,
            authority_sha256=str(sealed_plan["artifacts"]["sha256"]),
        )
        .artifacts[0]
        .path
        == "document.txt"
    )
    assert operator.list_processing_claim_output_tags(
        claim_id,
        authority_sha256=str(sealed_plan["output_tags"]["sha256"]),
    ).tags == ["reviewed"]
    output_capability = operator.create_transform_capability(
        claim_id,
        fence=claim_fence,
        audience="qualification.target/v1",
        actions=("read-inputs", "write-output"),
        artifacts=(source_artifact,),
    )

    output_root = tmp_path / "derived"
    output_payload_path = output_root / "derived" / "document.txt"
    output_payload_path.parent.mkdir(parents=True)
    output_payload_path.write_bytes(source.read_bytes().upper())
    output_relative_path = "derived/document.txt"
    disposition = ArtifactDisposition(
        input_collection_id=collection_id,
        input_archive_root_sha256=source_identity.archive_root_sha256,
        input_path="document.txt",
        status="transformed",
    )
    disposition_output = ArtifactDispositionOutput(
        input_collection_id=collection_id,
        input_archive_root_sha256=source_identity.archive_root_sha256,
        input_path="document.txt",
        output_path=output_relative_path,
    )
    operator.record_processing_claim_dispositions(
        claim_id,
        fence=claim_fence,
        dispositions=(disposition.as_dict(),),
    )
    operator.record_processing_claim_disposition_outputs(
        claim_id,
        fence=claim_fence,
        outputs=(disposition_output.as_dict(),),
    )
    disposition_state = operator.seal_processing_claim_dispositions(
        claim_id,
        fence=claim_fence,
    )
    while disposition_state.state != "sealed":
        assert disposition_state.state == "sealing"
        assert container.collection_workflows.process_due_disposition_sets() == 1
        disposition_state = operator.get_processing_claim_dispositions(claim_id)
    assert disposition_state.identity is not None
    disposition_identity = ArtifactDispositionSetIdentity.from_mapping(
        disposition_state.identity.model_dump(mode="json")
    )
    assert (
        len(
            operator.list_processing_claim_dispositions(
                claim_id,
                authority_sha256=disposition_identity.sha256,
            ).dispositions
        )
        == 1
    )
    assert (
        len(
            operator.list_processing_claim_disposition_outputs(
                claim_id,
                authority_sha256=disposition_identity.sha256,
            ).outputs
        )
        == 1
    )
    plan = sealed["plan"]
    derivation = CollectionDerivation(
        execution_id=execution_id,
        claim_id=claim_id,
        fence=claim_fence,
        recipe=recipe_identity,
        operation=operation_identity,
        input_set_sha256=str(plan["inputs"]["sha256"]),
        artifact_set_sha256=str(plan["artifacts"]["sha256"]),
        output_tag_set_sha256=str(plan["output_tags"]["sha256"]),
        execution_envelope_sha256=execution_id,
        execution_sha256=hashlib.sha256(b"qualification-execution-result").hexdigest(),
        controller_evidence=controller_evidence,
        controller_evidence_sha256=controller_evidence_sha256,
        disposition_set=disposition_identity,
    )
    derivation_path = output_root / DERIVATION_EVIDENCE_PATH
    derivation_path.parent.mkdir(parents=True)
    derivation_path.write_bytes(derivation.to_json_bytes())
    producer_evidence = ProducerEvidence(
        producer_app="qualification.target/v1",
        adapter_id="qualification.target/v1",
        adapter_version="1.0.0",
        source_event_id=execution_id,
        ingest_source=f"transform:{execution_id}",
        source_context={"execution_id": execution_id},
    )
    producer_evidence_path = output_root / PRODUCER_EVIDENCE_PATH
    producer_evidence_path.parent.mkdir(parents=True, exist_ok=True)
    producer_evidence_path.write_bytes(producer_evidence.to_json_bytes())
    output_files = {
        output_relative_path: output_payload_path,
        PRODUCER_EVIDENCE_PATH: producer_evidence_path,
        DERIVATION_EVIDENCE_PATH: derivation_path,
    }
    output_entries = [
        (
            path,
            current.stat().st_size,
            hashlib.sha256(current.read_bytes()).hexdigest(),
        )
        for path, current in output_files.items()
    ]
    output_payload_bytes = output_payload_path.stat().st_size
    output_payload_sha256 = hashlib.sha256(output_payload_path.read_bytes()).hexdigest()
    output_journal = create_derivative_journal_from_identity(
        relative_path=output_relative_path,
        byte_count=output_payload_bytes,
        sha256=output_payload_sha256,
        source_journals=(journal,),
        agent_name="qualification.target/v1",
        agent_version="1.0.0",
        event_label=operation_identity.id,
        started_at="2026-08-10T01:00:00Z",
        ended_at="2026-08-10T01:01:00Z",
    )
    output_journal_summary = validate_journal(output_journal)
    output_provenance_journals = {
        journal_summary.journal_id: journal,
        output_journal_summary.journal_id: output_journal,
    }
    target = _api(transport, str(output_capability["token"]), observer=observer)
    assert (
        len(
            target.list_collection_provenance(collection_id, page_size=100, page_token=None)[
                "files"
            ]
        )
        == 1
    )
    with target.stream_collection_provenance_journal(
        collection_id,
        journal_summary.journal_id,
    ) as chunks:
        assert b"".join(chunks) == journal
    target_retrieval_plan = target.plan_retrieval(
        [(collection_id, "document.txt")],
        restore_policy="never",
    )
    target_retrieval = target.create_retrieval_job(
        [(collection_id, "document.txt")],
        plan_etag=str(target_retrieval_plan["etag"]),
        restore_policy="never",
    )
    assert target_retrieval["state"] == "ready"
    assert target.acknowledge_retrieval_job(str(target_retrieval["id"]))["state"] == "completed"
    with pytest.raises(Forbidden):
        target.create_or_resume_collection_upload_session(
            hashlib.sha256(b"unauthorized-output").hexdigest(),
            ["reviewed"],
            ingest_source=f"transform:{execution_id}",
            provenance_mode="omitted",
            provenance_omission_reason="qualification transform evidence",
        )
    with pytest.raises(Forbidden):
        target.create_or_resume_collection_upload_session(
            execution_id,
            ["reviewed"],
            ingest_source="transform:another-execution",
            provenance_mode="omitted",
            provenance_omission_reason="qualification transform evidence",
        )
    with pytest.raises(Forbidden):
        target.create_or_resume_collection_upload_session(
            execution_id,
            ["reviewed"],
            ingest_source=f"transform:{execution_id}",
            archive_store="primary",
            provenance_mode="omitted",
            provenance_omission_reason="qualification transform evidence",
        )
    target_session = target.create_or_resume_collection_upload_session(
        execution_id,
        ["reviewed"],
        ingest_source=f"transform:{execution_id}",
        provenance_mode="captured",
    )
    output_collection_id = int(target_session["collection_id"])
    replayed_target_session = target.create_or_resume_collection_upload_session(
        execution_id,
        ["reviewed"],
        ingest_source=f"transform:{execution_id}",
        provenance_mode="captured",
    )
    assert replayed_target_session["resumed"] is True
    assert int(replayed_target_session["collection_id"]) == output_collection_id
    for journal_id, content in output_provenance_journals.items():
        staged_output = target.upload_collection_upload_session_provenance_journal(
            output_collection_id,
            journal_id,
            content=(content,),
            byte_count=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        )
        while staged_output.state != "sealed":
            assert staged_output.state == "validating"
            assert container.collection_uploads.process_due_provenance_journal_validations() == 1
            staged_output = target.get_collection_upload_session_provenance_journal(
                output_collection_id, journal_id
            )
    target.register_collection_upload_session_files(
        output_collection_id,
        [
            {
                "path": path,
                "bytes": byte_count,
                "sha256": sha256,
                "provenance": (
                    {
                        "status": "captured",
                        "journal_id": output_journal_summary.journal_id,
                        "current_state_id": output_journal_summary.current_state_id,
                    }
                    if path == output_relative_path
                    else {
                        "status": "omitted",
                        "omission_reason": "Riverhog control evidence has no host provenance",
                    }
                ),
            }
            for path, byte_count, sha256 in sorted(
                output_entries, key=lambda item: collection_upload_path_order_key(item[0])
            )
        ],
        registration_constraints=target_session["registration_constraints"],
    )
    target.complete_collection_upload_session(
        output_collection_id,
        files_total=len(output_entries),
        content_identity=collection_content_identity(output_entries),
    )
    while work := target.acquire_collection_upload_session_work(output_collection_id).work:
        for assignment in work:
            target.put_collection_upload_session_unit(
                output_collection_id,
                assignment.volume.volume_id,
                assignment.unit.unit,
                plan_sha256=assignment.plan_sha256,
                content=_unit_content(output_root, assignment.unit),
            )
    _finalize_upload(container, target, output_collection_id)
    assert target.get_collection_upload_session(output_collection_id)["state"] == "finalized"
    replayed_output = target.create_or_resume_collection_upload_session(
        execution_id,
        ["reviewed"],
        ingest_source=f"transform:{execution_id}",
        provenance_mode="captured",
    )
    assert replayed_output["state"] == "finalized"
    assert replayed_output["resumed"] is True
    assert int(replayed_output["collection_id"]) == output_collection_id
    target.close()

    derived_provenance = operator.get_collection_file_provenance(
        output_collection_id,
        output_relative_path,
    )
    assert derived_provenance["journal"]["journal_id"] == (output_journal_summary.journal_id)
    derived_trace = operator.trace_collection_file_provenance(
        output_collection_id,
        output_relative_path,
    )
    assert {
        item["journal"]["journal_id"]
        for item in derived_trace["items"]
        if item["kind"] == "journal"
    } == {
        journal_summary.journal_id,
        output_journal_summary.journal_id,
    }
    with operator.stream_collection_provenance_journal(
        output_collection_id,
        output_journal_summary.journal_id,
    ) as chunks:
        assert b"".join(chunks) == output_journal
    assert (
        operator.request_collection_provenance_verification(output_collection_id)["state"]
        == "queued"
    )
    output_verification = _complete_provenance_verification(
        container, operator, output_collection_id
    )
    assert output_verification["state"] == "succeeded"
    assert output_verification["result"]["valid"] is True

    settled = operator.settle_processing_claim(
        claim_id,
        fence=claim_fence,
        output_collection_id=output_collection_id,
        derivation=derivation.as_dict(),
        outcome_claim_id=outcome_claim_id,
        outcome_fence=outcome_fence,
        outcome_id="qualification-output",
    )
    assert settled["state"] == "settled"
    replayed_settlement = operator.settle_processing_claim(
        claim_id,
        fence=claim_fence,
        output_collection_id=output_collection_id,
        derivation=derivation.as_dict(),
        outcome_claim_id=outcome_claim_id,
        outcome_fence=outcome_fence,
        outcome_id="qualification-output",
    )
    assert replayed_settlement["state"] == "settled"
    assert (
        operator.get_collection_derivation(output_collection_id)["document_sha256"]
        == derivation.sha256
    )
    assert operator.release_processing_claim(claim_id, fence=claim_fence)["state"] == "released"
    outcomes = operator.get_processing_claim(outcome_claim_id)["outcomes"]
    assert outcomes["count"] == 1
    assert outcomes["authority"] is None
    settled_outcomes = operator.settle_processing_claim_outcomes(
        outcome_claim_id,
        fence=outcome_fence,
        retirement_policy="retire-after-verified-output",
    )
    while settled_outcomes["state"] == "active":
        assert container.collection_workflows.process_due_outcome_sets() == 1
        settled_outcomes = operator.settle_processing_claim_outcomes(
            outcome_claim_id,
            fence=outcome_fence,
            retirement_policy="retire-after-verified-output",
        )
    assert settled_outcomes["state"] == "settled"
    assert settled_outcomes["outcomes"]["authority"] is not None
    outcome_authority = settled_outcomes["outcomes"]["authority"]
    assert (
        operator.list_processing_claim_outcomes(
            outcome_claim_id,
            authority_sha256=str(outcome_authority["sha256"]),
        )
        .outcomes[0]
        .outcome_id
        == "qualification-output"
    )
    retiring = operator.begin_processing_claim_retirement(
        outcome_claim_id,
        fence=outcome_fence,
    )
    assert retiring["state"] == "retiring"
    replayed_outcomes = operator.settle_processing_claim_outcomes(
        outcome_claim_id,
        fence=outcome_fence,
        retirement_policy="retire-after-verified-output",
    )
    assert replayed_outcomes["state"] == "retiring"
    retirement = operator.plan_collection_deletion(
        collection_id,
        retirement_claim_id=outcome_claim_id,
    )
    assert retirement["status"] == "ready"
    assert (
        operator.delete_collection(
            collection_id,
            challenge=str(retirement["challenge"]),
            retirement_claim_id=outcome_claim_id,
        )["status"]
        == "deleted"
    )
    assert {
        item["journal"]["journal_id"]
        for item in operator.trace_collection_file_provenance(
            output_collection_id,
            output_relative_path,
            page_size=100,
        )["items"]
        if item["kind"] == "journal"
    } == {
        journal_summary.journal_id,
        output_journal_summary.journal_id,
    }
    assert (
        operator.release_processing_claim(
            outcome_claim_id,
            fence=outcome_fence,
        )["state"]
        == "released"
    )
    replayed_released_settlement = operator.settle_processing_claim_outcomes(
        outcome_claim_id,
        fence=outcome_fence,
        retirement_policy="retire-after-verified-output",
    )
    assert replayed_released_settlement["state"] == "released"

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
    from scripts.operation_qualification import operation_matrix

    observer.require(
        operation.operation_id
        for operation in operation_matrix()
        if operation.application == "riverhog" and operation.provider_evidence is None
    )

    transport.close()
    container.close()
