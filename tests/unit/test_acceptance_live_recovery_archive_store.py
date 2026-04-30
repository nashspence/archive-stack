from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from arc_core.ports.archive_store import ArchiveRestoreStatus, ArchiveUploadReceipt
from tests.fixtures.acceptance import AcceptanceSystem
from tests.fixtures.data import IMAGE_ID


class RecordingArchiveStore:
    def __init__(self) -> None:
        self.restore_requests: list[dict[str, object]] = []
        self.status_requests: list[dict[str, object]] = []
        self.download_requests: list[dict[str, object]] = []
        self.cleanup_requests: list[dict[str, object]] = []

    def upload_finalized_image(
        self,
        *,
        image_id: str,
        filename: str,
        image_root: Path,
    ) -> ArchiveUploadReceipt:
        raise AssertionError("acceptance recovery API test should not upload archives")

    def request_finalized_image_restore(
        self,
        *,
        image_id: str,
        object_path: str,
        retrieval_tier: str,
        hold_days: int,
        requested_at: str,
        estimated_ready_at: str,
    ) -> ArchiveRestoreStatus:
        self.restore_requests.append(
            {
                "image_id": image_id,
                "object_path": object_path,
                "retrieval_tier": retrieval_tier,
                "hold_days": hold_days,
                "requested_at": requested_at,
                "estimated_ready_at": estimated_ready_at,
            }
        )
        return ArchiveRestoreStatus(state="ready", ready_at=requested_at)

    def get_finalized_image_restore_status(
        self,
        *,
        image_id: str,
        object_path: str,
        requested_at: str,
        estimated_ready_at: str | None,
        estimated_expires_at: str | None,
    ) -> ArchiveRestoreStatus:
        self.status_requests.append(
            {
                "image_id": image_id,
                "object_path": object_path,
                "requested_at": requested_at,
                "estimated_ready_at": estimated_ready_at,
                "estimated_expires_at": estimated_expires_at,
            }
        )
        return ArchiveRestoreStatus(state="ready", ready_at=requested_at)

    def iter_restored_finalized_image(
        self,
        *,
        image_id: str,
        object_path: str,
    ) -> Iterator[bytes]:
        self.download_requests.append({"image_id": image_id, "object_path": object_path})
        yield b"live-restored-iso"

    def cleanup_finalized_image_restore(
        self,
        *,
        image_id: str,
        object_path: str,
    ) -> None:
        self.cleanup_requests.append({"image_id": image_id, "object_path": object_path})


def test_acceptance_recovery_api_can_use_live_archive_store_with_fake_backed_state(
    tmp_path: Path,
) -> None:
    system = AcceptanceSystem.create(tmp_path / "acceptance-system")
    store = RecordingArchiveStore()
    try:
        system.seed_planner_fixtures()
        response = system.request("POST", f"/v1/plan/candidates/{IMAGE_ID}/finalize")
        assert response.status_code == 200, response.text
        image_id = response.json()["id"]
        system.wait_for_image_glacier_state(image_id, "uploaded")
        object_path = system.state.glacier_status(image_id).object_path
        assert object_path is not None

        for copy_id, state in ((f"{image_id}-1", "lost"), (f"{image_id}-2", "damaged")):
            response = system.request(
                "POST",
                f"/v1/images/{image_id}/copies",
                json_body={"copy_id": copy_id, "location": f"fixture shelf {copy_id}"},
            )
            assert response.status_code == 200, response.text
            response = system.request(
                "PATCH",
                f"/v1/images/{image_id}/copies/{copy_id}",
                json_body={"state": state},
            )
            assert response.status_code == 200, response.text

        session = system.recovery_sessions.get_for_image(image_id)
        system.enable_live_recovery_archive_store(
            store,
            retrieval_tier="standard",
            hold_days=2,
            poll_interval_seconds=0.0,
        )

        response = system.request("POST", f"/v1/recovery-sessions/{session.id}/approve")
        assert response.status_code == 200, response.text
        assert store.restore_requests == [
            {
                "image_id": image_id,
                "object_path": object_path,
                "retrieval_tier": "standard",
                "hold_days": 2,
                "requested_at": store.restore_requests[0]["requested_at"],
                "estimated_ready_at": store.restore_requests[0]["estimated_ready_at"],
            }
        ]

        system.wait_for_recovery_session_state(str(session.id), "ready")
        response = system.request(
            "GET",
            f"/v1/recovery-sessions/{session.id}/images/{image_id}/iso",
        )
        assert response.status_code == 200, response.text
        assert response.content == b"live-restored-iso"
        assert store.download_requests == [{"image_id": image_id, "object_path": object_path}]

        response = system.request("POST", f"/v1/recovery-sessions/{session.id}/complete")
        assert response.status_code == 200, response.text
        assert response.json()["state"] == "completed"
        assert store.cleanup_requests == [{"image_id": image_id, "object_path": object_path}]
    finally:
        system.close()
