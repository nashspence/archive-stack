from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from tests.fixtures.acceptance import AcceptanceSystem
from tests.fixtures.data import IMAGE_ID


def test_acceptance_system_can_serve_real_iso_streams_from_fake_backed_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    system = AcceptanceSystem.create(tmp_path / "acceptance-system")
    try:
        system.seed_planner_fixtures()
        response = system.request("POST", f"/v1/plan/candidates/{IMAGE_ID}/finalize")
        assert response.status_code == 200, response.text
        image_id = response.json()["id"]

        response = system.request("GET", f"/v1/images/{image_id}/iso")
        assert response.status_code == 200, response.text
        assert b'"fixture": "spec-iso"' in response.content

        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=b"real-iso"),
        )
        system.enable_real_iso_streams()

        response = system.request("GET", f"/v1/images/{image_id}/iso")
        assert response.status_code == 200, response.text
        assert response.content == b"real-iso"

        system.wait_for_image_glacier_state(image_id, "uploaded")
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
        response = system.request("POST", f"/v1/recovery-sessions/{session.id}/approve")
        assert response.status_code == 200, response.text
        system.wait_for_recovery_session_state(str(session.id), "ready")

        response = system.request(
            "GET",
            f"/v1/recovery-sessions/{session.id}/images/{image_id}/iso",
        )
        assert response.status_code == 200, response.text
        assert response.content == b"real-iso"
    finally:
        system.close()
