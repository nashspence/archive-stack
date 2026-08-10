from __future__ import annotations

import hashlib
import importlib.util
import io
import sys
import tarfile
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts/install_locked_age.py"


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("install_locked_age", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _age_archive() -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name in ("age", "age-keygen", "age-plugin-batchpass"):
            body = b"#!/bin/sh\nprintf 'v1.3.1\\n'\n" if name == "age" else b"#!/bin/sh\nexit 0\n"
            member = tarfile.TarInfo(f"age/{name}")
            member.mode = 0o755
            member.size = len(body)
            archive.addfile(member, io.BytesIO(body))
    return buffer.getvalue()


def _write_lock(path: Path, checksum: str) -> None:
    path.write_text(
        "\n".join(
            (
                "[[tools.age]]",
                'version = "1.3.1"',
                '[tools.age."platforms.linux-x64"]',
                f'checksum = "sha256:{checksum}"',
                'url = "https://github.com/FiloSottile/age/releases/download/'
                'v1.3.1/age-v1.3.1-linux-amd64.tar.gz"',
                'provenance = "github-attestations"',
                "",
            )
        ),
        encoding="utf-8",
    )


def test_repository_age_lock_is_the_canonical_verified_linux_artifact() -> None:
    module = load_script()

    artifact = module.load_locked_age(REPO_ROOT / "mise.lock")

    assert artifact == module.LockedArtifact(
        version="1.3.1",
        url=(
            "https://github.com/FiloSottile/age/releases/download/"
            "v1.3.1/age-v1.3.1-linux-amd64.tar.gz"
        ),
        sha256="bdc69c09cbdd6cf8b1f333d372a1f58247b3a33146406333e30c0f26e8f51377",
        provenance="github-attestations",
    )


def test_installer_hashes_then_installs_the_locked_executables(tmp_path: Path) -> None:
    module = load_script()
    payload = _age_archive()
    lock = tmp_path / "mise.lock"
    _write_lock(lock, hashlib.sha256(payload).hexdigest())
    destination = tmp_path / "bin"

    artifact = module.install_locked_age(
        lock,
        destination,
        opener=lambda _url: io.BytesIO(payload),
    )

    assert artifact.version == "1.3.1"
    assert {path.name for path in destination.iterdir()} == {
        "age",
        "age-keygen",
        "age-plugin-batchpass",
    }
    assert all(path.stat().st_mode & 0o111 for path in destination.iterdir())


def test_installer_rejects_bytes_outside_the_lock(tmp_path: Path) -> None:
    module = load_script()
    payload = _age_archive()
    lock = tmp_path / "mise.lock"
    _write_lock(lock, "0" * 64)
    destination = tmp_path / "bin"

    with pytest.raises(module.InstallError, match="does not match mise.lock"):
        module.install_locked_age(
            lock,
            destination,
            opener=lambda _url: io.BytesIO(payload),
        )

    assert not destination.exists()
