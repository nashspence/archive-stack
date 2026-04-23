from __future__ import annotations

import importlib
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Annotated, Any

import typer

from arc_cli.client import ApiClient
from arc_cli.output import emit
from arc_core.domain.errors import ArcError

app = typer.Typer(help="arc optical recovery CLI")


@app.callback()
def arc_disc_app() -> None:
    """Keep the CLI in group mode so `arc-disc fetch ...` stays canonical."""


class PlaceholderOpticalReader:
    def read_iter(self, disc_path: str, *, device: str) -> Iterator[bytes]:
        raise NotImplementedError(f"optical read not implemented for {disc_path} on {device}")


@dataclass(frozen=True, slots=True)
class RecoveryCopyHint:
    copy_id: str
    location: str
    disc_path: str
    recovery_bytes: int
    recovery_sha256: str
    enc: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RecoveryPartHint:
    index: int
    bytes: int
    recovery_bytes: int
    copies: tuple[RecoveryCopyHint, ...]


@dataclass(frozen=True, slots=True)
class RecoveryEntry:
    id: str
    path: str
    bytes: int
    recovery_bytes: int
    parts: tuple[RecoveryPartHint, ...]


@dataclass(frozen=True, slots=True)
class UploadSession:
    entry: str
    upload_url: str
    offset: int
    length: int
    checksum_algorithm: str
    expires_at: str | None


def _load_factory(spec: str) -> object:
    module_name, sep, attr_name = spec.partition(":")
    if not sep:
        raise RuntimeError(f"invalid factory spec: {spec!r}")
    factory = getattr(importlib.import_module(module_name), attr_name)
    if not callable(factory):
        raise RuntimeError(f"factory must be callable: {spec!r}")
    return factory()


def build_optical_reader() -> object:
    spec = os.getenv("ARC_DISC_READER_FACTORY")
    if spec:
        return _load_factory(spec)
    return PlaceholderOpticalReader()


def _copy_from_manifest(payload: dict[str, Any]) -> RecoveryCopyHint:
    return RecoveryCopyHint(
        copy_id=str(payload["copy"]),
        location=str(payload["location"]),
        disc_path=str(payload["disc_path"]),
        recovery_bytes=int(payload.get("recovery_bytes", payload.get("bytes", 0))),
        recovery_sha256=str(payload.get("recovery_sha256", "")),
        enc=dict(payload["enc"]),
    )


def _part_from_manifest(payload: dict[str, Any]) -> RecoveryPartHint:
    copies = tuple(_copy_from_manifest(copy) for copy in payload.get("copies", []))
    if not copies:
        raise RuntimeError("fetch manifest part is missing copy hints")
    return RecoveryPartHint(
        index=int(payload["index"]),
        bytes=int(payload["bytes"]),
        recovery_bytes=int(payload.get("recovery_bytes", payload["bytes"])),
        copies=copies,
    )


def _entry_from_manifest(payload: dict[str, Any]) -> RecoveryEntry:
    manifest_parts = payload.get("parts")
    if manifest_parts:
        parts = tuple(
            _part_from_manifest(part)
            for part in sorted(manifest_parts, key=lambda item: int(item["index"]))
        )
    else:
        copies = tuple(_copy_from_manifest(copy) for copy in payload.get("copies", []))
        if not copies:
            raise RuntimeError(f"fetch manifest entry is missing copy hints: {payload['id']}")
        parts = (
            RecoveryPartHint(
                index=0,
                bytes=int(payload["bytes"]),
                recovery_bytes=int(payload.get("recovery_bytes", payload["bytes"])),
                copies=copies,
            ),
        )
    return RecoveryEntry(
        id=str(payload["id"]),
        path=str(payload["path"]),
        bytes=int(payload["bytes"]),
        recovery_bytes=int(payload.get("recovery_bytes", payload["bytes"])),
        parts=parts,
    )


def _upload_session_from_payload(entry: RecoveryEntry, payload: dict[str, Any]) -> UploadSession:
    if str(payload.get("entry")) != entry.id:
        raise RuntimeError(f"upload session entry mismatch for {entry.path}")
    if str(payload.get("protocol")) != "tus":
        raise RuntimeError(f"upload session protocol is not tus for {entry.path}")
    if int(payload.get("length", -1)) != entry.recovery_bytes:
        raise RuntimeError(f"upload session length mismatch for {entry.path}")
    offset = int(payload.get("offset", -1))
    if offset < 0 or offset > entry.recovery_bytes:
        raise RuntimeError(f"upload session offset is invalid for {entry.path}")
    return UploadSession(
        entry=entry.id,
        upload_url=str(payload["upload_url"]),
        offset=offset,
        length=entry.recovery_bytes,
        checksum_algorithm=str(payload["checksum_algorithm"]),
        expires_at=str(payload["expires_at"]) if payload.get("expires_at") is not None else None,
    )


def _prompt_for_disc(copy: RecoveryCopyHint, *, device: str) -> None:
    typer.echo(
        (
            f"Insert disc {copy.copy_id} from {copy.location} into {device}, "
            "then press Enter to continue."
        ),
        err=True,
    )
    try:
        input()
    except EOFError as exc:  # pragma: no cover - exercised via subprocess acceptance tests
        raise RuntimeError("stdin closed while waiting for disc insertion") from exc


