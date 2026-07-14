from __future__ import annotations

import hashlib
import importlib
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, cast

import typer

from riverhog_cli.client import ApiClient
from riverhog_cli.output import (
    emit,
    format_archive_restore,
    format_archive_restores,
    format_disc,
    format_discs,
    format_image,
    format_images,
    format_plan,
)
from riverhog_core.domain.errors import HashMismatch, NotFound, RiverhogError

app = typer.Typer(help="Riverhog optical media CLI.")
image_app = typer.Typer(help="Image planning and download operations.")
disc_app = typer.Typer(help="Burned disc catalog operations.")
disc_rebuild_app = typer.Typer(help="Disc rebuild operations.")
app.add_typer(image_app, name="image")
app.add_typer(disc_app, name="disc")
disc_app.add_typer(disc_rebuild_app, name="rebuild")


@app.callback()
def djdan_app() -> None:
    """Keep the CLI in group mode so `djdan fetch ...` and groups stay canonical."""


_DISC_IO_CHUNK_BYTES = 1024 * 1024
_DOWNLOAD_PROGRESS_INTERVAL_SECONDS = 10.0
_VERIFY_PROGRESS_INTERVAL_SECONDS = 10.0
_LOCAL_STAGE_HEARTBEAT_INTERVAL_SECONDS = 30.0
_DISC_REBUILD_REASONS = {"lost", "damaged"}


