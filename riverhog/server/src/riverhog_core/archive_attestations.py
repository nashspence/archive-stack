from __future__ import annotations

import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from riverhog_core.ports.archive_store import ArchiveObjectIdentity

ATTESTATION_OBJECT_IDS = ("checksums", "signature", "signature-proof")
ATTESTATION_OBJECT_KINDS = frozenset(ATTESTATION_OBJECT_IDS)
ATTESTATION_FILENAMES = {
    "checksums": "SHA256SUMS",
    "signature": "SHA256SUMS.minisig",
    "signature-proof": "SHA256SUMS.minisig.ots",
}
ATTESTED_KINDS = frozenset({"pack", "segment", "manifest", "proof"})
TRUSTED_COMMENT = "riverhog archive-copy attestation/v1"


class AttestationSignError(RuntimeError):
    pass


class AttestationVerifyError(RuntimeError):
    pass


class AttestationSigner(Protocol):
    def sign(self, checksums: bytes) -> bytes: ...


class AttestationVerifier(Protocol):
    def verify(self, *, checksums: bytes, signature: bytes) -> None: ...


def archive_copy_checksums(
    *,
    archive_storage_prefix: str,
    objects: Sequence[ArchiveObjectIdentity],
) -> bytes:
    prefix = f"{archive_storage_prefix.strip('/')}/"
    rows: list[tuple[str, str]] = []
    for current in objects:
        if current.kind not in ATTESTED_KINDS:
            continue
        if not current.stored_sha256 or len(current.stored_sha256) != 64:
            raise ValueError(f"archive object has no stored sha256: {current.object_id}")
        if not current.object_path.startswith(prefix):
            raise ValueError(f"archive object is outside its copy: {current.object_id}")
        relative_path = current.object_path.removeprefix(prefix)
        if not relative_path or "\n" in relative_path or "\r" in relative_path:
            raise ValueError(f"archive object path is not attestable: {current.object_id}")
        rows.append((relative_path, current.stored_sha256))
    if not rows:
        raise ValueError("archive copy has no immutable objects to attest")
    return "".join(f"{sha256}  {relative_path}\n" for relative_path, sha256 in sorted(rows)).encode(
        "utf-8"
    )


@dataclass(frozen=True, slots=True)
class CommandAttestationSigner:
    secret_key_file: Path
    command: tuple[str, ...] = ("minisign",)

    def sign(self, checksums: bytes) -> bytes:
        if not self.command:
            raise AttestationSignError("attestation signing command is empty")
        with tempfile.TemporaryDirectory(prefix="riverhog-attestation-sign-") as tmp:
            root = Path(tmp)
            checksums_path = root / "SHA256SUMS"
            signature_path = root / "SHA256SUMS.minisig"
            checksums_path.write_bytes(checksums)
            proc = subprocess.run(
                [
                    *self.command,
                    "-S",
                    "-H",
                    "-s",
                    str(self.secret_key_file),
                    "-m",
                    str(checksums_path),
                    "-x",
                    str(signature_path),
                    "-t",
                    TRUSTED_COMMENT,
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            if proc.returncode != 0:
                raise AttestationSignError(
                    proc.stderr.strip() or proc.stdout.strip() or "attestation signing failed"
                )
            if not signature_path.is_file():
                raise AttestationSignError("attestation signing produced no signature")
            return signature_path.read_bytes()


@dataclass(frozen=True, slots=True)
class CommandAttestationVerifier:
    public_key_file: Path
    command: tuple[str, ...] = ("minisign",)

    def verify(self, *, checksums: bytes, signature: bytes) -> None:
        if not self.command:
            raise AttestationVerifyError("attestation verification command is empty")
        with tempfile.TemporaryDirectory(prefix="riverhog-attestation-verify-") as tmp:
            root = Path(tmp)
            checksums_path = root / "SHA256SUMS"
            signature_path = root / "SHA256SUMS.minisig"
            checksums_path.write_bytes(checksums)
            signature_path.write_bytes(signature)
            proc = subprocess.run(
                [
                    *self.command,
                    "-V",
                    "-H",
                    "-q",
                    "-p",
                    str(self.public_key_file),
                    "-m",
                    str(checksums_path),
                    "-x",
                    str(signature_path),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            if proc.returncode != 0:
                raise AttestationVerifyError(
                    proc.stderr.strip()
                    or proc.stdout.strip()
                    or "attestation signature verification failed"
                )


__all__ = [
    "ATTESTATION_FILENAMES",
    "ATTESTATION_OBJECT_IDS",
    "ATTESTATION_OBJECT_KINDS",
    "AttestationSigner",
    "AttestationVerifier",
    "CommandAttestationSigner",
    "CommandAttestationVerifier",
    "archive_copy_checksums",
]
