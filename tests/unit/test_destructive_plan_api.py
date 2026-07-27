from __future__ import annotations

from types import SimpleNamespace

import anyio
import httpx
from fastapi import FastAPI
from riverhog_api.deps import get_container
from riverhog_api.routers.archive import router as archive_router
from riverhog_api.routers.collections import router as collections_router
from riverhog_core.app_permissions import ALL_PERMISSIONS, ApplicationAccess, ApplicationPrincipal

COLLECTION_ID = 42
OBJECT_KINDS = (
    "pack",
    "manifest",
    "proof",
    "checksums",
    "signature",
    "signature-proof",
    "metadata",
)


class _AppKeys:
    @staticmethod
    def authenticate(_token: str) -> ApplicationPrincipal:
        return ApplicationPrincipal(
            app="operator",
            key_id="operator-key",
            access=frozenset({ApplicationAccess(ALL_PERMISSIONS)}),
        )


class _CollectionAccess:
    @staticmethod
    def require(_principal: object, _permission: str, collection_id: int) -> int:
        return collection_id


def _objects(*, include_store: bool) -> list[dict[str, object]]:
    return [
        {
            **({"store": "b2"} if include_store else {}),
            "kind": kind,
            "object_path": f"archives/opaque/{kind}",
            "stored_bytes": 1,
        }
        for kind in OBJECT_KINDS
    ]


class _CollectionDeletions:
    @staticmethod
    def plan(collection_id: int) -> dict[str, object]:
        return {
            "status": "ready",
            "collection_id": collection_id,
            "warning": "Confirm exact custody loss.",
            "expires_at": "2026-07-27T01:00:00.000000Z",
            "challenge": "delete-fixture",
            "files": [{"path": "file.txt", "bytes": 1}],
            "file_count": 1,
            "bytes": 1,
            "archive_objects": _objects(include_store=True),
            "remote_storage_bytes": len(OBJECT_KINDS),
            "upload_files": [],
            "record_etag": "0" * 64,
            "metadata_rows": {"collections": 1},
            "blockers": [],
            "billing_note": "Provider billing can lag.",
        }


class _ArchiveCopyRetirements:
    @staticmethod
    def plan(collection_id: int, *, store: str) -> dict[str, object]:
        return {
            "status": "blocked",
            "collection_id": collection_id,
            "store": store,
            "warning": "Confirm exact custody loss.",
            "expires_at": "2026-07-27T01:00:00.000000Z",
            "challenge": None,
            "target_copy": {
                "store": store,
                "last_verified_at": "2026-07-27T00:00:00.000000Z",
                "remote_storage_bytes": len(OBJECT_KINDS),
                "objects": _objects(include_store=False),
            },
            "retained_copies": [],
            "retired_restore_records": [],
            "blockers": ["retirement would remove the last copy"],
            "verification_note": "A retained copy is verified before retirement.",
            "billing_note": "Provider billing can lag.",
        }


def test_mature_archive_objects_survive_destructive_plan_response_validation() -> None:
    container = SimpleNamespace(
        app_keys=_AppKeys(),
        collection_access=_CollectionAccess(),
        collection_deletions=_CollectionDeletions(),
        archive_copy_retirements=_ArchiveCopyRetirements(),
    )
    api = FastAPI()
    api.dependency_overrides[get_container] = lambda: container
    api.include_router(collections_router, prefix="/v1")
    api.include_router(archive_router, prefix="/v1")

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=api)
        headers = {"Authorization": "Bearer operator"}
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            deletion = await client.post(
                f"/v1/collections/{COLLECTION_ID}/deletion-plan",
                headers=headers,
            )
            assert deletion.status_code == 200
            assert {item["kind"] for item in deletion.json()["archive_objects"]} == set(
                OBJECT_KINDS
            )

            retirement = await client.post(
                "/v1/archive/copies/retirement-plan",
                headers=headers,
                json={"collection_id": COLLECTION_ID, "store": "b2"},
            )
            assert retirement.status_code == 200
            assert {item["kind"] for item in retirement.json()["target_copy"]["objects"]} == set(
                OBJECT_KINDS
            )

    anyio.run(exercise)
