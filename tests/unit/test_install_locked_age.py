from __future__ import annotations

import hashlib
import http.client
import importlib.util
import io
import sys
import tarfile
import tomllib
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


def test_locked_downloader_matches_the_repository_http_retry_budget() -> None:
    module = load_script()
    mise = tomllib.loads((REPO_ROOT / "mise.toml").read_text(encoding="utf-8"))

    assert len(module.DOWNLOAD_RETRY_DELAYS_SECONDS) == mise["settings"]["http_retries"]


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


def test_installer_retries_transient_download_failures(tmp_path: Path) -> None:
    module = load_script()
    payload = _age_archive()
    lock = tmp_path / "mise.lock"
    _write_lock(lock, hashlib.sha256(payload).hexdigest())
    destination = tmp_path / "bin"
    attempts = 0
    delays: list[float] = []

    def open_after_transient_failures(_url: str) -> io.BytesIO:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise http.client.RemoteDisconnected("remote closed before responding")
        return io.BytesIO(payload)

    artifact = module.install_locked_age(
        lock,
        destination,
        opener=open_after_transient_failures,
        sleeper=delays.append,
    )

    assert artifact.version == "1.3.1"
    assert attempts == 3
    assert delays == [1.0, 2.0]
    assert {path.name for path in destination.iterdir()} == {
        "age",
        "age-keygen",
        "age-plugin-batchpass",
    }


def test_installer_exhausts_the_bounded_transient_retry_budget(tmp_path: Path) -> None:
    module = load_script()
    lock = tmp_path / "mise.lock"
    _write_lock(lock, "0" * 64)
    attempts = 0
    delays: list[float] = []

    def always_disconnected(_url: str) -> io.BytesIO:
        nonlocal attempts
        attempts += 1
        raise http.client.RemoteDisconnected("remote closed before responding")

    with pytest.raises(http.client.RemoteDisconnected):
        module.install_locked_age(
            lock,
            tmp_path / "bin",
            opener=always_disconnected,
            sleeper=delays.append,
        )

    assert attempts == 1 + len(module.DOWNLOAD_RETRY_DELAYS_SECONDS)
    assert delays == list(module.DOWNLOAD_RETRY_DELAYS_SECONDS)


def test_installer_rejects_bytes_outside_the_lock(tmp_path: Path) -> None:
    module = load_script()
    payload = _age_archive()
    lock = tmp_path / "mise.lock"
    _write_lock(lock, "0" * 64)
    destination = tmp_path / "bin"
    attempts = 0
    delays: list[float] = []

    def open_unlocked_bytes(_url: str) -> io.BytesIO:
        nonlocal attempts
        attempts += 1
        return io.BytesIO(payload)

    with pytest.raises(module.InstallError, match="does not match mise.lock"):
        module.install_locked_age(
            lock,
            destination,
            opener=open_unlocked_bytes,
            sleeper=delays.append,
        )

    assert attempts == 1
    assert delays == []
    assert not destination.exists()