def _elapsed_text(started_at: float) -> str:
    elapsed = time.monotonic() - started_at
    if elapsed < 60:
        return f"{elapsed:.1f}s"
    minutes = int(elapsed // 60)
    seconds = int(elapsed % 60)
    return f"{minutes}m {seconds:02d}s"


def _file_entry_text(file_count: int | None) -> str:
    if file_count is None:
        return "the file entries on this image"
    return f"{file_count:,} file entries"


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
    return None


def _require_tool(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise RuntimeError(f"{name} is required for optical media I/O")
    return executable


def _run_checked(command: list[str], *, action: str) -> None:
    completed = threading.Event()
    heartbeat_printed = False
    started_at = time.monotonic()

    def heartbeat() -> None:
        nonlocal heartbeat_printed
        while not completed.wait(_LOCAL_STAGE_HEARTBEAT_INTERVAL_SECONDS):
            heartbeat_printed = True
            typer.echo(
                f"{action} still running after {_elapsed_text(started_at)}; "
                "large images and many file entries can take some time",
                err=True,
            )

    heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
    heartbeat_thread.start()
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        completed.set()
        raise RuntimeError(f"{command[0]} is required for {action}") from exc
    finally:
        completed.set()
        heartbeat_thread.join(timeout=1)
    if proc.returncode == 0:
        if heartbeat_printed:
            typer.echo(f"{action} completed in {_elapsed_text(started_at)}", err=True)
        return
    detail = ((proc.stderr or proc.stdout).strip() or f"{command[0]} exited {proc.returncode}")[
        -1500:
    ]
    raise RuntimeError(f"{action} failed: {detail}")


def _run_passthrough_checked(command: list[str], *, action: str) -> None:
    try:
        proc = subprocess.run(command, check=False)
    except FileNotFoundError as exc:
        raise RuntimeError(f"{command[0]} is required for {action}") from exc
    if proc.returncode == 0:
        return
    raise RuntimeError(f"{action} failed: {command[0]} exited {proc.returncode}")


def _run_captured(command: list[str], *, action: str) -> str:
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"{command[0]} is required for {action}") from exc
    if proc.returncode == 0:
        return proc.stdout
    detail = ((proc.stderr or proc.stdout).strip() or f"{command[0]} exited {proc.returncode}")[
        -1500:
    ]
    raise RuntimeError(f"{action} failed: {detail}")


def _safe_disc_relative_path(disc_path: str) -> PurePosixPath:
    path = PurePosixPath(disc_path)
    parts = tuple(part for part in path.parts if part not in {"", "/"})
    if not parts or any(part in {".", ".."} for part in parts):
        raise RuntimeError(f"unsafe optical media path: {disc_path}")
    return PurePosixPath(*parts)


def _iter_file_chunks(path: Path) -> Iterator[bytes]:
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(_DISC_IO_CHUNK_BYTES)
            if not chunk:
                return
            yield chunk


class XorrisoOpticalReader:
    def read_iter(self, disc_path: str, *, device: str) -> Iterator[bytes]:
        relative_path = _safe_disc_relative_path(disc_path)
        device_path = Path(device)
        if device_path.is_dir():
            mounted_path = device_path.joinpath(*relative_path.parts).resolve()
            mount_root = device_path.resolve()
            if mounted_path != mount_root and mount_root not in mounted_path.parents:
                raise RuntimeError(f"unsafe mounted optical media path: {disc_path}")
            if not mounted_path.is_file():
                raise RuntimeError(f"optical media file is missing: {mounted_path}")
            yield from _iter_file_chunks(mounted_path)
            return

        xorriso = _require_tool("xorriso")
        with tempfile.TemporaryDirectory(prefix="djdan-read-") as temp_root:
            output_path = Path(temp_root) / relative_path.name
            _run_checked(
                [
                    xorriso,
                    "-osirrox",
                    "on",
                    "-indev",
                    device,
                    "-extract",
                    f"/{relative_path.as_posix()}",
                    str(output_path),
                ],
                action=f"extracting {disc_path} from {device}",
            )
            yield from _iter_file_chunks(output_path)


@dataclass(frozen=True, slots=True)
class RecoveryDiscHint:
    disc_id: str
    location: str
    disc_path: str
    recovery_bytes: int
    recovery_sha256: str


@dataclass(frozen=True, slots=True)
class RecoveryPartHint:
    index: int
    bytes: int
    recovery_bytes: int
    discs: tuple[RecoveryDiscHint, ...]


@dataclass(frozen=True, slots=True)
class RecoveryEntry:
    id: str
    collection_id: str
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


class BurnedMediaVerificationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BurnBacklogItem:
    image_id: str | None
    candidate_id: str | None
    filename: str
    fill: float
    expected_bytes: int | None = None
    target_bytes: int | None = None
    archive_restore_id: str | None = None


@dataclass(frozen=True, slots=True)
class RecoveryHandoff:
    image_id: str
    restore_id: str
    state: str
    latest_message: str | None


@dataclass(frozen=True, slots=True)
class ArchiveRestoreImageHint:
    image_id: str
    filename: str


@dataclass(frozen=True, slots=True)
class ArchiveRestoreHint:
    restore_id: str
    type: str
    state: str
    latest_message: str | None
    images: tuple[ArchiveRestoreImageHint, ...]


@dataclass(slots=True)
class DiscPromptState:
    ready_disc_id: str | None = None


_PENDING_BURN_STATES = {"needed", "burning"}
_REDUNDANCY_DISC_STATES = {"registered", "verified"}


@dataclass(slots=True)
class BurnDiscProgress:
    burned: bool = False
    media_verified: bool = False
    label_confirmed: bool = False
    label_notification_sent: bool = False
    location: str | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "burned": self.burned,
            "media_verified": self.media_verified,
            "label_confirmed": self.label_confirmed,
            "label_notification_sent": self.label_notification_sent,
            "location": self.location,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> BurnDiscProgress:
        return cls(
            burned=bool(payload.get("burned", False)),
            media_verified=bool(payload.get("media_verified", False)),
            label_confirmed=bool(payload.get("label_confirmed", False)),
            label_notification_sent=bool(payload.get("label_notification_sent", False)),
            location=str(payload["location"]) if payload.get("location") else None,
        )


@dataclass(slots=True)
class BurnImageProgress:
    verified_sha256: str | None = None
    discs: dict[str, BurnDiscProgress] = field(default_factory=dict)

    def to_payload(self) -> dict[str, object]:
        return {
            "verified_sha256": self.verified_sha256,
            "discs": {disc_id: progress.to_payload() for disc_id, progress in self.discs.items()},
        }

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> BurnImageProgress:
        discs_raw = payload.get("discs", {})
        if not isinstance(discs_raw, dict):
            discs_raw = {}
        discs = {
            str(disc_id): BurnDiscProgress.from_payload(disc_payload)
            for disc_id, disc_payload in discs_raw.items()
            if isinstance(disc_payload, dict)
        }
        verified_sha256 = (
            str(payload["verified_sha256"]) if payload.get("verified_sha256") is not None else None
        )
        return cls(verified_sha256=verified_sha256, discs=discs)


@dataclass(slots=True)
class BurnSessionState:
    path: Path
    images: dict[str, BurnImageProgress] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> BurnSessionState:
        if not path.exists():
            return cls(path=path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        images_raw = payload.get("images", {})
        images = {
            str(image_id): BurnImageProgress.from_payload(image_payload)
            for image_id, image_payload in images_raw.items()
            if isinstance(image_payload, dict)
        }
        return cls(path=path, images=images)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "images": {
                image_id: progress.to_payload() for image_id, progress in self.images.items()
            }
        }
        self.path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    def image_progress(self, image_id: str) -> BurnImageProgress:
        progress = self.images.get(image_id)
        if progress is None:
            progress = BurnImageProgress()
            self.images[image_id] = progress
        return progress

    def disc_progress(self, image_id: str, disc_id: str) -> BurnDiscProgress:
        image = self.image_progress(image_id)
        progress = image.discs.get(disc_id)
        if progress is None:
            progress = BurnDiscProgress()
            image.discs[disc_id] = progress
        return progress


def _load_factory(spec: str) -> object:
    module_name, sep, attr_name = spec.partition(":")
    if not sep:
        raise RuntimeError(f"invalid factory spec: {spec!r}")
    factory = getattr(importlib.import_module(module_name), attr_name)
    if not callable(factory):
        raise RuntimeError(f"factory must be callable: {spec!r}")
    return factory()


def build_optical_reader() -> object:
    spec = os.getenv("DJDAN_READER_FACTORY")
    if spec:
        return _load_factory(spec)
    return XorrisoOpticalReader()


class XorrisoIsoVerifier:
    def verify(self, iso_path: Path) -> None:
        xorriso = _require_tool("xorriso")
        _run_checked(
            [
                xorriso,
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
            action=f"staged ISO verification for {iso_path}",
        )


class XorrisoDiscBurner:
    def __init__(self, *, dummy: bool = False) -> None:
        self._dummy = dummy

    def burn(self, iso_path: Path, *, device: str, disc_id: str) -> None:
        if not iso_path.is_file():
            raise RuntimeError(f"staged ISO is missing for {disc_id}: {iso_path}")
        xorriso = _require_tool("xorriso")
        command = [
            xorriso,
            "-as",
            "cdrecord",
            "-v",
        ]
        if self._dummy:
            command.append("-dummy")
        command.extend(
            [
                f"dev={device}",
                str(iso_path),
            ]
        )
        _run_checked(
            command,
            action=f"burning {disc_id} to {device}",
        )


def _parse_diskutil_field(output: str, field: str, *, device: str) -> str:
    for line in output.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() == field:
            field_value = value.strip()
            if field_value:
                return field_value
    raise RuntimeError(f"diskutil did not report {field} for {device}")


def _inspect_macos_device_path(device: str) -> str:
    diskutil = _require_tool("diskutil")
    diskutil_output = _run_captured(
        [diskutil, "info", device],
        action=f"inspecting macOS optical device {device}",
    )
    _parse_diskutil_field(diskutil_output, "Device / Media Name", device=device)
    if "Optical Drive Type:" not in diskutil_output:
        raise RuntimeError(f"{device} is not a macOS optical drive")
    return diskutil_output


def _hdiutil_device_args(device: str) -> tuple[list[str], str | None]:
    normalized = device.strip()
    if normalized in {"", "default", "auto"}:
        return [], None
    if normalized.startswith("hdiutil:"):
        hdiutil_device = normalized.removeprefix("hdiutil:").strip()
        if not hdiutil_device:
            raise RuntimeError("hdiutil device target cannot be empty")
        return ["-device", hdiutil_device], None
    if normalized.startswith("IOService:"):
        return ["-device", normalized], None
    if normalized == "/dev/sr0":
        raise RuntimeError("macOS burn device must be /dev/diskN, /dev/rdiskN, or default")
    if not normalized.startswith(("/dev/disk", "/dev/rdisk")):
        raise RuntimeError(
            "macOS burn device must be /dev/diskN, /dev/rdiskN, default, "
            "or a native hdiutil target like hdiutil:IOService:..."
        )
    return [], _inspect_macos_device_path(normalized)


def _macos_optical_media_type(diskutil_output: str, *, device: str) -> str | None:
    try:
        return _parse_diskutil_field(diskutil_output, "Optical Media Type", device=device)
    except RuntimeError:
        return None


def _macos_raw_read_device(device: str) -> str:
    if sys.platform != "darwin":
        return device
    match = re.fullmatch(r"/dev/(r?disk\d+)", device.strip())
    if match is None:
        return device

    diskutil = shutil.which("diskutil")
    if diskutil is None:
        return device

    disk_id = match.group(1)
    block_device = f"/dev/{disk_id.removeprefix('r')}"
    raw_device = f"/dev/r{disk_id.removeprefix('r')}"
    diskutil_output = _run_captured(
        [diskutil, "info", block_device],
        action=f"inspecting macOS optical device {block_device} before media verification",
    )
    try:
        mounted = _parse_diskutil_field(
            diskutil_output,
            "Mounted",
            device=block_device,
        )
    except RuntimeError:
        mounted = "No"
    if mounted.casefold().startswith("yes"):
        _run_checked(
            [diskutil, "unmountDisk", block_device],
            action=f"unmounting {block_device} before media verification",
        )
    return raw_device


class HdiutilDiscBurner:
    def __init__(self, *, dummy: bool = False) -> None:
        self._dummy = dummy
        self.verifies_media = not dummy

    def _resolve_device_args(self, device: str) -> list[str]:
        device_args, diskutil_output = _hdiutil_device_args(device)
        media_type = (
            _macos_optical_media_type(diskutil_output, device=device)
            if diskutil_output is not None
            else None
        )
        if self._dummy and media_type is not None and media_type.startswith("BD-"):
            raise RuntimeError(
                f"macOS native test burns are not available for {media_type} media "
                "on this drive; run a real burn or use CD/DVD media for --simulate"
            )
        return device_args

    def preflight(self, *, device: str) -> None:
        self._resolve_device_args(device)

    def burn(self, iso_path: Path, *, device: str, disc_id: str) -> None:
        if not iso_path.is_file():
            raise RuntimeError(f"staged ISO is missing for {disc_id}: {iso_path}")
        hdiutil = _require_tool("hdiutil")
        device_args = self._resolve_device_args(device)
        command = [
            hdiutil,
            "burn",
            *device_args,
            "-speed",
            "max",
        ]
        if self._dummy:
            command.extend(["-noverifyburn", "-noeject", "-testburn"])
        else:
            command.extend(["-verifyburn", "-eject"])
        command.append(str(iso_path))
        try:
            _run_passthrough_checked(
                command,
                action=f"burning {disc_id} to {device}",
            )
        except RuntimeError as exc:
            if self._dummy:
                raise
            raise BurnedMediaVerificationError(
                f"hdiutil did not complete a verified burn for {disc_id}"
            ) from exc


class RawBurnedMediaVerifier:
    def verify(self, iso_path: Path, *, device: str, disc_id: str) -> None:
        if not iso_path.is_file():
            raise RuntimeError(f"staged ISO is missing for {disc_id}: {iso_path}")
        read_device = _macos_raw_read_device(device)
        expected_size = iso_path.stat().st_size
        expected_digest = hashlib.sha256()
        actual_digest = hashlib.sha256()
        remaining = expected_size
        verified = 0
        started_at = time.monotonic()
        last_report_at = started_at
        try:
            with iso_path.open("rb") as expected, Path(read_device).open("rb") as actual:
                while remaining > 0:
                    expected_chunk = expected.read(min(_DISC_IO_CHUNK_BYTES, remaining))
                    if not expected_chunk:
                        raise RuntimeError(f"staged ISO ended unexpectedly for {disc_id}")
                    actual_chunk = actual.read(len(expected_chunk))
                    if len(actual_chunk) != len(expected_chunk):
                        raise RuntimeError(
                            f"burned media for {disc_id} ended before {expected_size} "
                            "ISO bytes could be read"
                        )
                    expected_digest.update(expected_chunk)
                    actual_digest.update(actual_chunk)
                    remaining -= len(expected_chunk)
                    verified += len(expected_chunk)
                    now = time.monotonic()
                    if now - last_report_at >= _VERIFY_PROGRESS_INTERVAL_SECONDS or remaining == 0:
                        _print_progress(
                            label=f"verify {disc_id}",
                            completed=verified,
                            total=expected_size,
                            started_at=started_at,
                        )
                        last_report_at = now
        except OSError as exc:
            raise RuntimeError(
                f"could not read burned media for {disc_id} from {read_device}"
            ) from exc

        expected_sha256 = expected_digest.hexdigest()
        actual_sha256 = actual_digest.hexdigest()
        if actual_sha256 != expected_sha256:
            raise RuntimeError(
                f"burned media verification failed for {disc_id}: "
                f"expected sha256 {expected_sha256}, read {actual_sha256}"
            )


class TerminalBurnPrompts:
    def wait_for_blank_disc(
        self,
        disc_id: str,
        *,
        device: str,
        target_bytes: int | None = None,
    ) -> None:
        media_text = (
            f" with at least {_format_media_capacity(target_bytes)} capacity"
            if target_bytes is not None
            else ""
        )
        typer.echo(
            (
                f"Insert blank media{media_text} for {disc_id} into {device}, "
                "then press Enter to continue."
            ),
            err=True,
        )
        try:
            input()
        except EOFError as exc:  # pragma: no cover - exercised via subprocess acceptance tests
            raise RuntimeError("stdin closed while waiting for blank media") from exc

    def confirm_label(self, disc_id: str, *, label_text: str) -> None:
        typer.echo(
            f'Type "labeled" after writing "{label_text}" on disc {disc_id}.',
            err=True,
        )
        while True:
            try:
                response = input().strip()
            except EOFError as exc:  # pragma: no cover - exercised via subprocess acceptance tests
                raise RuntimeError("stdin closed while waiting for label confirmation") from exc
            normalized = response.strip("\"'`").casefold()
            if normalized == "labeled":
                return
            typer.echo(
                f'label confirmation for {disc_id} is still pending; type "labeled" to continue.',
                err=True,
            )

    def prompt_location(self, disc_id: str) -> str:
        typer.echo(f"Enter the storage location for {disc_id}.", err=True)
        try:
            response = input().strip()
        except EOFError as exc:  # pragma: no cover - exercised via subprocess acceptance tests
            raise RuntimeError("stdin closed while waiting for storage location") from exc
        if not response:
            raise RuntimeError(f"storage location required for {disc_id}")
        return response

    def confirm_unlabeled_disc_available(self, disc_id: str) -> bool:
        typer.echo(
            f"Is the already-burned unlabeled disc for {disc_id} still available? [y/N]",
            err=True,
        )
        try:
            response = input().strip().casefold()
        except EOFError as exc:  # pragma: no cover - exercised via subprocess acceptance tests
            raise RuntimeError("stdin closed while confirming unlabeled disc availability") from exc
        return response in {"y", "yes"}


def build_iso_verifier() -> object:
    spec = os.getenv("DJDAN_ISO_VERIFIER_FACTORY")
    if spec:
        return _load_factory(spec)
    return XorrisoIsoVerifier()


def build_disc_burner() -> object:
    spec = os.getenv("DJDAN_BURNER_FACTORY")
    if spec:
        return _load_factory(spec)
    if sys.platform == "darwin":
        return HdiutilDiscBurner()
    return XorrisoDiscBurner()


def build_simulated_disc_burner() -> object:
    if sys.platform == "darwin":
        return HdiutilDiscBurner(dummy=True)
    return XorrisoDiscBurner(dummy=True)


def build_burned_media_verifier() -> object:
    spec = os.getenv("DJDAN_BURNED_MEDIA_VERIFIER_FACTORY")
    if spec:
        return _load_factory(spec)
    return RawBurnedMediaVerifier()


def build_burn_prompts() -> object:
    spec = os.getenv("DJDAN_BURN_PROMPTS_FACTORY")
    if spec:
        return _load_factory(spec)
    return TerminalBurnPrompts()


def _default_staging_dir() -> Path:
    configured = os.getenv("DJDAN_STAGING_DIR")
    return Path(configured) if configured else Path(".djdan-staging")


def _disc_from_manifest(payload: dict[str, Any]) -> RecoveryDiscHint:
    return RecoveryDiscHint(
        disc_id=str(payload["disc_id"]),
        location=str(payload["location"]),
        disc_path=str(payload["disc_path"]),
        recovery_bytes=int(payload.get("recovery_bytes", payload.get("bytes", 0))),
        recovery_sha256=str(payload.get("recovery_sha256", "")),
    )


def _part_from_manifest(payload: dict[str, Any]) -> RecoveryPartHint:
    discs = tuple(_disc_from_manifest(disc) for disc in payload.get("discs", []))
    if not discs:
        raise RuntimeError("fetch manifest part is missing disc hints")
    return RecoveryPartHint(
        index=int(payload["index"]),
        bytes=int(payload["bytes"]),
        recovery_bytes=int(payload.get("recovery_bytes", payload["bytes"])),
        discs=discs,
    )


def _entry_from_manifest(payload: dict[str, Any]) -> RecoveryEntry:
    manifest_parts = payload.get("parts")
    if manifest_parts:
        parts = tuple(
            _part_from_manifest(part)
            for part in sorted(manifest_parts, key=lambda item: int(item["index"]))
        )
    else:
        discs = tuple(_disc_from_manifest(disc) for disc in payload.get("discs", []))
        if not discs:
            raise RuntimeError(f"fetch manifest entry is missing disc hints: {payload['id']}")
        parts = (
            RecoveryPartHint(
                index=0,
                bytes=int(payload["bytes"]),
                recovery_bytes=int(payload.get("recovery_bytes", payload["bytes"])),
                discs=discs,
            ),
        )
    return RecoveryEntry(
        id=str(payload["id"]),
        collection_id=str(payload["collection_id"]),
        path=str(payload["path"]),
        bytes=int(payload["bytes"]),
        recovery_bytes=int(payload.get("recovery_bytes", payload["bytes"])),
        parts=parts,
    )


def _entry_label(entry: RecoveryEntry) -> str:
    return f"{entry.collection_id}/{entry.path}"


def _upload_session_from_payload(entry: RecoveryEntry, payload: dict[str, Any]) -> UploadSession:
    if str(payload.get("entry")) != entry.id:
        raise RuntimeError(f"upload session entry mismatch for {_entry_label(entry)}")
    if str(payload.get("protocol")) != "tus":
        raise RuntimeError(f"upload session protocol is not tus for {_entry_label(entry)}")
    if int(payload.get("length", -1)) != entry.recovery_bytes:
        raise RuntimeError(f"upload session length mismatch for {_entry_label(entry)}")
    offset = int(payload.get("offset", -1))
    if offset < 0 or offset > entry.recovery_bytes:
        raise RuntimeError(f"upload session offset is invalid for {_entry_label(entry)}")
    return UploadSession(
        entry=entry.id,
        upload_url=str(payload["upload_url"]),
        offset=offset,
        length=entry.recovery_bytes,
        checksum_algorithm=str(payload["checksum_algorithm"]),
        expires_at=str(payload["expires_at"]) if payload.get("expires_at") is not None else None,
    )


def _prompt_for_disc(disc: RecoveryDiscHint, *, device: str) -> None:
    typer.echo(
        (
            f"Insert disc {disc.disc_id} from {disc.location} into {device}, "
            "then press Enter to continue."
        ),
        err=True,
    )
    try:
        input()
    except EOFError as exc:  # pragma: no cover - exercised via subprocess acceptance tests
        raise RuntimeError("stdin closed while waiting for disc insertion") from exc


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)


def _format_bytes(value: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{value} B"


def _format_media_capacity(value: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(value)
    for unit in units:
        if size < 1000 or unit == units[-1]:
            return f"{size:.1f} {unit}"
        size /= 1000
    return f"{value} B"


def _burn_size_summary(
    *,
    expected_bytes: int | None,
    target_bytes: int | None,
    fill: float,
) -> str:
    parts: list[str] = []
    if expected_bytes is not None:
        parts.append(_format_bytes(expected_bytes))
    if target_bytes is not None:
        parts.append(f"{_format_media_capacity(target_bytes)} target media")
    parts.append(f"fill={fill:.3f}")
    return ", ".join(parts)


def _print_progress(
    *,
    label: str,
    completed: int,
    total: int,
    started_at: float,
) -> None:
    elapsed = max(time.monotonic() - started_at, 0.001)
    percent = (completed / total) * 100 if total > 0 else 100.0
    rate = completed / elapsed
    typer.echo(
        (
            f"{label}: {_format_bytes(completed)} of {_format_bytes(total)} "
            f"({percent:.1f}%, {_format_bytes(int(rate))}/s)"
        ),
        err=True,
    )


def _download_progress_logger(
    label: str,
    *,
    estimated_total: int | None = None,
) -> Callable[[int, int | None], None]:
    last_reported_at = 0.0

    def progress(downloaded: int, total: int | None) -> None:
        nonlocal last_reported_at
        now = time.monotonic()
        display_total = total if total is not None else estimated_total
        complete = display_total is not None and downloaded >= display_total
        if not complete and now - last_reported_at < _DOWNLOAD_PROGRESS_INTERVAL_SECONDS:
            return
        last_reported_at = now
        if display_total is None or display_total <= 0:
            typer.echo(f"{label}: downloaded {_format_bytes(downloaded)}", err=True)
            return
        percent = (downloaded / display_total) * 100
        typer.echo(
            (
                f"{label}: downloaded {_format_bytes(downloaded)} of "
                f"{_format_bytes(display_total)} ({percent:.1f}%)"
            ),
            err=True,
        )

    return progress


def _call_download_with_optional_progress(
    method: Any,
    *args: object,
    progress: Callable[[int, int | None], None],
) -> object:
    try:
        supports_progress = "progress" in inspect.signature(method).parameters
    except (TypeError, ValueError):
        supports_progress = False
    if supports_progress:
        return method(*args, progress=progress)
    return method(*args)


def _burn_state_path(staging_dir: Path) -> Path:
    return staging_dir / "burn-session.json"


def _staged_iso_path(staging_dir: Path, *, image_id: str, filename: str) -> Path:
    return staging_dir / image_id / filename


def _storage_guidance(disc_id: str) -> str:
    ordinal = disc_id.rsplit("-", 1)[-1]
    if ordinal == "1":
        return "Store this first disc in your primary archive location."
    return "Store this disc in a different physical location from the first disc."


def _default_burn_device() -> str:
    if sys.platform == "darwin":
        return "default"
    return "/dev/sr0"


def _disc_label(disc_payload: dict[str, Any]) -> str:
    label = disc_payload.get("label_text")
    return str(label if label is not None else disc_payload.get("disc_id"))


def _iter_paged_payloads(fetch_page: Any) -> list[dict[str, Any]]:
    page = 1
    payload = fetch_page(page)
    results = [payload]
    pages = int(payload.get("pages", 0))
    while page < pages:
        page += 1
        results.append(fetch_page(page))
    return results


def _images_missing_disc_coverage(
    client: ApiClient,
    session_state: BurnSessionState | None = None,
) -> list[dict[str, Any]]:
    images: list[dict[str, Any]] = []
    for payload in _iter_paged_payloads(
        lambda page: client.list_images(page=page, per_page=100, sort="finalized_at", order="desc")
    ):
        for image in payload.get("images", []):
            if not isinstance(image, dict):
                continue
            registered = int(image.get("discs_registered", 0))
            required = int(image.get("discs_required", 0))
            if registered < required or _image_has_resumable_disc_verification(
                client,
                str(image.get("id")),
                session_state,
            ):
                images.append(image)
    return images


def _image_has_resumable_disc_verification(
    client: ApiClient,
    image_id: str,
    session_state: BurnSessionState | None,
) -> bool:
    if session_state is None:
        return False
    discs = client.list_image_discs(image_id).get("discs", [])
    if not isinstance(discs, list):
        return False
    return any(
        isinstance(disc, dict)
        and _can_mark_disc_verified_from_checkpoint(
            session_state,
            image_id,
            disc,
        )
        for disc in discs
    )


def _disc_states_for_image(client: ApiClient, image_id: str) -> set[str]:
    discs = client.list_image_discs(image_id).get("discs", [])
    if not isinstance(discs, list):
        return set()
    return {
        str(disc.get("state"))
        for disc in discs
        if isinstance(disc, dict) and disc.get("state") is not None
    }


def _image_needs_recovery_iso_source(client: ApiClient, image_id: str) -> bool:
    states = _disc_states_for_image(client, image_id)
    if not states:
        return False
    has_pending = bool(states & _PENDING_BURN_STATES)
    return has_pending


def _is_standard_burn_backlog_image(client: ApiClient, image_id: str) -> bool:
    states = _disc_states_for_image(client, image_id)
    if not states:
        return False

    has_pending = bool(states & _PENDING_BURN_STATES)
    has_redundancy = bool(states & _REDUNDANCY_DISC_STATES)
    all_pending = bool(states) and states <= _PENDING_BURN_STATES
    if not has_pending:
        return False
    if hasattr(client, "get_archive_restore_for_image"):
        try:
            payload = client.get_archive_restore_for_image(image_id)
        except NotFound:
            pass
        else:
            if str(payload.get("state")) in {"requested", "ready", "paused", "expired"}:
                return False
    if all_pending and not has_redundancy:
        return True
    if all_pending:
        return True
    if has_redundancy:
        return True
    return False


def _get_image_archive_restore(
    client: ApiClient,
    image_id: str,
) -> dict[str, Any] | None:
    get_archive_restore = getattr(client, "get_archive_restore_for_image", None)
    if get_archive_restore is None:
        return None
    try:
        return cast(dict[str, Any], get_archive_restore(image_id))
    except NotFound:
        return None


def _archive_restore_filename(
    payload: dict[str, Any],
    image_id: str,
    fallback: str,
) -> str:
    images = payload.get("images", [])
    if isinstance(images, list):
        for image in images:
            if not isinstance(image, dict) or str(image.get("id")) != image_id:
                continue
            filename = image.get("filename")
            if filename is not None:
                return str(filename)
    return fallback


def _recovery_burn_backlog_item(
    client: ApiClient,
    image: dict[str, Any],
    *,
    staging_dir: Path | None,
) -> BurnBacklogItem | None:
    image_id = str(image["id"])
    if not _image_needs_recovery_iso_source(client, image_id):
        return None
    payload = _get_image_archive_restore(client, image_id)
    if payload is None:
        return None
    state = str(payload.get("state"))
    if state == "requested":
        return None
    hint = _archive_restore_hint_from_payload(payload)
    if state == "expired":
        if staging_dir is None or not _can_resume_expired_archive_restore(
            hint,
            client=client,
            staging_dir=staging_dir,
        ):
            return None
    elif state != "ready":
        return None
    return BurnBacklogItem(
        image_id=image_id,
        candidate_id=None,
        filename=_archive_restore_filename(payload, image_id, str(image["filename"])),
        fill=float(image.get("fill", 0)),
        expected_bytes=_optional_int(image.get("bytes")),
        target_bytes=_optional_int(image.get("target_bytes")),
        archive_restore_id=hint.restore_id,
    )


def _discover_burn_backlog(
    client: ApiClient,
    session_state: BurnSessionState | None = None,
    *,
    staging_dir: Path | None = None,
) -> list[BurnBacklogItem]:
    items: list[BurnBacklogItem] = []

    for payload in _iter_paged_payloads(
        lambda page: client.get_plan(
            page=page,
            per_page=100,
            sort="fill",
            order="desc",
            iso_ready=True,
        )
    ):
        for candidate in payload.get("candidates", []):
            if not isinstance(candidate, dict) or not candidate.get("iso_ready"):
                continue
            items.append(
                BurnBacklogItem(
                    image_id=None,
                    candidate_id=str(candidate["candidate_id"]),
                    filename=f"{candidate['candidate_id']}.iso",
                    fill=float(candidate.get("fill", 0)),
                    expected_bytes=_optional_int(candidate.get("bytes")),
                    target_bytes=(
                        _optional_int(candidate.get("target_bytes"))
                        or _optional_int(payload.get("target_bytes"))
                    ),
                )
            )

    for image in _images_missing_disc_coverage(client, session_state):
        image_id = str(image["id"])
        recovery_item = _recovery_burn_backlog_item(
            client,
            image,
            staging_dir=staging_dir,
        )
        if recovery_item is not None:
            items.append(recovery_item)
            continue
        if not _image_has_resumable_disc_verification(
            client,
            image_id,
            session_state,
        ) and not _is_standard_burn_backlog_image(client, image_id):
            continue
        items.append(
            BurnBacklogItem(
                image_id=image_id,
                candidate_id=None,
                filename=str(image["filename"]),
                fill=float(image.get("fill", 0)),
                expected_bytes=_optional_int(image.get("bytes")),
                target_bytes=_optional_int(image.get("target_bytes")),
            )
        )

    return sorted(
        items,
        key=lambda item: (item.fill, item.image_id or item.candidate_id or ""),
        reverse=True,
    )


def _discover_recovery_handoffs(client: ApiClient) -> list[RecoveryHandoff]:
    handoffs: list[RecoveryHandoff] = []
    for image in _images_missing_disc_coverage(client):
        image_id = str(image["id"])
        if _is_standard_burn_backlog_image(client, image_id):
            continue
        try:
            payload = client.get_archive_restore_for_image(image_id)
        except NotFound:
            continue
        state = str(payload.get("state"))
        if state in {"ready", "completed"}:
            continue
        handoffs.append(
            RecoveryHandoff(
                image_id=image_id,
                restore_id=str(payload["id"]),
                state=state,
                latest_message=(
                    str(payload["latest_message"])
                    if payload.get("latest_message") is not None
                    else None
                ),
            )
        )
    return handoffs


def _report_recovery_handoffs(handoffs: list[RecoveryHandoff]) -> None:
    if not handoffs:
        return
    typer.echo("burn backlog is waiting for disc rebuild restore work")
    for handoff in handoffs:
        typer.echo(f"{handoff.image_id}: archive restore {handoff.restore_id} is {handoff.state}")
        if handoff.latest_message:
            typer.echo(handoff.latest_message)


def _archive_restore_hint_from_payload(payload: dict[str, Any]) -> ArchiveRestoreHint:
    images_payload = payload.get("images", [])
    images = tuple(
        ArchiveRestoreImageHint(
            image_id=str(image["id"]),
            filename=str(image["filename"]),
        )
        for image in images_payload
        if isinstance(image, dict)
    )
    return ArchiveRestoreHint(
        restore_id=str(payload["id"]),
        type=str(payload.get("type", "disc_rebuild")),
        state=str(payload["state"]),
        latest_message=(
            str(payload["latest_message"]) if payload.get("latest_message") is not None else None
        ),
        images=images,
    )


def _clear_recovery_artifacts(
    session_state: BurnSessionState,
    *,
    staging_dir: Path,
    images: tuple[ArchiveRestoreImageHint, ...],
) -> None:
    mutated = False
    for image in images:
        staging_root = staging_dir / image.image_id
        if staging_root.exists():
            shutil.rmtree(staging_root)
        if image.image_id in session_state.images:
            del session_state.images[image.image_id]
            mutated = True
    if not mutated:
        return
    if session_state.images:
        session_state.save()
        return
    if session_state.path.exists():
        session_state.path.unlink()


def _save_or_remove_burn_restore_state(session_state: BurnSessionState) -> None:
    if session_state.images:
        session_state.save()
        return
    if session_state.path.exists():
        session_state.path.unlink()


def _clear_burn_artifacts_if_complete(
    client: ApiClient,
    session_state: BurnSessionState,
    *,
    staging_dir: Path,
    image_id: str,
) -> None:
    if image_id not in session_state.images:
        return
    discs = client.list_image_discs(image_id).get("discs", [])
    if not isinstance(discs, list):
        return
    for disc_payload in discs:
        if not isinstance(disc_payload, dict):
            continue
        if str(disc_payload.get("state")) in _PENDING_BURN_STATES:
            return
        if _can_mark_disc_verified_from_checkpoint(session_state, image_id, disc_payload):
            return

    staging_root = staging_dir / image_id
    if staging_root.exists():
        shutil.rmtree(staging_root)
        typer.echo(f"cleared staged ISO artifacts for {image_id}", err=True)
    del session_state.images[image_id]
    _save_or_remove_burn_restore_state(session_state)


def _clear_completed_burn_artifacts(
    client: ApiClient,
    session_state: BurnSessionState,
    *,
    staging_dir: Path,
) -> None:
    for image_id in tuple(session_state.images):
        _clear_burn_artifacts_if_complete(
            client,
            session_state,
            staging_dir=staging_dir,
            image_id=image_id,
        )


def _maybe_complete_archive_restore_after_burn(
    client: ApiClient,
    restore_id: str,
    *,
    session_state: BurnSessionState,
    staging_dir: Path,
) -> None:
    try:
        payload = client.get_archive_restore(restore_id)
    except NotFound:
        return
    state = str(payload.get("state"))
    if state == "completed":
        return
    if state not in {"ready", "expired"}:
        return
    hint = _archive_restore_hint_from_payload(payload)
    for image in hint.images:
        if _image_requires_recovery_burn(client, image.image_id):
            return
        if _image_has_resumable_disc_verification(
            client,
            image.image_id,
            session_state,
        ):
            return
    client.complete_archive_restore(restore_id)
    _clear_recovery_artifacts(
        session_state,
        staging_dir=staging_dir,
        images=hint.images,
    )


def _image_requires_recovery_burn(client: ApiClient, image_id: str) -> bool:
    discs_payload = client.list_image_discs(image_id)
    return any(
        isinstance(disc_payload, dict) and str(disc_payload.get("state")) in _PENDING_BURN_STATES
        for disc_payload in discs_payload.get("discs", [])
    )


def _image_file_count(client: ApiClient, image_id: str) -> int | None:
    try:
        return _optional_int(client.get_image(image_id).get("files"))
    except Exception:
        return None


def _image_target_bytes(client: ApiClient, image_id: str) -> int | None:
    try:
        return _optional_int(client.get_image(image_id).get("target_bytes"))
    except Exception:
        return None


def _can_resume_expired_archive_restore(
    archive_restore: ArchiveRestoreHint,
    *,
    client: ApiClient,
    staging_dir: Path,
) -> bool:
    for image in archive_restore.images:
        if not _image_requires_recovery_burn(client, image.image_id):
            continue
        iso_path = _staged_iso_path(
            staging_dir,
            image_id=image.image_id,
            filename=image.filename,
        )
        if not iso_path.is_file():
            return False
    return True


def _ensure_staged_iso(
    client: ApiClient,
    image_id: str,
    filename: str,
    *,
    staging_dir: Path,
    verifier: Any,
    session_state: BurnSessionState,
    archive_restore_id: str | None = None,
    expected_total_bytes: int | None = None,
) -> Path:
    image_progress = session_state.image_progress(image_id)
    iso_path = _staged_iso_path(staging_dir, image_id=image_id, filename=filename)
    iso_path.parent.mkdir(parents=True, exist_ok=True)

    if iso_path.is_file() and image_progress.verified_sha256 is not None:
        if _sha256_file(iso_path) == image_progress.verified_sha256:
            typer.echo(f"reusing staged ISO {iso_path}", err=True)
            return iso_path
        typer.echo(f"staged ISO is invalid at {iso_path}; re-downloading", err=True)
    elif iso_path.is_file():
        typer.echo(f"verifying existing staged ISO {iso_path}", err=True)
        verifier.verify(iso_path)
        image_progress.verified_sha256 = _sha256_file(iso_path)
        session_state.save()
        return iso_path
    else:
        typer.echo(f"staged ISO is missing at {iso_path}; re-downloading", err=True)

    if archive_restore_id is None:
        typer.echo(f"downloading ISO {image_id} to {iso_path}", err=True)
        typer.echo(
            "server may spend a few minutes preparing ISO metadata before download progress begins",
            err=True,
        )
        _call_download_with_optional_progress(
            client.download_iso,
            image_id,
            iso_path,
            progress=_download_progress_logger(
                f"download ISO {image_id}",
                estimated_total=expected_total_bytes,
            ),
        )
    else:
        typer.echo(f"downloading restored ISO {image_id} to {iso_path}", err=True)
        typer.echo(
            "server may spend a few minutes preparing ISO metadata before download progress begins",
            err=True,
        )
        _call_download_with_optional_progress(
            client.download_recovered_iso,
            archive_restore_id,
            image_id,
            iso_path,
            progress=_download_progress_logger(
                f"download restored ISO {image_id}",
                estimated_total=expected_total_bytes,
            ),
        )
    typer.echo(f"verifying staged ISO {iso_path}", err=True)
    verifier.verify(iso_path)
    image_progress.verified_sha256 = _sha256_file(iso_path)
    session_state.save()
    return iso_path


def _stage_archive_restore_images(
    client: ApiClient,
    archive_restore_id: str,
    *,
    staging_dir: Path,
    session_state: BurnSessionState,
    iso_verifier: Any,
) -> ArchiveRestoreHint:
    payload = client.get_archive_restore(archive_restore_id)
    if str(payload.get("state")) == "expired":
        typer.echo(
            "restore window expired remotely; resuming from local staged ISO artifacts",
            err=True,
        )
    hint = _archive_restore_hint_from_payload(payload)
    for image in hint.images:
        if not _image_requires_recovery_burn(client, image.image_id):
            continue
        _ensure_staged_iso(
            client,
            image.image_id,
            image.filename,
            staging_dir=staging_dir,
            verifier=iso_verifier,
            session_state=session_state,
            archive_restore_id=archive_restore_id,
            expected_total_bytes=_image_target_bytes(client, image.image_id),
        )
    return hint


def _register_burned_disc(
    client: ApiClient,
    image_id: str,
    disc_id: str,
    *,
    location: str,
    file_count: int | None,
) -> None:
    typer.echo(
        (
            f"registering disc {disc_id}; Riverhog is recording this physical disc "
            f"and indexing {_file_entry_text(file_count)} for recovery"
        ),
        err=True,
    )
    typer.echo(
        "this can take a little while for images with many small files; the CLI will wait",
        err=True,
    )
    started_at = time.monotonic()
    client.register_disc(image_id, location, disc_id=disc_id)
    typer.echo(f"disc {disc_id} registered and indexed in {_elapsed_text(started_at)}", err=True)
    _mark_disc_verified(client, image_id, disc_id, location=location)


def _mark_disc_verified(
    client: ApiClient,
    image_id: str,
    disc_id: str,
    *,
    location: str,
) -> None:
    typer.echo(f"marking disc {disc_id} verified", err=True)
    started_at = time.monotonic()
    client.update_disc(
        image_id,
        disc_id,
        location=location,
        state="verified",
        verification_state="verified",
    )
    typer.echo(f"disc {disc_id} verified in {_elapsed_text(started_at)}", err=True)


def _notify_label_needed(
    client: ApiClient,
    image_id: str,
    disc_id: str,
    progress: BurnDiscProgress,
    session_state: BurnSessionState,
) -> None:
    if progress.label_notification_sent:
        return
    notify = getattr(client, "notify_disc_label_needed", None)
    if notify is None:
        return
    try:
        notify(image_id, disc_id)
    except Exception as exc:
        typer.echo(
            f"warning: failed to notify operator that {disc_id} needs labeling: {exc}",
            err=True,
        )
        return
    progress.label_notification_sent = True
    session_state.save()


def _reset_failed_burn_checkpoint(
    session_state: BurnSessionState,
    image_id: str,
    disc_id: str,
) -> BurnDiscProgress:
    progress = BurnDiscProgress()
    session_state.image_progress(image_id).discs[disc_id] = progress
    session_state.save()
    return progress


def _verification_failed_message(disc_id: str) -> str:
    return (
        f"burned media verification failed for {disc_id}; discard or destroy this disc. "
        "Do not label it, keep it, or count it toward disc redundancy."
    )


def _burn_pending_disc(
    disc_payload: dict[str, Any],
    *,
    client: ApiClient,
    image_id: str,
    filename: str,
    file_count: int | None,
    staging_dir: Path,
    session_state: BurnSessionState,
    iso_verifier: Any,
    burner: Any,
    media_verifier: Any,
    prompts: Any,
    device: str,
    archive_restore_id: str | None = None,
    expected_total_bytes: int | None = None,
    target_bytes: int | None = None,
) -> str:
    disc_id = str(disc_payload["disc_id"])
    progress = session_state.disc_progress(image_id, disc_id)

    if progress.burned and not progress.label_confirmed:
        typer.echo(
            f"checking whether the unlabeled disc for {disc_id} is still available",
            err=True,
        )
        if not prompts.confirm_unlabeled_disc_available(disc_id):
            typer.echo(
                f"unlabeled disc for {disc_id} is unavailable; restarting burn",
                err=True,
            )
            progress = BurnDiscProgress()
            session_state.image_progress(image_id).discs[disc_id] = progress
            session_state.save()

    blank_media_ready = False
    if not progress.burned:
        prompts.wait_for_blank_disc(disc_id, device=device, target_bytes=target_bytes)
        blank_media_ready = True
        preflight = getattr(burner, "preflight", None)
        if callable(preflight):
            preflight(device=device)

    iso_path = _ensure_staged_iso(
        client,
        image_id,
        filename,
        staging_dir=staging_dir,
        verifier=iso_verifier,
        session_state=session_state,
        archive_restore_id=archive_restore_id,
        expected_total_bytes=expected_total_bytes,
    )

    while not progress.media_verified:
        if not progress.burned:
            if not blank_media_ready:
                typer.echo(
                    f"Insert a new blank disc to retry burn disc {disc_id}.",
                    err=True,
                )
                prompts.wait_for_blank_disc(disc_id, device=device, target_bytes=target_bytes)
                preflight = getattr(burner, "preflight", None)
                if callable(preflight):
                    preflight(device=device)
                blank_media_ready = True
            typer.echo(f"burning disc {disc_id} from {iso_path}", err=True)
            try:
                burner.burn(iso_path, device=device, disc_id=disc_id)
            except BurnedMediaVerificationError as exc:
                typer.echo(
                    f"{exc}; treating this disc as suspect and rejecting it",
                    err=True,
                )
                typer.echo(_verification_failed_message(disc_id), err=True)
                progress = _reset_failed_burn_checkpoint(session_state, image_id, disc_id)
                blank_media_ready = False
                continue
            else:
                progress.burned = True
                if bool(getattr(burner, "verifies_media", False)):
                    progress.media_verified = True
                session_state.save()

        if progress.media_verified:
            break

        typer.echo(f"verifying burned media for {disc_id}", err=True)
        try:
            media_verifier.verify(iso_path, device=device, disc_id=disc_id)
        except Exception:
            typer.echo(_verification_failed_message(disc_id), err=True)
            progress = _reset_failed_burn_checkpoint(session_state, image_id, disc_id)
            blank_media_ready = False
            continue
        progress.media_verified = True
        session_state.save()

    _notify_label_needed(client, image_id, disc_id, progress, session_state)

    if progress.label_confirmed:
        typer.echo(f"resuming label confirmation for {disc_id}", err=True)
    else:
        if progress.burned and progress.media_verified:
            typer.echo(f"resuming label confirmation for {disc_id}", err=True)
        else:
            typer.echo(f"awaiting label confirmation for {disc_id}", err=True)
        typer.echo(f"label text: {_disc_label(disc_payload)}", err=True)
        typer.echo(f"storage guidance: {_storage_guidance(disc_id)}", err=True)
        prompts.confirm_label(disc_id, label_text=_disc_label(disc_payload))
        progress.label_confirmed = True
        progress.location = prompts.prompt_location(disc_id)
        session_state.save()

    if progress.location is None:
        raise RuntimeError(f"storage location required for {disc_id}")
    _register_burned_disc(
        client,
        image_id,
        disc_id,
        location=progress.location,
        file_count=file_count,
    )
    return disc_id


def _can_mark_disc_verified_from_checkpoint(
    session_state: BurnSessionState,
    image_id: str,
    disc_payload: dict[str, Any],
) -> bool:
    if str(disc_payload.get("state")) not in _REDUNDANCY_DISC_STATES:
        return False
    if str(disc_payload.get("verification_state")) == "verified":
        return False
    disc_id = str(disc_payload.get("disc_id"))
    progress = session_state.images.get(image_id, BurnImageProgress()).discs.get(disc_id)
    if progress is None:
        return False
    return bool(
        progress.burned
        and progress.media_verified
        and progress.label_confirmed
        and progress.location
    )


def _mark_checkpointed_disc_verified(
    disc_payload: dict[str, Any],
    *,
    client: ApiClient,
    image_id: str,
    session_state: BurnSessionState,
) -> str:
    disc_id = str(disc_payload["disc_id"])
    progress = session_state.images[image_id].discs[disc_id]
    assert progress.location is not None
    typer.echo(f"resuming verification update for {disc_id}", err=True)
    _mark_disc_verified(client, image_id, disc_id, location=progress.location)
    return disc_id


def _simulate_pending_disc(
    disc_payload: dict[str, Any],
    *,
    client: ApiClient,
    image_id: str,
    filename: str,
    staging_dir: Path,
    session_state: BurnSessionState,
    iso_verifier: Any,
    burner: Any,
    prompts: Any,
    device: str,
    archive_restore_id: str | None = None,
    expected_total_bytes: int | None = None,
    target_bytes: int | None = None,
) -> str:
    disc_id = str(disc_payload["disc_id"])
    prompts.wait_for_blank_disc(disc_id, device=device, target_bytes=target_bytes)
    preflight = getattr(burner, "preflight", None)
    if callable(preflight):
        preflight(device=device)
    iso_path = _ensure_staged_iso(
        client,
        image_id,
        filename,
        staging_dir=staging_dir,
        verifier=iso_verifier,
        session_state=session_state,
        archive_restore_id=archive_restore_id,
        expected_total_bytes=expected_total_bytes,
    )
    typer.echo(f"simulating burn disc {disc_id} from {iso_path}", err=True)
    burner.burn(iso_path, device=device, disc_id=disc_id)
    typer.echo(
        (
            f"simulated burn completed for {disc_id}; "
            "no media verification, label confirmation, or disc registration was performed"
        ),
        err=True,
    )
    return disc_id


def _process_burn_backlog_item(
    item: BurnBacklogItem,
    *,
    client: ApiClient,
    staging_dir: Path,
    session_state: BurnSessionState,
    iso_verifier: Any,
    burner: Any,
    media_verifier: Any,
    prompts: Any,
    device: str,
    simulate: bool = False,
) -> list[str]:
    if item.image_id is None:
        assert item.candidate_id is not None
        summary = _burn_size_summary(
            expected_bytes=item.expected_bytes,
            target_bytes=item.target_bytes,
            fill=item.fill,
        )
        typer.echo(
            f"selected candidate {item.candidate_id} for finalization ({summary})",
            err=True,
        )
        image_payload = client.finalize_image(item.candidate_id)
        image_id = str(image_payload["id"])
        filename = str(image_payload["filename"])
        file_count = _optional_int(image_payload.get("files"))
        expected_total_bytes = _optional_int(image_payload.get("bytes")) or item.expected_bytes
        target_bytes = _optional_int(image_payload.get("target_bytes")) or item.target_bytes
    else:
        image_id = item.image_id
        filename = item.filename
        file_count = _image_file_count(client, image_id)
        expected_total_bytes = item.expected_bytes
        target_bytes = item.target_bytes or _image_target_bytes(client, image_id)
        summary = _burn_size_summary(
            expected_bytes=expected_total_bytes,
            target_bytes=target_bytes,
            fill=item.fill,
        )
        typer.echo(
            (
                f"selected recovered image {image_id} ({summary})"
                if item.archive_restore_id is not None
                else f"selected image {image_id} ({summary})"
            ),
            err=True,
        )

    if item.archive_restore_id is not None:
        _stage_archive_restore_images(
            client,
            item.archive_restore_id,
            staging_dir=staging_dir,
            session_state=session_state,
            iso_verifier=iso_verifier,
        )

    payload = client.list_image_discs(image_id)
    completed: list[str] = []
    for disc_payload in payload.get("discs", []):
        if not isinstance(disc_payload, dict):
            continue
        if _can_mark_disc_verified_from_checkpoint(session_state, image_id, disc_payload):
            completed.append(
                _mark_checkpointed_disc_verified(
                    disc_payload,
                    client=client,
                    image_id=image_id,
                    session_state=session_state,
                )
            )
            continue
        if str(disc_payload.get("state")) not in _PENDING_BURN_STATES:
            continue
        if simulate:
            return [
                _simulate_pending_disc(
                    disc_payload,
                    client=client,
                    image_id=image_id,
                    filename=filename,
                    staging_dir=staging_dir,
                    session_state=session_state,
                    iso_verifier=iso_verifier,
                    burner=burner,
                    prompts=prompts,
                    device=device,
                    archive_restore_id=item.archive_restore_id,
                    expected_total_bytes=expected_total_bytes,
                    target_bytes=target_bytes,
                )
            ]
        completed.append(
            _burn_pending_disc(
                disc_payload,
                client=client,
                image_id=image_id,
                filename=filename,
                file_count=file_count,
                staging_dir=staging_dir,
                session_state=session_state,
                iso_verifier=iso_verifier,
                burner=burner,
                media_verifier=media_verifier,
                prompts=prompts,
                device=device,
                archive_restore_id=item.archive_restore_id,
                expected_total_bytes=expected_total_bytes,
                target_bytes=target_bytes,
            )
        )
    if not simulate:
        _clear_burn_artifacts_if_complete(
            client,
            session_state,
            staging_dir=staging_dir,
            image_id=image_id,
        )
        if item.archive_restore_id is not None:
            _maybe_complete_archive_restore_after_burn(
                client,
                item.archive_restore_id,
                session_state=session_state,
                staging_dir=staging_dir,
            )
    return completed


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
                f"current file {_entry_label(entry)}: {entry_percent:.1f}% | "
                f"manifest: {manifest_percent:.1f}% | rate: {rate:.1f} B/s"
            ),
            err=True,
        )


def _iter_recovered_chunks(reader: Any, disc: RecoveryDiscHint, *, device: str) -> Iterator[bytes]:
    if hasattr(reader, "read_iter"):
        yield from reader.read_iter(disc.disc_path, device=device)
        return
    yield reader.read(disc.disc_path, device=device)


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
    prompt_state: DiscPromptState,
) -> None:
    offset = session.offset
    part_start = 0

    for part in entry.parts:
        part_end = part_start + part.recovery_bytes
        if offset >= part_end:
            part_start = part_end
            continue

        disc = part.discs[0]
        if disc.disc_id != prompt_state.ready_disc_id:
            _prompt_for_disc(disc, device=device)
            prompt_state.ready_disc_id = disc.disc_id
        resume_within_part = max(offset - part_start, 0)
        recovered_chunks = _skip_uploaded_prefix(
            _iter_recovered_chunks(reader, disc, device=device),
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
                raise RuntimeError(f"upload offset advanced unexpectedly for {_entry_label(entry)}")
            offset = next_offset
            progress.record_uploaded_bytes(entry, uploaded_bytes)
            progress.report(entry)

        part_start = part_end

    if offset != entry.recovery_bytes:
        raise RuntimeError(
            f"upload for {_entry_label(entry)} stopped at {offset} of {entry.recovery_bytes} bytes"
        )


def _reset_byte_complete_uploads(
    client: ApiClient,
    fetch_id: str,
    entries: tuple[RecoveryEntry, ...],
    progress: ProgressReporter,
) -> list[RecoveryEntry]:
    reset_entries: list[RecoveryEntry] = []
    for entry in entries:
        if progress.uploaded_bytes_by_entry.get(entry.id, 0) < entry.recovery_bytes:
            continue
        client.cancel_fetch_entry_upload(fetch_id, entry.id)
        reset_entries.append(entry)
        typer.echo(
            (
                f"reset byte-complete upload for {_entry_label(entry)}; "
                "try another registered disc or recovered media"
            ),
            err=True,
        )
    if reset_entries:
        typer.echo(
            (
                "fetch remains active and incomplete; if every registered disc fails, "
                "report the damaged discs and use the archive restore before retrying"
            ),
            err=True,
        )
    return reset_entries


IMAGE_PLAN_QUERY_HELP = (
    "Substring match over candidate id, collection ids, and represented projected file paths"
)
IMAGE_QUERY_HELP = "Substring match over id, filename, and collection ids"
_DISC_SORT_FIELDS = {"disc_id", "image_id", "state", "verification_state", "location"}


@image_app.command("list")
def image_list_cmd(
    page: Annotated[int, typer.Option("--page", min=1)] = 1,
    per_page: Annotated[int, typer.Option("--per-page", min=1, max=100)] = 25,
    sort: Annotated[str, typer.Option("--sort", help="Sort field")] = "finalized_at",
    order: Annotated[str, typer.Option("--order", help="Sort order")] = "desc",
    query: Annotated[
        str | None,
        typer.Option("--query", "--search", help=IMAGE_QUERY_HELP),
    ] = None,
    collection: Annotated[
        str | None, typer.Option("--collection", help="Filter by exact contained collection id")
    ] = None,
    has_discs: Annotated[
        bool | None,
        typer.Option(
            "--has-discs/--no-discs", help="Filter by whether the image has registered discs"
        ),
    ] = None,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """List finalized images."""

    payload = ApiClient().list_images(
        page=page,
        per_page=per_page,
        sort=sort,
        order=order,
        query=query,
        collection=collection,
        has_discs=has_discs,
    )
    emit(payload if json_mode else format_images(payload), json_mode=json_mode)


@image_app.command("show")
def image_show_cmd(
    image_id: Annotated[str, typer.Argument(help="Finalized image id")],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Show finalized image details."""

    payload = ApiClient().get_image(image_id)
    emit(payload if json_mode else format_image(payload), json_mode=json_mode)


@image_app.command("plan")
def image_plan_cmd(
    page: Annotated[int, typer.Option("--page", min=1)] = 1,
    per_page: Annotated[int, typer.Option("--per-page", min=1, max=100)] = 25,
    sort: Annotated[str, typer.Option("--sort", help="Sort field")] = "fill",
    order: Annotated[str, typer.Option("--order", help="Sort order")] = "desc",
    query: Annotated[
        str | None,
        typer.Option("--query", "--search", help=IMAGE_PLAN_QUERY_HELP),
    ] = None,
    collection: Annotated[
        str | None, typer.Option("--collection", help="Filter by exact contained collection id")
    ] = None,
    iso_ready: Annotated[
        bool | None,
        typer.Option(
            "--iso-ready/--not-ready", help="Filter by whether the candidate is ready to finalize"
        ),
    ] = None,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """List image planner candidates."""

    payload = ApiClient().get_plan(
        page=page,
        per_page=per_page,
        sort=sort,
        order=order,
        query=query,
        collection=collection,
        iso_ready=iso_ready,
    )
    emit(payload if json_mode else format_plan(payload), json_mode=json_mode)


@image_app.command("download")
def image_download_cmd(
    image_id: Annotated[str, typer.Argument(help="Finalized image id")],
    output: Annotated[Path | None, typer.Option("-o", "--output", help="Output path")] = None,
) -> None:
    """Download a finalized ISO image."""

    client = ApiClient()
    if output is None:
        content = client.download_iso(image_id)
        if not isinstance(content, bytes):
            raise typer.Exit(code=1)
        sys.stdout.buffer.write(content)
        raise typer.Exit(code=0)

    image_payload = client.get_image(image_id)
    estimated_total = _optional_int(image_payload.get("bytes"))
    typer.echo(f"downloading ISO {image_id} to {output}", err=True)
    download_result = _call_download_with_optional_progress(
        client.download_iso,
        image_id,
        output,
        progress=_download_progress_logger(
            f"download ISO {image_id}",
            estimated_total=estimated_total,
        ),
    )
    downloaded_bytes = (
        len(download_result)
        if isinstance(download_result, bytes)
        else _optional_int(download_result)
    )
    if downloaded_bytes is None:
        raise typer.Exit(code=1)
    typer.echo(f"wrote {downloaded_bytes} bytes to {output}")


def _image_id_from_disc_id(disc_id: str) -> str:
    if "-" not in disc_id:
        raise typer.BadParameter("disc id must include an image id prefix, like 20260420T040001Z-1")
    return disc_id.rsplit("-", 1)[0]


def _emit_disc_payload(payload: dict[str, Any], *, image_id: str, json_mode: bool) -> None:
    human_payload = {"image_id": image_id, **payload}
    emit(payload if json_mode else format_disc(human_payload), json_mode=json_mode)


def _disc_payload_with_recovery_status(
    client: ApiClient,
    payload: dict[str, Any],
    *,
    image_id: str,
) -> dict[str, Any]:
    try:
        archive_restore = client.get_archive_restore_for_image(image_id)
    except NotFound:
        return payload
    return {**payload, "archive_restore": archive_restore}


def _validate_disc_sort(sort: str) -> None:
    if sort not in _DISC_SORT_FIELDS:
        raise typer.BadParameter(
            f"sort must be one of {', '.join(sorted(_DISC_SORT_FIELDS))}",
            param_hint="--sort",
        )


def _list_discs_payload(
    client: ApiClient,
    *,
    page: int,
    per_page: int,
    sort: str,
    order: str,
    query: str | None,
    image_id: str | None = None,
) -> dict[str, Any]:
    normalized_order = order.casefold()
    if normalized_order not in {"asc", "desc"}:
        raise typer.BadParameter("order must be asc or desc", param_hint="--order")
    _validate_disc_sort(sort)
    return client.list_discs(
        page=page,
        per_page=per_page,
        sort=sort,
        order=normalized_order,
        query=query,
        image_id=image_id,
    )


@disc_app.command("list")
def disc_list_cmd(
    image_id: Annotated[
        str | None,
        typer.Argument(help="Optional finalized image id", show_default=False),
    ] = None,
    page: Annotated[int, typer.Option("--page", min=1)] = 1,
    per_page: Annotated[int, typer.Option("--per-page", min=1, max=100)] = 25,
    sort: Annotated[str, typer.Option("--sort", help="Sort field")] = "disc_id",
    order: Annotated[str, typer.Option("--order", help="Sort order")] = "asc",
    query: Annotated[
        str | None,
        typer.Option("--query", "--search", help="Substring match over disc fields"),
    ] = None,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """List registered burned discs."""

    client = ApiClient()
    payload = _list_discs_payload(
        client,
        page=page,
        per_page=per_page,
        sort=sort,
        order=order,
        query=query,
        image_id=image_id,
    )
    emit(payload if json_mode else format_discs(payload), json_mode=json_mode)


@disc_app.command("show")
def disc_show_cmd(
    disc_id: Annotated[str, typer.Argument(help="Generated disc/disc id")],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Show burned disc details."""

    disc = ApiClient().get_disc(disc_id)
    emit(disc if json_mode else format_disc(disc), json_mode=json_mode)


@disc_app.command("location")
def disc_location_cmd(
    disc_id: Annotated[str, typer.Argument(help="Generated disc/disc id")],
    to: Annotated[str, typer.Option("--to", help="New physical location label")],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Update a disc location label."""

    image_id = _image_id_from_disc_id(disc_id)
    payload = ApiClient().update_disc(image_id, disc_id, location=to)
    _emit_disc_payload(payload, image_id=image_id, json_mode=json_mode)


@disc_rebuild_app.command("start")
def disc_rebuild_start_cmd(
    disc_id: Annotated[str, typer.Argument(help="Generated disc/disc id")],
    reason: Annotated[
        str,
        typer.Option("--reason", help="Why this disc needs rebuild: lost or damaged"),
    ],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Start rebuild work for a lost or damaged disc."""

    try:
        normalized_reason = reason.casefold()
        if normalized_reason not in _DISC_REBUILD_REASONS:
            raise RuntimeError("--reason must be lost or damaged")

        image_id = _image_id_from_disc_id(disc_id)
        client = ApiClient()
        payload = _disc_payload_with_recovery_status(
            client,
            client.update_disc(image_id, disc_id, state=normalized_reason),
            image_id=image_id,
        )
    except (RiverhogError, RuntimeError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    _emit_disc_payload(payload, image_id=image_id, json_mode=json_mode)


@disc_rebuild_app.command("list")
def disc_rebuild_list_cmd(
    page: Annotated[int, typer.Option("--page", min=1)] = 1,
    per_page: Annotated[int, typer.Option("--per-page", min=1, max=100)] = 25,
    sort: Annotated[str, typer.Option("--sort", help="Sort field")] = "created_at",
    order: Annotated[str, typer.Option("--order", help="Sort order")] = "desc",
    state: Annotated[
        str | None,
        typer.Option(
            "--state",
            help=("Filter by requested, ready, paused, expired, completed, failed, or canceled"),
        ),
    ] = None,
    include_all: Annotated[
        bool,
        typer.Option("--all", help="Include completed and expired disc rebuild archive restores"),
    ] = False,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """List disc rebuild archive restores."""

    try:
        payload = _list_disc_rebuild_restores(
            ApiClient(),
            page=page,
            per_page=per_page,
            sort=sort,
            order=order.casefold(),
            state=state,
            include_all=include_all,
        )
    except (RiverhogError, RuntimeError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    emit(
        payload if json_mode else format_archive_restores(_disc_rebuild_human_payload(payload)),
        json_mode=json_mode,
    )


@disc_rebuild_app.command("show")
def disc_rebuild_show_cmd(
    restore_id: Annotated[str, typer.Argument(help="Archive restore id")],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Show a disc rebuild archive restore."""

    try:
        payload = ApiClient().get_archive_restore(restore_id)
        _require_disc_rebuild_restore(payload)
    except (RiverhogError, RuntimeError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    emit(
        payload if json_mode else format_archive_restore(_disc_rebuild_human_payload(payload)),
        json_mode=json_mode,
    )


@disc_rebuild_app.command("pause")
def disc_rebuild_pause_cmd(
    restore_id: Annotated[str, typer.Argument(help="Archive restore id")],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Pause an active disc rebuild archive restore."""

    try:
        payload = ApiClient().pause_archive_restore(restore_id)
        _require_disc_rebuild_restore(payload)
    except (RiverhogError, RuntimeError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    emit(
        payload if json_mode else format_archive_restore(_disc_rebuild_human_payload(payload)),
        json_mode=json_mode,
    )


@disc_rebuild_app.command("resume")
def disc_rebuild_resume_cmd(
    restore_id: Annotated[str, typer.Argument(help="Archive restore id")],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Resume a paused disc rebuild archive restore."""

    try:
        payload = ApiClient().resume_archive_restore(restore_id)
        _require_disc_rebuild_restore(payload)
    except (RiverhogError, RuntimeError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    emit(
        payload if json_mode else format_archive_restore(_disc_rebuild_human_payload(payload)),
        json_mode=json_mode,
    )


def _run_fetch_workflow(client: ApiClient, fetch_id: str, *, device: str) -> dict[str, Any]:
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
    prompt_state = DiscPromptState()
    for entry in entries:
        _upload_entry_from_disc(
            entry,
            sessions[entry.id],
            client=client,
            reader=reader,
            device=device,
            progress=progress,
            prompt_state=prompt_state,
        )

    try:
        return client.complete_fetch(fetch_id)
    except HashMismatch as exc:
        _reset_byte_complete_uploads(client, fetch_id, entries, progress)
        raise RuntimeError(f"final fetch verification failed: {exc}") from exc


def _queued_djdan_fetch_ids(client: ApiClient) -> list[str]:
    fetch_ids: list[str] = []
    seen: set[str] = set()
    for state in ("uploading", "queued_djdan"):
        page = 1
        while True:
            payload = client.list_fetches(
                page=page,
                per_page=100,
                state=state,
                sort="order",
                order="asc",
            )
            for fetch in payload.get("fetches", []):
                if not isinstance(fetch, dict):
                    continue
                fetch_id = str(fetch.get("id"))
                if fetch_id in seen:
                    continue
                seen.add(fetch_id)
                fetch_ids.append(fetch_id)
            if page >= int(payload.get("pages", 0)):
                break
            page += 1
    return fetch_ids


@app.command("fetch")
def fetch_cmd(
    fetch_id: Annotated[str | None, typer.Argument(help="Optional fetch id")] = None,
    device: Annotated[str, typer.Option("--device", help="Optical device path")] = "/dev/sr0",
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Run the guided hot-storage fetch workflow."""

    try:
        client = ApiClient()
        fetch_ids = [fetch_id] if fetch_id is not None else _queued_djdan_fetch_ids(client)
        if not fetch_ids:
            emit({"fetches": [], "message": "no fetches queued for djdan"}, json_mode=json_mode)
            return
        completed: list[dict[str, Any]] = []
        for current_fetch_id in fetch_ids:
            typer.echo(f"djdan: fetching {current_fetch_id}", err=True)
            completed.append(_run_fetch_workflow(client, current_fetch_id, device=device))
    except (RiverhogError, RuntimeError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    emit({"fetches": completed}, json_mode=json_mode)


@app.command("burn")
def burn_cmd(
    device: Annotated[
        str | None,
        typer.Option(
            "--device",
            help="Optical device path; omitted uses the platform default burner",
        ),
    ] = None,
    staging_dir: Annotated[
        Path | None,
        typer.Option("--staging-dir", help="Local staging directory for ISO downloads"),
    ] = None,
    simulate: Annotated[
        bool,
        typer.Option(
            "--simulate",
            help=(
                "Use native non-writing burn mode and exit without media "
                "verification or disc registration"
            ),
        ),
    ] = False,
) -> None:
    """Run the guided burn-backlog workflow."""

    try:
        client = ApiClient()
        iso_verifier = build_iso_verifier()
        burner = build_simulated_disc_burner() if simulate else build_disc_burner()
        media_verifier = build_burned_media_verifier()
        prompts = build_burn_prompts()
        resolved_staging_dir = (staging_dir or _default_staging_dir()).expanduser()
        resolved_device = device or _default_burn_device()
        session_state = BurnSessionState.load(_burn_state_path(resolved_staging_dir))
        completed_disc_ids: list[str] = []
        simulated_disc_ids: list[str] = []

        while True:
            backlog = _discover_burn_backlog(
                client,
                session_state,
                staging_dir=resolved_staging_dir,
            )
            if not backlog:
                break
            disc_ids = _process_burn_backlog_item(
                backlog[0],
                client=client,
                staging_dir=resolved_staging_dir,
                session_state=session_state,
                iso_verifier=iso_verifier,
                burner=burner,
                media_verifier=media_verifier,
                prompts=prompts,
                device=resolved_device,
                simulate=simulate,
            )
            if simulate:
                simulated_disc_ids.extend(disc_ids)
                break
            completed_disc_ids.extend(disc_ids)

    except (RiverhogError, RuntimeError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if simulate:
        if simulated_disc_ids:
            typer.echo("simulated burn completed; no discs were registered")
            for disc_id in simulated_disc_ids:
                typer.echo(disc_id)
            return
        recovery_handoffs = _discover_recovery_handoffs(client)
        typer.echo("burn backlog already clear")
        _report_recovery_handoffs(recovery_handoffs)
        return
    _clear_completed_burn_artifacts(
        client,
        session_state,
        staging_dir=resolved_staging_dir,
    )
    recovery_handoffs = _discover_recovery_handoffs(client)
    if completed_disc_ids:
        typer.echo("burn backlog cleared")
        for disc_id in completed_disc_ids:
            typer.echo(disc_id)
        _report_recovery_handoffs(recovery_handoffs)
        return
    typer.echo("burn backlog already clear")
    _report_recovery_handoffs(recovery_handoffs)


def _list_disc_rebuild_restores(
    client: ApiClient,
    *,
    page: int,
    per_page: int,
    sort: str,
    order: str,
    state: str | None,
    include_all: bool,
) -> dict[str, Any]:
    return client.list_archive_restores(
        page=page,
        per_page=per_page,
        sort=sort,
        order=order,
        terminal="all" if state is not None or include_all else "active",
        restore_type="disc_rebuild",
        state=state,
    )


def _require_disc_rebuild_restore(payload: dict[str, Any]) -> None:
    if str(payload.get("type", "disc_rebuild")) != "disc_rebuild":
        raise RuntimeError("djdan disc rebuild only handles disc_rebuild archive restores")


def _disc_rebuild_human_payload(payload: dict[str, Any]) -> dict[str, Any]:
    def convert_restore(restore: dict[str, Any]) -> dict[str, Any]:
        converted = dict(restore)
        if str(converted.get("type", "disc_rebuild")) == "disc_rebuild":
            converted["type"] = "disc rebuild"
        return converted

    converted = dict(payload)
    if str(converted.get("type", "disc_rebuild")) == "disc_rebuild":
        converted["type"] = "disc rebuild"
    restores = converted.get("restores")
    if isinstance(restores, list):
        converted["restores"] = [
            convert_restore(restore) if isinstance(restore, dict) else restore
            for restore in restores
        ]
    return converted


def main() -> None:
    app()


if __name__ == "__main__":
    main()
