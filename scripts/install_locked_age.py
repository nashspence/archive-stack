from __future__ import annotations

import argparse
import hashlib
import shutil
import stat
import subprocess
import tarfile
import tempfile
import tomllib
import urllib.request
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, cast

AGE_PLATFORM = "platforms.linux-x64"
AGE_BINARIES = ("age", "age-keygen", "age-plugin-batchpass")


@dataclass(frozen=True, slots=True)
class LockedArtifact:
    version: str
    url: str
    sha256: str
    provenance: str


class InstallError(RuntimeError):
    """A locked build input is absent, invalid, or fails verification."""


def load_locked_age(lock_path: Path) -> LockedArtifact:
    lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    entries = lock.get("tools", {}).get("age")
    if not isinstance(entries, list) or len(entries) != 1:
        raise InstallError("mise.lock must contain exactly one age tool")
    entry = entries[0]
    platform = entry.get(AGE_PLATFORM)
    if not isinstance(platform, dict):
        raise InstallError(f"mise.lock has no age {AGE_PLATFORM} artifact")
    checksum = str(platform.get("checksum", ""))
    algorithm, separator, digest = checksum.partition(":")
    if algorithm != "sha256" or not separator or len(digest) != 64:
        raise InstallError("locked age artifact needs one SHA-256 digest")
    if any(character not in "0123456789abcdef" for character in digest):
        raise InstallError("locked age SHA-256 digest is not lowercase hexadecimal")
    artifact = LockedArtifact(
        version=str(entry.get("version", "")),
        url=str(platform.get("url", "")),
        sha256=digest,
        provenance=str(platform.get("provenance", "")),
    )
    expected_url = (
        "https://github.com/FiloSottile/age/releases/download/"
        f"v{artifact.version}/age-v{artifact.version}-linux-amd64.tar.gz"
    )
    if artifact.provenance != "github-attestations" or artifact.url != expected_url:
        raise InstallError("locked age artifact lacks its canonical upstream provenance")
    return artifact


def _download(
    artifact: LockedArtifact,
    destination: Path,
    *,
    opener: Callable[[str], AbstractContextManager[BinaryIO]],
) -> None:
    digest = hashlib.sha256()
    with opener(artifact.url) as response, destination.open("wb") as output:
        while chunk := response.read(1024 * 1024):
            digest.update(chunk)
            output.write(chunk)
    if digest.hexdigest() != artifact.sha256:
        raise InstallError("downloaded age artifact does not match mise.lock")


def install_locked_age(
    lock_path: Path,
    destination: Path,
    *,
    opener: Callable[[str], AbstractContextManager[BinaryIO]] | None = None,
) -> LockedArtifact:
    artifact = load_locked_age(lock_path)
    if opener is None:
        opener = cast(
            Callable[[str], AbstractContextManager[BinaryIO]],
            lambda url: urllib.request.urlopen(url, timeout=60),
        )
    with tempfile.TemporaryDirectory(prefix="riverhog-age-install.") as temporary:
        scratch = Path(temporary)
        archive_path = scratch / "age.tar.gz"
        _download(artifact, archive_path, opener=opener)
        unpacked = scratch / "unpacked"
        unpacked.mkdir()
        with tarfile.open(archive_path, mode="r:gz") as archive:
            archive.extractall(unpacked, filter="data")
        verified = scratch / "verified"
        verified.mkdir()
        for name in AGE_BINARIES:
            source = unpacked / "age" / name
            if not source.exists() or not stat.S_ISREG(source.lstat().st_mode):
                raise InstallError(f"verified age archive has no regular age/{name}")
            target = verified / name
            shutil.copyfile(source, target)
            target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        completed = subprocess.run(
            [str(verified / "age"), "--version"],
            check=True,
            capture_output=True,
            text=True,
        )
        if completed.stdout.strip() != f"v{artifact.version}":
            raise InstallError("installed age binary differs from the locked version")
        destination.mkdir(parents=True, exist_ok=True)
        for name in AGE_BINARIES:
            shutil.copy2(verified / name, destination / name)
    return artifact


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install the provenance- and checksum-locked Linux age toolchain."
    )
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    install_locked_age(args.lock, args.destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
