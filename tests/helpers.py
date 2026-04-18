from __future__ import annotations

from pathlib import Path
import re
from typing import Callable

from .mock_data import MockFile


def _default_root_node_name(description: str | None) -> str:
    base = (description or "archive-collection").strip().lower()
    slug = re.sub(r"[^a-z0-9._-]+", "-", base).strip("-")
    return slug or "archive-collection"


def create_collection(harness, *, description: str, keep_buffer_after_archive: bool = False, root_node_name: str | None = None) -> str:
    response = harness.client.post(
        "/v1/collections",
        headers=harness.auth_headers(),
        json={
            "root_node_name": root_node_name or _default_root_node_name(description),
            "description": description,
            "keep_buffer_after_archive": keep_buffer_after_archive,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["collection_id"]


def reserve_collection_upload(harness, collection_id: str, sample: MockFile) -> dict:
    response = harness.client.post(
        f"/v1/collections/{collection_id}/uploads",
        headers=harness.auth_headers(),
        json=sample.upload_payload(),
    )
    assert response.status_code == 200, response.text
    return response.json()


def simulate_tusd_upload(harness, slot: dict, content: bytes) -> None:
    payload = {
        "ID": slot["upload_id"],
        "Size": len(content),
        "MetaData": slot["tus_metadata"],
    }
    precreate = harness.client.post(
        harness.hook_url(),
        headers=harness.hook_headers("pre-create"),
        json=payload,
    )
    assert precreate.status_code == 200, precreate.text
    incoming_path = Path(precreate.json()["ChangeFileInfo"]["Storage"]["Path"])
    incoming_path.parent.mkdir(parents=True, exist_ok=True)
    incoming_path.write_bytes(content)

    for hook_name, body in [
        ("post-create", {"ID": slot["upload_id"]}),
        ("post-receive", {"ID": slot["upload_id"], "Offset": len(content)}),
        ("post-finish", {"ID": slot["upload_id"]}),
    ]:
        response = harness.client.post(
            harness.hook_url(),
            headers=harness.hook_headers(hook_name),
            json=body,
        )
        assert response.status_code == 200, response.text


def upload_collection_file(harness, collection_id: str, sample: MockFile) -> dict:
    slot = reserve_collection_upload(harness, collection_id, sample)
    simulate_tusd_upload(harness, slot, sample.content)
    return slot


def seal_collection(harness, collection_id: str) -> dict:
    response = harness.client.post(
        f"/v1/collections/{collection_id}/seal",
        headers=harness.auth_headers(),
    )
    assert response.status_code == 200, response.text
    return response.json()


def force_flush(harness) -> list[str]:
    response = harness.client.post(
        "/v1/containers/flush?force=true",
        headers=harness.auth_headers(),
    )
    assert response.status_code == 200, response.text
    return response.json()["closed_containers"]


def closed_container_roots(harness, container_ids: list[str]) -> dict[str, Path]:
    roots: dict[str, Path] = {}
    with harness.session() as session:
        for container_id in container_ids:
            container = session.get(harness.models.Container, container_id)
            assert container is not None
            roots[container_id] = Path(container.root_abs_path)
    return roots


def activation_container_from_root(
    harness,
    container_id: str,
    *,
    mutate: Callable[[str, bytes], bytes] | None = None,
) -> tuple[dict, dict]:
    create_session = harness.client.post(
        f"/v1/containers/{container_id}/activation/sessions",
        headers=harness.auth_headers(),
    )
    assert create_session.status_code == 200, create_session.text
    session_body = create_session.json()
    session_id = session_body["session_id"]

    expected = harness.client.get(
        f"/v1/containers/{container_id}/activation/sessions/{session_id}/expected",
        headers=harness.auth_headers(),
    )
    assert expected.status_code == 200, expected.text

    with harness.session() as session:
        container = session.get(harness.models.Container, container_id)
        assert container is not None
        root = Path(container.root_abs_path)

    for entry in expected.json()["entries"]:
        relpath = entry["relative_path"]
        content = (root / relpath).read_bytes()
        if mutate is not None:
            content = mutate(relpath, content)
        slot_response = harness.client.post(
            f"/v1/containers/{container_id}/activation/sessions/{session_id}/uploads",
            headers=harness.auth_headers(),
            json={"relative_path": relpath},
        )
        assert slot_response.status_code == 200, slot_response.text
        simulate_tusd_upload(harness, slot_response.json(), content)

    complete = harness.client.post(
        f"/v1/containers/{container_id}/activation/sessions/{session_id}/complete",
        headers=harness.auth_headers(),
    )
    return session_body, complete


def register_iso(harness, container_id: str, content: bytes) -> dict:
    source = harness.archive_root / "seed-isos" / f"{container_id}.iso"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(content)
    response = harness.client.post(
        f"/v1/containers/{container_id}/iso/register",
        headers=harness.auth_headers(),
        json={"server_path": str(source)},
    )
    assert response.status_code == 200, response.text
    return response.json()


def create_iso(harness, container_id: str, *, overwrite: bool = False, volume_label: str | None = None) -> dict:
    payload: dict[str, object] = {"overwrite": overwrite}
    if volume_label is not None:
        payload["volume_label"] = volume_label
    response = harness.client.post(
        f"/v1/containers/{container_id}/iso/create",
        headers=harness.auth_headers(),
        json=payload,
    )
    assert response.status_code == 200, response.text
    return response.json()
