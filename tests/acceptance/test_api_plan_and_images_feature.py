from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

from arc_core.planner.manifest import MANIFEST_FILENAME, README_FILENAME
from tests.fixtures.acceptance import AcceptanceSystem, acceptance_system
from tests.fixtures.data import (
    DOCS_COLLECTION_ID,
    DOCS_FILES,
    IMAGE_ID,
    SPLIT_FILE_PARTS,
    SPLIT_FILE_RELPATH,
    SPLIT_IMAGE_ONE_ID,
    SPLIT_IMAGE_TWO_ID,
    TARGET_BYTES,
    fixture_decrypt_bytes,
)


def _write_downloaded_iso(iso_bytes: bytes, workspace: Path) -> Path:
    iso_path = workspace / "image.iso"
    iso_path.write_bytes(iso_bytes)
    return iso_path


def _verify_iso(iso_path: Path) -> None:
    proc = subprocess.run(
        [
            "xorriso",
            "-abort_on",
            "FAILURE",
            "-for_backup",
            "-md5",
            "on",
            "-indev",
            str(iso_path),
            "-check_md5",
            "FAILURE",
            "--",
            "-check_md5_r",
            "FAILURE",
            "/",
            "--",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, "\n".join(part for part in (proc.stdout, proc.stderr) if part)


def _extract_iso(iso_path: Path, workspace: Path) -> Path:
    extract_root = workspace / "disc"
    extract_root.mkdir()
    proc = subprocess.run(
        [
            "xorriso",
            "-osirrox",
            "on",
            "-indev",
            str(iso_path),
            "-extract",
            "/",
            str(extract_root),
            "-end",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return extract_root


def test_read_the_current_plan(acceptance_system: AcceptanceSystem) -> None:
    acceptance_system.seed_planner_fixtures()

    response = acceptance_system.request("GET", "/v1/plan")

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {
        "ready",
        "target_bytes",
        "min_fill_bytes",
        "images",
        "unplanned_bytes",
        "note",
    }
    assert payload["ready"] is True
    assert payload["target_bytes"] == TARGET_BYTES
    assert payload["images"]
    fills = []
    for image in payload["images"]:
        assert set(image) == {"id", "bytes", "fill", "files", "collections", "iso_ready"}
        assert image["fill"] == image["bytes"] / payload["target_bytes"]
        fills.append(image["fill"])
    assert fills == sorted(fills, reverse=True)


def test_read_one_image_summary(acceptance_system: AcceptanceSystem) -> None:
    acceptance_system.seed_planner_fixtures()

    response = acceptance_system.request("GET", f"/v1/images/{IMAGE_ID}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == IMAGE_ID
    assert set(payload) == {"id", "bytes", "fill", "files", "collections", "iso_ready"}


def test_download_an_iso_for_a_ready_image(acceptance_system: AcceptanceSystem) -> None:
    acceptance_system.seed_planner_fixtures()

    response = acceptance_system.request("GET", f"/v1/images/{IMAGE_ID}/iso")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/octet-stream")
    assert response.headers["content-disposition"].endswith(f'"{IMAGE_ID}.iso"')
    assert response.content


def test_ready_image_iso_uses_the_canonical_disc_layout(
    acceptance_system: AcceptanceSystem,
    tmp_path: Path,
) -> None:
    acceptance_system.seed_planner_fixtures()

    response = acceptance_system.request("GET", f"/v1/images/{IMAGE_ID}/iso")

    assert response.status_code == 200
    iso_path = _write_downloaded_iso(response.content, tmp_path)
    _verify_iso(iso_path)
    extract_root = _extract_iso(iso_path, tmp_path)
    relfiles = sorted(
        path.relative_to(extract_root).as_posix()
        for path in extract_root.rglob("*")
        if path.is_file()
    )

    assert README_FILENAME in relfiles
    assert MANIFEST_FILENAME in relfiles
    assert "collections/000001.ots.age" in relfiles
    assert "collections/000001.yml.age" in relfiles
    assert "files/000001.age" in relfiles
    assert "files/000001.yml.age" in relfiles
    assert "files/000002.age" in relfiles
    assert "files/000002.yml.age" in relfiles
    assert not any(
        "invoice-123.pdf" in relpath or "receipt-456.pdf" in relpath for relpath in relfiles
    )
    assert all(relpath == README_FILENAME or relpath.endswith(".age") for relpath in relfiles)

    readme = (extract_root / README_FILENAME).read_text(encoding="utf-8")
    assert "arc-disc" in readme
    assert "DISC.yml.age" in readme
    assert "multiple discs" in readme

    manifest = yaml.safe_load(
        fixture_decrypt_bytes((extract_root / MANIFEST_FILENAME).read_bytes()).decode("utf-8")
    )
    assert manifest["schema"] == "disc-manifest/v1"
    assert manifest["image"] == {"id": IMAGE_ID, "volume_id": "ARC-IMG-20260420-01"}
    assert [collection["id"] for collection in manifest["collections"]] == [DOCS_COLLECTION_ID]

    collection = manifest["collections"][0]
    assert collection["manifest"] == "collections/000001.yml.age"
    assert collection["proof"] == "collections/000001.ots.age"
    assert [entry["path"] for entry in collection["files"]] == [
        "/tax/2022/invoice-123.pdf",
        "/tax/2022/receipt-456.pdf",
    ]
    assert collection["files"][0]["object"] == "files/000001.age"
    assert collection["files"][0]["sidecar"] == "files/000001.yml.age"
    assert collection["files"][1]["object"] == "files/000002.age"
    assert collection["files"][1]["sidecar"] == "files/000002.yml.age"

    sidecar = yaml.safe_load(
        fixture_decrypt_bytes(
            (extract_root / collection["files"][0]["sidecar"]).read_bytes()
        ).decode("utf-8")
    )
    assert sidecar["schema"] == "file-sidecar/v1"
    assert sidecar["collection"] == DOCS_COLLECTION_ID
    assert sidecar["path"] == "/tax/2022/invoice-123.pdf"

    payload = fixture_decrypt_bytes((extract_root / collection["files"][0]["object"]).read_bytes())
    assert payload == DOCS_FILES["tax/2022/invoice-123.pdf"]

    collection_manifest = yaml.safe_load(
        fixture_decrypt_bytes((extract_root / collection["manifest"]).read_bytes()).decode("utf-8")
    )
    assert collection_manifest["schema"] == "collection-hash-manifest/v1"
    assert collection_manifest["collection"] == DOCS_COLLECTION_ID
    assert [row["relative_path"] for row in collection_manifest["files"]] == sorted(DOCS_FILES)

    proof = fixture_decrypt_bytes((extract_root / collection["proof"]).read_bytes()).decode("utf-8")
    assert "OpenTimestamps stub proof v1" in proof
    assert "file: HASHES.yml" in proof


def test_register_a_physical_copy(acceptance_system: AcceptanceSystem) -> None:
    acceptance_system.seed_planner_fixtures()
    before = acceptance_system.request("GET", f"/v1/collections/{DOCS_COLLECTION_ID}").json()

    response = acceptance_system.request(
        "POST",
        f"/v1/images/{IMAGE_ID}/copies",
        json_body={"id": "BR-021-A", "location": "Shelf B1"},
    )

    after = acceptance_system.request("GET", f"/v1/collections/{DOCS_COLLECTION_ID}").json()
    assert response.status_code == 200
    assert response.json()["copy"] == {
        "id": "BR-021-A",
        "image": IMAGE_ID,
        "location": "Shelf B1",
        "created_at": "2026-04-20T12:00:00Z",
    }
    assert after["archived_bytes"] > before["archived_bytes"]
    assert after["pending_bytes"] < before["pending_bytes"]


def test_split_file_parts_are_listed_per_disc_and_reconstruct_the_original_plaintext(
    acceptance_system: AcceptanceSystem,
    tmp_path: Path,
) -> None:
    acceptance_system.seed_split_planner_fixtures()

    extracted_parts: list[bytes] = []
    for image_id, expected_index in (
        (SPLIT_IMAGE_ONE_ID, 1),
        (SPLIT_IMAGE_TWO_ID, 2),
    ):
        response = acceptance_system.request("GET", f"/v1/images/{image_id}/iso")

        assert response.status_code == 200
        image_workspace = tmp_path / image_id
        image_workspace.mkdir()
        iso_path = _write_downloaded_iso(response.content, image_workspace)
        _verify_iso(iso_path)
        extract_workspace = tmp_path / f"{image_id}-extract"
        extract_workspace.mkdir()
        extract_root = _extract_iso(iso_path, extract_workspace)
        relfiles = sorted(
            path.relative_to(extract_root).as_posix()
            for path in extract_root.rglob("*")
            if path.is_file()
        )

        assert f"files/000001.00{expected_index}.age" in relfiles
        assert f"files/000001.00{expected_index}.yml.age" in relfiles

        manifest = yaml.safe_load(
            fixture_decrypt_bytes((extract_root / MANIFEST_FILENAME).read_bytes()).decode("utf-8")
        )
        collection = manifest["collections"][0]
        file_entry = collection["files"][0]

        assert file_entry["path"] == f"/{SPLIT_FILE_RELPATH}"
        assert "object" not in file_entry
        assert "sidecar" not in file_entry
        assert file_entry["parts"] == {
            "count": 2,
            "present": [
                {
                    "index": expected_index,
                    "object": f"files/000001.00{expected_index}.age",
                    "sidecar": f"files/000001.00{expected_index}.yml.age",
                }
            ],
        }

        sidecar = yaml.safe_load(
            fixture_decrypt_bytes(
                (extract_root / file_entry["parts"]["present"][0]["sidecar"]).read_bytes()
            ).decode("utf-8")
        )
        assert sidecar["schema"] == "file-sidecar/v1"
        assert sidecar["collection"] == DOCS_COLLECTION_ID
        assert sidecar["path"] == f"/{SPLIT_FILE_RELPATH}"
        assert sidecar["part"] == {"index": expected_index, "count": 2}

        extracted_parts.append(
            fixture_decrypt_bytes(
                (extract_root / file_entry["parts"]["present"][0]["object"]).read_bytes()
            )
        )

    assert tuple(extracted_parts) == SPLIT_FILE_PARTS
    assert b"".join(extracted_parts) == DOCS_FILES[SPLIT_FILE_RELPATH]


def test_reusing_a_copy_id_fails(acceptance_system: AcceptanceSystem) -> None:
    acceptance_system.seed_planner_fixtures()
    first = acceptance_system.request(
        "POST",
        f"/v1/images/{IMAGE_ID}/copies",
        json_body={"id": "BR-021-A", "location": "Shelf B1"},
    )
    assert first.status_code == 200

    response = acceptance_system.request(
        "POST",
        f"/v1/images/{IMAGE_ID}/copies",
        json_body={"id": "BR-021-A", "location": "Shelf B2"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"
