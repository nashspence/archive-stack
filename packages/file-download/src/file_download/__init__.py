from __future__ import annotations

import hashlib
import os
import re
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

DownloadProgress = Callable[[int, int], None]


class FileDownloadError(RuntimeError):
    pass


class DownloadDestinationExists(FileDownloadError):
    pass


class DownloadIntegrityError(FileDownloadError):
    pass


@dataclass(frozen=True)
class DownloadReceipt:
    output: Path
    bytes: int
    sha256: str


def verified_download(
    chunks: Iterable[bytes],
    *,
    output: Path,
    expected_bytes: int,
    expected_sha256: str,
    overwrite: bool = False,
    progress: DownloadProgress | None = None,
) -> DownloadReceipt:
    """Stream one exact file into place only after its identity is verified."""

    if expected_bytes < 0:
        raise ValueError("expected_bytes must not be negative")
    normalized_sha256 = expected_sha256.strip().casefold()
    if not SHA256_RE.fullmatch(normalized_sha256):
        raise ValueError("expected_sha256 must be 64 hexadecimal characters")

    output = Path(output)
    if not output.parent.is_dir():
        raise FileDownloadError(f"output parent directory does not exist: {output.parent}")
    if os.path.lexists(output) and not overwrite:
        raise DownloadDestinationExists(f"output already exists: {output}")

    temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.part")
    digest = hashlib.sha256()
    byte_count = 0
    try:
        with temporary.open("xb") as handle:
            for chunk in chunks:
                if not chunk:
                    continue
                handle.write(chunk)
                digest.update(chunk)
                byte_count += len(chunk)
                if byte_count > expected_bytes:
                    raise DownloadIntegrityError(
                        f"download exceeded expected size: {byte_count} > {expected_bytes}"
                    )
                if progress is not None:
                    progress(byte_count, expected_bytes)
            handle.flush()
            os.fsync(handle.fileno())

        actual_sha256 = digest.hexdigest()
        if byte_count != expected_bytes:
            raise DownloadIntegrityError(
                f"download size mismatch: expected {expected_bytes}, received {byte_count}"
            )
        if actual_sha256 != normalized_sha256:
            raise DownloadIntegrityError(
                f"download SHA-256 mismatch: expected {normalized_sha256}, received {actual_sha256}"
            )

        if overwrite:
            os.replace(temporary, output)
        else:
            try:
                os.link(temporary, output)
            except FileExistsError as exc:
                raise DownloadDestinationExists(f"output already exists: {output}") from exc
            temporary.unlink()
        return DownloadReceipt(output=output, bytes=byte_count, sha256=actual_sha256)
    finally:
        temporary.unlink(missing_ok=True)
