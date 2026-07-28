from __future__ import annotations

import hashlib
import io
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
import yaml
from riverhog_age import encrypt_age_scrypt
from riverhog_recover import RecoveryError, recover_archive

PASSPHRASE = "correct horse battery archive"
OFFICIAL_AGE = shutil.which("age")
OFFICIAL_BATCHPASS = shutil.which("age-plugin-batchpass")

pytestmark = pytest.mark.skipif(
    OFFICIAL_AGE is None or OFFICIAL_BATCHPASS is None,
    reason="official age and age-plugin-batchpass are required",
)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _pack(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:") as archive:
        for path, content in files.items():
            info = tarfile.TarInfo(path)
            info.size = len(content)
            info.mode = 0o600
            archive.addfile(info, io.BytesIO(content))
    return output.getvalue()


def _write_archive(root: Path) -> dict[str, bytes]:
    expected = {
        "notes/alpha.txt": b"alpha\n",
        "notes/beta.txt": b"beta\n",
        "video.bin": b"first-second",
    }
    pack = _pack({path: expected[path] for path in sorted(expected) if path.startswith("notes/")})
    objects = [pack, b"first-", b"second"]
    object_rows = [
        {"id": "data-000000", "kind": "pack", "bytes": len(pack), "sha256": _sha256(pack)},
        {
            "id": "data-000001",
            "kind": "segment",
            "bytes": len(objects[1]),
            "sha256": _sha256(objects[1]),
        },
        {
            "id": "data-000002",
            "kind": "segment",
            "bytes": len(objects[2]),
            "sha256": _sha256(objects[2]),
        },
    ]
    file_rows = [
        {
            "path": path,
            "bytes": len(content),
            "sha256": _sha256(content),
            "objects": [
                {
                    "object": "data-000000",
                    "offset": 0,
                    "bytes": len(content),
                    "member": path,
                }
            ],
        }
        for path, content in sorted(expected.items())
        if path.startswith("notes/")
    ]
    file_rows.append(
        {
            "path": "video.bin",
            "bytes": len(expected["video.bin"]),
            "sha256": _sha256(expected["video.bin"]),
            "objects": [
                {"object": "data-000001", "offset": 0, "bytes": len(objects[1])},
                {
                    "object": "data-000002",
                    "offset": len(objects[1]),
                    "bytes": len(objects[2]),
                },
            ],
        }
    )
    tree = hashlib.sha256()
    for row in file_rows:
        tree.update(f"{row['path']}\t{row['bytes']}\t{row['sha256']}\n".encode())
    manifest = yaml.safe_dump(
        {
            "schema": "collection-archive-manifest/v2",
            "tree": {
                "sha256": tree.hexdigest(),
                "total_bytes": sum(len(content) for content in expected.values()),
            },
            "objects": object_rows,
            "files": file_rows,
        },
        sort_keys=False,
    ).encode()

    object_dir = root / "objects"
    object_dir.mkdir(parents=True)
    ciphertext: dict[str, bytes] = {
        "manifest.yml.age": encrypt_age_scrypt(manifest, PASSPHRASE, log_n=1),
        **{
            f"objects/data-{index:06d}.age": encrypt_age_scrypt(
                content,
                PASSPHRASE,
                log_n=1,
            )
            for index, content in enumerate(objects)
        },
    }
    for relative, content in ciphertext.items():
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    checksum_lines = (
        f"{_sha256(content)}  {relative}\n" for relative, content in sorted(ciphertext.items())
    )
    (root / "SHA256SUMS").write_text(
        "".join(checksum_lines),
        encoding="utf-8",
    )
    return expected


def test_recovers_complete_collection_without_server_or_database(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    expected = _write_archive(archive)
    output = tmp_path / "recovered"

    summary = recover_archive(archive, output, passphrase=PASSPHRASE)

    assert summary.files == len(expected)
    assert summary.bytes == sum(len(content) for content in expected.values())
    assert {path: (output / path).read_bytes() for path in expected} == expected


def test_cli_recovers_with_permission_restricted_passphrase_file(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    expected = _write_archive(archive)
    output = tmp_path / "recovered"
    passphrase_file = tmp_path / "passphrase"
    passphrase_file.write_text(PASSPHRASE, encoding="utf-8")
    passphrase_file.chmod(0o600)

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "riverhog_recover.cli",
            str(archive),
            str(output),
            "--passphrase-file",
            str(passphrase_file),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "Recovered 3 files" in completed.stdout
    assert {path: (output / path).read_bytes() for path in expected} == expected


def test_ciphertext_corruption_fails_without_publishing_partial_output(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    _write_archive(archive)
    damaged = archive / "objects/data-000001.age"
    damaged.write_bytes(damaged.read_bytes() + b"damage")
    output = tmp_path / "recovered"

    with pytest.raises(RecoveryError, match="ciphertext checksum mismatch"):
        recover_archive(archive, output, passphrase=PASSPHRASE)

    assert not output.exists()
