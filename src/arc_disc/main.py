from __future__ import annotations

import hashlib
import importlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import typer

from arc_cli.client import ApiClient
from arc_cli.output import emit

app = typer.Typer(help="arc optical recovery CLI")


@app.callback()
def arc_disc_app() -> None:
    """Keep the CLI in group mode so `arc-disc fetch ...` stays canonical."""


class PlaceholderOpticalReader:
    def read(self, disc_path: str, *, device: str) -> bytes:
        raise NotImplementedError(f"optical read not implemented for {disc_path} on {device}")


class PlaceholderCrypto:
    def decrypt_entry(self, encrypted: bytes, enc: dict[str, Any]) -> bytes:
        raise NotImplementedError("entry decryption is not implemented")


@dataclass(frozen=True, slots=True)
class RecoveryCopyHint:
    copy_id: str
    location: str
    disc_path: str
    enc: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RecoveryPartHint:
    index: int
    bytes: int
    sha256: str
    copies: tuple[RecoveryCopyHint, ...]


@dataclass(frozen=True, slots=True)
class RecoveryEntry:
    id: str
    path: str
    bytes: int
    sha256: str
    parts: tuple[RecoveryPartHint, ...]


STATE_FILENAME = ".arc-disc-state.json"


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


def build_crypto() -> object:
    spec = os.getenv("ARC_DISC_CRYPTO_FACTORY")
    if spec:
        return _load_factory(spec)
    return PlaceholderCrypto()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _copy_from_manifest(payload: dict[str, Any]) -> RecoveryCopyHint:
    return RecoveryCopyHint(
        copy_id=str(payload["copy"]),
        location=str(payload["location"]),
        disc_path=str(payload["disc_path"]),
        enc=dict(payload["enc"]),
    )


def _part_from_manifest(payload: dict[str, Any]) -> RecoveryPartHint:
    copies = tuple(_copy_from_manifest(copy) for copy in payload.get("copies", []))
    if not copies:
        raise RuntimeError("fetch manifest part is missing copy hints")
    return RecoveryPartHint(
        index=int(payload["index"]),
        bytes=int(payload["bytes"]),
        sha256=str(payload["sha256"]),
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
                sha256=str(payload["sha256"]),
                copies=copies,
            ),
        )
    return RecoveryEntry(
        id=str(payload["id"]),
        path=str(payload["path"]),
        bytes=int(payload["bytes"]),
        sha256=str(payload["sha256"]),
        parts=parts,
    )


def _prepare_state_dir(state_dir: Path, *, fetch_id: str, manifest: dict[str, Any]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / STATE_FILENAME
    expected = {
        "fetch_id": fetch_id,
        "manifest_id": str(manifest["id"]),
        "target": str(manifest["target"]),
    }
    if state_path.exists():
        existing = json.loads(state_path.read_text(encoding="utf-8"))
        if any(existing.get(key) != value for key, value in expected.items()):
            raise RuntimeError(
                f"state directory {state_dir} belongs to a different fetch and cannot be reused"
            )
        return
    state_path.write_text(json.dumps(expected, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _part_path(state_dir: Path, entry_id: str, part_index: int) -> Path:
    return state_dir / "parts" / entry_id / f"{part_index:06d}.part"


def _read_valid_staged_part(
    state_dir: Path,
    entry: RecoveryEntry,
    part: RecoveryPartHint,
) -> bytes | None:
    path = _part_path(state_dir, entry.id, part.index)
    if not path.is_file():
        return None
    data = path.read_bytes()
    if len(data) != part.bytes or _sha256_bytes(data) != part.sha256:
        path.unlink()
        return None
    return data


def _write_part(
    state_dir: Path,
    entry: RecoveryEntry,
    part: RecoveryPartHint,
    plaintext: bytes,
) -> None:
    if len(plaintext) != part.bytes or _sha256_bytes(plaintext) != part.sha256:
        raise RuntimeError(
            f"recovered part {part.index} for {entry.path} did not match the fetch manifest"
        )
    path = _part_path(state_dir, entry.id, part.index)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_bytes(plaintext)
    tmp_path.replace(path)


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


def _recover_pending_parts(
    entries: tuple[RecoveryEntry, ...],
    *,
    state_dir: Path,
    reader: Any,
    crypto: Any,
    device: str,
) -> None:
    pending_by_copy: dict[
        str,
        tuple[RecoveryCopyHint, list[tuple[RecoveryEntry, RecoveryPartHint]]],
    ] = {}
    copy_order: list[str] = []

    for entry in entries:
        for part in entry.parts:
            if _read_valid_staged_part(state_dir, entry, part) is not None:
                continue
            copy = part.copies[0]
            bucket = pending_by_copy.get(copy.copy_id)
            if bucket is None:
                bucket = (copy, [])
                pending_by_copy[copy.copy_id] = bucket
                copy_order.append(copy.copy_id)
            bucket[1].append((entry, part))

    for copy_id in copy_order:
        copy, items = pending_by_copy[copy_id]
        _prompt_for_disc(copy, device=device)
        for entry, part in items:
            if _read_valid_staged_part(state_dir, entry, part) is not None:
                continue
            encrypted = reader.read(copy.disc_path, device=device)
            plaintext = crypto.decrypt_entry(encrypted, copy.enc)
            _write_part(state_dir, entry, part, plaintext)


def _reconstruct_entry(state_dir: Path, entry: RecoveryEntry) -> bytes:
    parts: list[bytes] = []
    for part in entry.parts:
        plaintext = _read_valid_staged_part(state_dir, entry, part)
        if plaintext is None:
            raise RuntimeError(f"missing recovered part {part.index} for {entry.path}")
        parts.append(plaintext)
    plaintext = b"".join(parts)
    if len(plaintext) != entry.bytes or _sha256_bytes(plaintext) != entry.sha256:
        raise RuntimeError(
            f"reconstructed plaintext for {entry.path} did not match the fetch manifest"
        )
    return plaintext


@app.command("fetch")
def fetch_cmd(
    fetch_id: Annotated[str, typer.Argument(help="Fetch id")],
    state_dir: Annotated[Path, typer.Option("--state-dir", help="Local recovery state directory")],
    device: Annotated[str, typer.Option("--device", help="Optical device path")] = "/dev/sr0",
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    client = ApiClient()
    manifest = client.get_fetch_manifest(fetch_id)
    reader = build_optical_reader()
    crypto = build_crypto()
    entries = tuple(_entry_from_manifest(entry) for entry in manifest.get("entries", []))

    _prepare_state_dir(state_dir, fetch_id=fetch_id, manifest=manifest)
    _recover_pending_parts(
        entries,
        state_dir=state_dir,
        reader=reader,
        crypto=crypto,
        device=device,
    )

    for entry in entries:
        plaintext = _reconstruct_entry(state_dir, entry)
        client.upload_fetch_entry(fetch_id, entry.id, entry.sha256, plaintext)

    payload = client.complete_fetch(fetch_id)
    emit(payload, json_mode=json_mode)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
