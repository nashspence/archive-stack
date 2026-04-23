from __future__ import annotations

import hashlib
import io
import queue
import tarfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

CHUNK_BYTES = 1024 * 1024
TarMemberPredicate = Callable[[str], bool]


class QueueReader(io.RawIOBase):
    """Bridge an async request stream into the blocking tarfile reader."""

    def __init__(self, *, max_chunks: int = 8) -> None:
        self.queue: queue.Queue[bytes | BaseException | None] = queue.Queue(maxsize=max_chunks)
        self.buffer = bytearray()

    def readable(self) -> bool:
        return True

    def feed(self, data: bytes) -> None:
        self.queue.put(data)

    def finish(self) -> None:
        self.queue.put(None)

    def abort(self, exc: BaseException) -> None:
        self.queue.put(exc)

    def readinto(self, output: bytearray) -> int:
        while not self.buffer:
            item = self.queue.get()
            if item is None:
                return 0
            if isinstance(item, BaseException):
                raise item
            self.buffer.extend(item)

        size = min(len(output), len(self.buffer))
        output[:size] = self.buffer[:size]
        del self.buffer[:size]
        return size


@dataclass(frozen=True)
class TarExtractionResult:
    files: int
    dirs: int
    skipped: int
    bytes_written: int
    manifest_path: Path
    stream_sha256: str | None = None
    stream_bytes: int | None = None


def safe_target(root: Path, member_name: str) -> Path:
    parts = [part for part in PurePosixPath(member_name).parts if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise ValueError(f"unsafe tar path: {member_name}")
    target = (root / Path(*parts)).resolve()
    root_resolved = root.resolve()
    if target != root_resolved and root_resolved not in target.parents:
        raise ValueError(f"unsafe tar path: {member_name}")
    return target


def extract_tar_stream(
    reader: QueueReader,
    dest: Path,
    *,
    allow_member: TarMemberPredicate | None = None,
    write_manifest: bool = True,
) -> TarExtractionResult:
    dest.mkdir(parents=True, exist_ok=True)
    files = 0
    dirs = 0
    skipped = 0
    bytes_written = 0

    manifest_path = dest / "SHA256SUMS.txt"
    with manifest_path.open("w", encoding="utf-8") as manifest:
        with tarfile.open(fileobj=reader, mode="r|*") as archive:
            for member in archive:
                if allow_member is not None and not allow_member(member.name):
                    raise ValueError(f"unexpected tar member: {member.name}")

                target = safe_target(dest, member.name)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    dirs += 1
                    continue

                if not member.isreg():
                    skipped += 1
                    continue

                source = archive.extractfile(member)
                if source is None:
                    skipped += 1
                    continue

                target.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                size = 0
                with source, target.open("wb") as sink:
                    while True:
                        chunk = source.read(CHUNK_BYTES)
                        if not chunk:
                            break
                        sink.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                if write_manifest:
                    manifest.write(f"{digest.hexdigest()}  {member.name}\n")
                files += 1
                bytes_written += size

    return TarExtractionResult(
        files=files,
        dirs=dirs,
        skipped=skipped,
        bytes_written=bytes_written,
        manifest_path=manifest_path,
    )