@dataclass(slots=True)
class ProgressReporter:
    entries: tuple[RecoveryEntry, ...]
    started_at: float
    uploaded_bytes_by_entry: dict[str, int] = field(default_factory=dict)
    uploaded_manifest_bytes: int = 0

    @classmethod
    def begin(
        cls,
        entries: tuple[RecoveryEntry, ...],
        *,
        uploaded_bytes_by_entry: dict[str, int] | None = None,
    ) -> ProgressReporter:
        uploaded_bytes_by_entry = dict(uploaded_bytes_by_entry or {})
        return cls(
            entries=entries,
            started_at=time.monotonic(),
            uploaded_bytes_by_entry=uploaded_bytes_by_entry,
            uploaded_manifest_bytes=sum(uploaded_bytes_by_entry.values()),
        )

    @property
    def manifest_total_bytes(self) -> int:
        return sum(entry.recovery_bytes for entry in self.entries)

    def record_uploaded_bytes(self, entry: RecoveryEntry, byte_count: int) -> None:
        self.uploaded_bytes_by_entry[entry.id] = (
            self.uploaded_bytes_by_entry.get(entry.id, 0) + byte_count
        )
        self.uploaded_manifest_bytes += byte_count

    def report(self, entry: RecoveryEntry) -> None:
        entry_total = max(entry.recovery_bytes, 1)
        manifest_total = max(self.manifest_total_bytes, 1)
        entry_percent = (self.uploaded_bytes_by_entry.get(entry.id, 0) / entry_total) * 100
        manifest_percent = (self.uploaded_manifest_bytes / manifest_total) * 100
        elapsed = max(time.monotonic() - self.started_at, 0.001)
        rate = self.uploaded_manifest_bytes / elapsed
        typer.echo(
            (
                f"current file {entry.path}: {entry_percent:.1f}% | "
                f"manifest: {manifest_percent:.1f}% | rate: {rate:.1f} B/s"
            ),
            err=True,
        )


def _iter_recovered_chunks(reader: Any, copy: RecoveryCopyHint, *, device: str) -> Iterator[bytes]:
    if hasattr(reader, "read_iter"):
        yield from reader.read_iter(copy.disc_path, device=device)
        return
    yield reader.read(copy.disc_path, device=device)


def _skip_uploaded_prefix(chunks: Iterator[bytes], *, skip_bytes: int) -> Iterator[bytes]:
    remaining = skip_bytes
    for chunk in chunks:
        if not chunk:
            continue
        if remaining >= len(chunk):
            remaining -= len(chunk)
            continue
        if remaining > 0:
            chunk = chunk[remaining:]
            remaining = 0
        yield chunk


def _upload_entry_from_disc(
    entry: RecoveryEntry,
    session: UploadSession,
    *,
    client: ApiClient,
    reader: Any,
    device: str,
    progress: ProgressReporter,
) -> None:
    offset = session.offset
    part_start = 0

    for part in entry.parts:
        part_end = part_start + part.recovery_bytes
        if offset >= part_end:
            part_start = part_end
            continue

        copy = part.copies[0]
        _prompt_for_disc(copy, device=device)
        resume_within_part = max(offset - part_start, 0)
        recovered_chunks = _skip_uploaded_prefix(
            _iter_recovered_chunks(reader, copy, device=device),
            skip_bytes=resume_within_part,
        )
        for chunk in recovered_chunks:
            if not chunk:
                continue
            upload_result = client.append_upload_chunk(
                session.upload_url,
                offset=offset,
                checksum_algorithm=session.checksum_algorithm,
                content=chunk,
            )
            next_offset = int(upload_result["offset"])
            uploaded_bytes = next_offset - offset
            if uploaded_bytes != len(chunk):
                raise RuntimeError(f"upload offset advanced unexpectedly for {entry.path}")
            offset = next_offset
            progress.record_uploaded_bytes(entry, uploaded_bytes)
            progress.report(entry)

        part_start = part_end

    if offset != entry.recovery_bytes:
        raise RuntimeError(
            f"upload for {entry.path} stopped at {offset} of {entry.recovery_bytes} bytes"
        )


@app.command("fetch")
def fetch_cmd(
    fetch_id: Annotated[str, typer.Argument(help="Fetch id")],
    device: Annotated[str, typer.Option("--device", help="Optical device path")] = "/dev/sr0",
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    try:
        client = ApiClient()
        manifest = client.get_fetch_manifest(fetch_id)
        reader = build_optical_reader()
        entries = tuple(_entry_from_manifest(entry) for entry in manifest.get("entries", []))
        sessions = {
            entry.id: _upload_session_from_payload(
                entry,
                client.create_or_resume_fetch_entry_upload(fetch_id, entry.id),
            )
            for entry in entries
        }
        progress = ProgressReporter.begin(
            entries,
            uploaded_bytes_by_entry={entry.id: sessions[entry.id].offset for entry in entries},
        )
        for entry in entries:
            _upload_entry_from_disc(
                entry,
                sessions[entry.id],
                client=client,
                reader=reader,
                device=device,
                progress=progress,
            )

        payload = client.complete_fetch(fetch_id)
    except (ArcError, RuntimeError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    emit(payload, json_mode=json_mode)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
