from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import jsonschema
import pytest
import yaml

from arc_core.planner.manifest import MANIFEST_FILENAME, README_FILENAME
from tests.fixtures.data import fixture_decrypt_bytes

REPO_ROOT = Path(__file__).resolve().parents[2]
DISC_CONTRACTS_ROOT = REPO_ROOT / "contracts" / "disc"
XORRISO = shutil.which("xorriso")


@dataclass(slots=True)
class InspectedIso:
    image_id: str
    iso_path: Path
    extract_root: Path
    directories: list[str]
    files: list[str]
    readme: str
    disc_manifest: dict[str, Any]


def require_xorriso() -> str:
    if XORRISO is None:
        pytest.skip("xorriso is required for ISO-producing acceptance scenarios")
    return XORRISO


def inspect_downloaded_iso(*, image_id: str, iso_bytes: bytes, workspace: Path) -> InspectedIso:
    require_xorriso()
    image_root, iso_path = _prepare_inspection_workspace(
        image_id=image_id,
        iso_bytes=iso_bytes,
        workspace=workspace,
    )
    _verify_iso(iso_path)

    extract_root = image_root / "disc"
    extract_root.mkdir()
    _extract_iso(iso_path, extract_root)

    return _inspect_extracted_root(
        image_id=image_id,
        iso_path=iso_path,
        extract_root=extract_root,
    )


def inspect_fixture_image_root(
    *,
    image_id: str,
    image_root: Path,
    iso_bytes: bytes,
    workspace: Path,
) -> InspectedIso:
    inspection_root, iso_path = _prepare_inspection_workspace(
        image_id=image_id,
        iso_bytes=iso_bytes,
        workspace=workspace,
    )
    extract_root = inspection_root / "disc"
    shutil.copytree(image_root, extract_root)

    return _inspect_extracted_root(
        image_id=image_id,
        iso_path=iso_path,
        extract_root=extract_root,
    )


def _prepare_inspection_workspace(
    *,
    image_id: str,
    iso_bytes: bytes,
    workspace: Path,
) -> tuple[Path, Path]:
    image_root = workspace / "iso-inspection" / image_id
    if image_root.exists():
        shutil.rmtree(image_root)
    image_root.mkdir(parents=True)

    iso_path = image_root / "image.iso"
    iso_path.write_bytes(iso_bytes)
    return image_root, iso_path


def _inspect_extracted_root(*, image_id: str, iso_path: Path, extract_root: Path) -> InspectedIso:

    files = sorted(
        path.relative_to(extract_root).as_posix()
        for path in extract_root.rglob("*")
        if path.is_file()
    )
    directories = sorted(
        path.relative_to(extract_root).as_posix()
        for path in extract_root.rglob("*")
        if path.is_dir()
    )
    readme = (extract_root / README_FILENAME).read_text(encoding="utf-8")
    disc_manifest = decrypt_yaml_file(extract_root / MANIFEST_FILENAME)
    return InspectedIso(
        image_id=image_id,
        iso_path=iso_path,
        extract_root=extract_root,
        directories=directories,
        files=files,
        readme=readme,
        disc_manifest=disc_manifest,
    )


def assert_root_layout_contract(inspected_iso: InspectedIso) -> None:
    contract = cast(
        dict[str, Any],
        json.loads((DISC_CONTRACTS_ROOT / "root-layout.json").read_text(encoding="utf-8")),
    )
    root_entries = list(inspected_iso.extract_root.iterdir())
    root_files = sorted(path.name for path in root_entries if path.is_file())
    root_directories = sorted(path.name for path in root_entries if path.is_dir())

    assert root_files == sorted(contract["required_root_files"])
    assert root_directories == sorted(contract["required_root_directories"])
    assert inspected_iso.directories == sorted(contract["required_root_directories"])

    patterns = [re.compile(pattern) for pattern in contract["allowed_file_patterns"]]
    unexpected = [
        relpath
        for relpath in inspected_iso.files
        if not any(pattern.fullmatch(relpath) for pattern in patterns)
    ]
    assert not unexpected, f"unexpected ISO file paths: {unexpected}"

    unexpected_plaintext = [
        relpath
        for relpath in inspected_iso.files
        if not relpath.endswith(".age") and relpath not in contract["plaintext_files"]
    ]
    assert not unexpected_plaintext, f"unexpected plaintext ISO files: {unexpected_plaintext}"


def assert_contract_schema(contract_filename: str, payload: object) -> None:
    schema = cast(
        dict[str, Any],
        json.loads((DISC_CONTRACTS_ROOT / contract_filename).read_text(encoding="utf-8")),
    )
    jsonschema.validate(payload, schema)


def decrypt_yaml_file(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(fixture_decrypt_bytes(path.read_bytes()).decode("utf-8"))
    assert isinstance(payload, dict)
    return cast(dict[str, Any], payload)


def payload_bytes(path: Path) -> bytes:
    return fixture_decrypt_bytes(path.read_bytes())


def manifest_entry_by_path(
    disc_manifest: dict[str, Any], relpath: object
) -> tuple[str, dict[str, Any]]:
    raw_path = str(relpath)
    normalized = raw_path if raw_path.startswith("/") else f"/{raw_path.lstrip('/')}"
    for collection in cast(list[dict[str, Any]], disc_manifest["collections"]):
        for file_entry in cast(list[dict[str, Any]], collection["files"]):
            if file_entry["path"] == normalized:
                return str(collection["id"]), file_entry
    raise AssertionError(f"no manifest entry for {normalized}")


def assert_disc_manifest_semantics(disc_manifest: dict[str, Any]) -> None:
    collections = cast(list[dict[str, Any]], disc_manifest["collections"])
    assert [collection["id"] for collection in collections] == sorted(
        collection["id"] for collection in collections
    )
    for collection in collections:
        files = cast(list[dict[str, Any]], collection["files"])
        assert [file_entry["path"] for file_entry in files] == sorted(
            file_entry["path"] for file_entry in files
        )


def assert_collection_manifest_semantics(
    collection_manifest: dict[str, Any],
    *,
    expected_collection_id: str,
    expected_files: list[str],
) -> None:
    assert collection_manifest["collection"] == expected_collection_id
    directories = cast(list[str], collection_manifest["directories"])
    file_rows = cast(list[dict[str, Any]], collection_manifest["files"])
    assert directories == sorted(directories)
    assert [row["relative_path"] for row in file_rows] == expected_files
    total_bytes = sum(int(row["size_bytes"]) for row in file_rows)
    assert int(collection_manifest["tree"]["total_bytes"]) == total_bytes


def assert_sidecar_semantics(
    sidecar: dict[str, Any],
    *,
    expected_collection_id: str,
    expected_path: str,
    expected_bytes: int,
    expected_sha256: str,
    expected_part: dict[str, int] | None,
) -> None:
    assert sidecar["collection"] == expected_collection_id
    assert sidecar["path"] == expected_path
    assert int(sidecar["bytes"]) == expected_bytes
    assert sidecar["sha256"] == expected_sha256
    if expected_part is None:
        assert "part" not in sidecar
        return
    assert sidecar["part"] == expected_part


def _verify_iso(iso_path: Path) -> None:
    proc = subprocess.run(
        [
            require_xorriso(),
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


def _extract_iso(iso_path: Path, extract_root: Path) -> None:
    proc = subprocess.run(
        [
            require_xorriso(),
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
