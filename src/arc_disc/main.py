from __future__ import annotations

import hashlib
import importlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Annotated, Any

import typer

from arc_cli.client import ApiClient
from arc_cli.output import emit
from arc_core.domain.errors import ArcError, HashMismatch, NotFound
from contracts.operator import copy as operator_copy

_DEFAULT_OPTICAL_DEVICE = os.getenv("ARC_DISC_ACCEPTANCE_DEVICE", "/dev/sr0")

app = typer.Typer(help="arc optical recovery CLI", invoke_without_command=True)


@app.callback(invoke_without_command=True)
def arc_disc_app(
    ctx: typer.Context,
    device: Annotated[str, typer.Option("--device", help="Optical device path")] = (
        _DEFAULT_OPTICAL_DEVICE
    ),
    staging_dir: Annotated[
        Path | None,
        typer.Option("--staging-dir", help="Local staging directory for ISO downloads"),
    ] = None,
) -> None:
    """Run the guided physical-media backlog clearer when no subcommand is provided."""
    if ctx.invoked_subcommand is not None:
        return
    try:
        completed_copy_ids, recovery_handoffs = _run_burn_backlog(
            device=device,
            staging_dir=staging_dir,
            preflight_statechart="arc_disc.guided",
        )
    except OpticalDeviceProblem as exc:
        typer.echo(exc.copy_text, err=True)
        raise typer.Exit(code=1) from exc
    except (ArcError, RuntimeError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if completed_copy_ids:
        typer.echo("burn backlog cleared")
        for copy_id in completed_copy_ids:
            typer.echo(copy_id)
        _report_recovery_handoffs(recovery_handoffs)
        return
    typer.echo("burn backlog already clear")
    _report_recovery_handoffs(recovery_handoffs)


_DISC_IO_CHUNK_BYTES = 1024 * 1024


def _require_tool(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise RuntimeError(f"{name} is required for optical media I/O")
    return executable


def _run_checked(command: list[str], *, action: str) -> None:
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
        return
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
        with tempfile.TemporaryDirectory(prefix="arc-disc-read-") as temp_root:
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
class RecoveryCopyHint:
    copy_id: str
    location: str
    disc_path: str
    recovery_bytes: int
    recovery_sha256: str


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


@dataclass(frozen=True, slots=True)
class BurnBacklogItem:
    image_id: str | None
    candidate_id: str | None
    filename: str
    fill: float


@dataclass(frozen=True, slots=True)
class RecoveryHandoff:
    image_id: str
    session_id: str
    state: str
    latest_message: str | None


@dataclass(frozen=True, slots=True)
class RecoverySessionImageHint:
    image_id: str
    filename: str


@dataclass(frozen=True, slots=True)
class RecoverySessionHint:
    session_id: str
    type: str
    state: str
    latest_message: str | None
    images: tuple[RecoverySessionImageHint, ...]


_PENDING_BURN_STATES = {"needed", "burning"}
_PROTECTED_COPY_STATES = {"registered", "verified"}
_ACTIVE_RECOVERY_SESSION_STATES = {"pending_approval", "restore_requested", "ready"}


class OpticalDeviceProblem(RuntimeError):
    def __init__(self, *, statechart: str, state: str, copy_text: str) -> None:
        super().__init__(copy_text)
        self.statechart = statechart
        self.state = state
        self.copy_text = copy_text


def _device_has_no_read_write_bits(path: Path) -> bool:
    try:
        mode = path.stat().st_mode
    except PermissionError:
        return True
    return mode & 0o666 == 0


def _check_optical_device_ready(device: str, *, statechart: str) -> None:
    path = Path(device)
    if not path.exists():
        raise OpticalDeviceProblem(
            statechart=statechart,
            state="device_missing",
            copy_text=operator_copy.device_missing(),
        )
    if _device_has_no_read_write_bits(path) or not os.access(path, os.R_OK | os.W_OK):
        raise OpticalDeviceProblem(
            statechart=statechart,
            state="device_permission_denied",
            copy_text=operator_copy.device_permission_denied(),
        )


def _device_lost_during_work() -> OpticalDeviceProblem:
    return OpticalDeviceProblem(
        statechart="arc_disc.burn",
        state="device_lost_during_work",
        copy_text=operator_copy.device_lost_during_work(),
    )


@dataclass(slots=True)
class BurnCopyProgress:
    burned: bool = False
    media_verified: bool = False
    label_confirmed: bool = False
    location: str | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "burned": self.burned,
            "media_verified": self.media_verified,
            "label_confirmed": self.label_confirmed,
            "location": self.location,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> BurnCopyProgress:
        return cls(
            burned=bool(payload.get("burned", False)),
            media_verified=bool(payload.get("media_verified", False)),
            label_confirmed=bool(payload.get("label_confirmed", False)),
            location=str(payload["location"]) if payload.get("location") else None,
        )


@dataclass(slots=True)
class BurnImageProgress:
    verified_sha256: str | None = None
    copies: dict[str, BurnCopyProgress] = field(default_factory=dict)

    def to_payload(self) -> dict[str, object]:
        return {
            "verified_sha256": self.verified_sha256,
            "copies": {copy_id: progress.to_payload() for copy_id, progress in self.copies.items()},
        }

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> BurnImageProgress:
        copies_raw = payload.get("copies", {})
        if not isinstance(copies_raw, dict):
            copies_raw = {}
        copies = {
            str(copy_id): BurnCopyProgress.from_payload(copy_payload)
            for copy_id, copy_payload in copies_raw.items()
            if isinstance(copy_payload, dict)
        }
        verified_sha256 = (
            str(payload["verified_sha256"]) if payload.get("verified_sha256") is not None else None
        )
        return cls(verified_sha256=verified_sha256, copies=copies)


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

    def copy_progress(self, image_id: str, copy_id: str) -> BurnCopyProgress:
        image = self.image_progress(image_id)
        progress = image.copies.get(copy_id)
        if progress is None:
            progress = BurnCopyProgress()
            image.copies[copy_id] = progress
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
    spec = os.getenv("ARC_DISC_READER_FACTORY")
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
    def burn(self, iso_path: Path, *, device: str, copy_id: str) -> None:
        if not iso_path.is_file():
            raise RuntimeError(f"staged ISO is missing for {copy_id}: {iso_path}")
        xorriso = _require_tool("xorriso")
        _run_checked(
            [
                xorriso,
                "-as",
                "cdrecord",
                "-v",
                f"dev={device}",
                str(iso_path),
            ],
            action=f"burning {copy_id} to {device}",
        )


class RawBurnedMediaVerifier:
    def verify(self, iso_path: Path, *, device: str, copy_id: str) -> None:
        if not iso_path.is_file():
            raise RuntimeError(f"staged ISO is missing for {copy_id}: {iso_path}")
        expected_size = iso_path.stat().st_size
        expected_digest = hashlib.sha256()
        actual_digest = hashlib.sha256()
        remaining = expected_size
        try:
            with iso_path.open("rb") as expected, Path(device).open("rb") as actual:
                while remaining > 0:
                    expected_chunk = expected.read(min(_DISC_IO_CHUNK_BYTES, remaining))
                    if not expected_chunk:
                        raise RuntimeError(f"staged ISO ended unexpectedly for {copy_id}")
                    actual_chunk = actual.read(len(expected_chunk))
                    if len(actual_chunk) != len(expected_chunk):
                        raise RuntimeError(
                            f"burned media for {copy_id} ended before {expected_size} "
                            "ISO bytes could be read"
                        )
                    expected_digest.update(expected_chunk)
                    actual_digest.update(actual_chunk)
                    remaining -= len(expected_chunk)
        except OSError as exc:
            raise RuntimeError(f"could not read burned media for {copy_id} from {device}") from exc

        expected_sha256 = expected_digest.hexdigest()
        actual_sha256 = actual_digest.hexdigest()
        if actual_sha256 != expected_sha256:
            raise RuntimeError(
                f"burned media verification failed for {copy_id}: "
                f"expected sha256 {expected_sha256}, read {actual_sha256}"
            )


class TerminalBurnPrompts:
    def wait_for_blank_disc(self, copy_id: str, *, device: str) -> None:
        typer.echo(
            (f"Insert blank media for {copy_id} into {device}, then press Enter to continue."),
            err=True,
        )
        try:
            input()
        except EOFError as exc:  # pragma: no cover - exercised via subprocess acceptance tests
            raise RuntimeError("stdin closed while waiting for blank media") from exc

    def confirm_label(self, copy_id: str, *, label_text: str) -> None:
        typer.echo(
            f'Type "labeled" after writing "{label_text}" on disc {copy_id}.',
            err=True,
        )
        try:
            response = input().strip()
        except EOFError as exc:  # pragma: no cover - exercised via subprocess acceptance tests
            raise RuntimeError("stdin closed while waiting for label confirmation") from exc
        if response.casefold() != "labeled":
            raise RuntimeError(f"label confirmation required for {copy_id}")

    def prompt_location(self, copy_id: str) -> str:
        typer.echo(f"Enter the storage location for {copy_id}.", err=True)
        try:
            response = input().strip()
        except EOFError as exc:  # pragma: no cover - exercised via subprocess acceptance tests
            raise RuntimeError("stdin closed while waiting for storage location") from exc
        if not response:
            raise RuntimeError(f"storage location required for {copy_id}")
        return response

    def confirm_unlabeled_copy_available(self, copy_id: str) -> bool:
        typer.echo(
            f"Is the already-burned unlabeled disc for {copy_id} still available? [y/N]",
            err=True,
        )
        try:
            response = input().strip().casefold()
        except EOFError as exc:  # pragma: no cover - exercised via subprocess acceptance tests
            raise RuntimeError("stdin closed while confirming unlabeled disc availability") from exc
        return response in {"y", "yes"}


def build_iso_verifier() -> object:
    spec = os.getenv("ARC_DISC_ISO_VERIFIER_FACTORY")
    if spec:
        return _load_factory(spec)
    return XorrisoIsoVerifier()


def build_disc_burner() -> object:
    spec = os.getenv("ARC_DISC_BURNER_FACTORY")
    if spec:
        return _load_factory(spec)
    return XorrisoDiscBurner()


def build_burned_media_verifier() -> object:
    spec = os.getenv("ARC_DISC_BURNED_MEDIA_VERIFIER_FACTORY")
    if spec:
        return _load_factory(spec)
    return RawBurnedMediaVerifier()


def build_burn_prompts() -> object:
    spec = os.getenv("ARC_DISC_BURN_PROMPTS_FACTORY")
    if spec:
        return _load_factory(spec)
    return TerminalBurnPrompts()


def _default_staging_dir() -> Path:
    configured = os.getenv("ARC_DISC_STAGING_DIR")
    return Path(configured) if configured else Path(".arc-disc-staging")


def _copy_from_manifest(payload: dict[str, Any]) -> RecoveryCopyHint:
    return RecoveryCopyHint(
        copy_id=str(payload["copy"]),
        location=str(payload["location"]),
        disc_path=str(payload["disc_path"]),
        recovery_bytes=int(payload.get("recovery_bytes", payload.get("bytes", 0))),
        recovery_sha256=str(payload.get("recovery_sha256", "")),
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


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)


def _burn_state_path(staging_dir: Path) -> Path:
    return staging_dir / "burn-session.json"


def _staged_iso_path(staging_dir: Path, *, image_id: str, filename: str) -> Path:
    return staging_dir / image_id / filename


def _storage_guidance(copy_id: str) -> str:
    ordinal = copy_id.rsplit("-", 1)[-1]
    if ordinal == "1":
        return "Store this first copy in your primary archive location."
    return "Store this copy in a different physical location from the first copy."


def _copy_label(copy_payload: dict[str, Any]) -> str:
    label = copy_payload.get("label_text")
    return str(label if label is not None else copy_payload.get("id"))


def _iter_paged_payloads(fetch_page: Any) -> list[dict[str, Any]]:
    page = 1
    payload = fetch_page(page)
    results = [payload]
    pages = int(payload.get("pages", 0))
    while page < pages:
        page += 1
        results.append(fetch_page(page))
    return results


def _images_missing_copy_coverage(client: ApiClient) -> list[dict[str, Any]]:
    images: list[dict[str, Any]] = []
    for payload in _iter_paged_payloads(
        lambda page: client.list_images(page=page, per_page=100, sort="finalized_at", order="desc")
    ):
        for image in payload.get("images", []):
            if not isinstance(image, dict):
                continue
            registered = int(image.get("physical_copies_registered", 0))
            required = int(image.get("physical_copies_required", 0))
            if registered >= required:
                continue
            images.append(image)
    return images


def _is_standard_burn_backlog_image(client: ApiClient, image_id: str) -> bool:
    copies = client.list_copies(image_id).get("copies", [])
    if not isinstance(copies, list) or not copies:
        return False

    states = {
        str(copy.get("state"))
        for copy in copies
        if isinstance(copy, dict) and copy.get("state") is not None
    }
    has_pending = bool(states & _PENDING_BURN_STATES)
    has_protected = bool(states & _PROTECTED_COPY_STATES)
    all_pending = bool(states) and states <= _PENDING_BURN_STATES
    if not has_pending:
        return False
    if all_pending and not has_protected:
        if not hasattr(client, "get_recovery_session_for_image"):
            return True
        try:
            payload = client.get_recovery_session_for_image(image_id)
        except NotFound:
            return True
        recovery_state = str(payload.get("state"))
        if recovery_state in {"pending_approval", "restore_requested", "ready", "expired"}:
            return False
    if all_pending:
        return True
    if has_protected:
        return True
    return False


def _discover_burn_backlog(client: ApiClient) -> list[BurnBacklogItem]:
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
                )
            )

    for image in _images_missing_copy_coverage(client):
        image_id = str(image["id"])
        if not _is_standard_burn_backlog_image(client, image_id):
            continue
        items.append(
            BurnBacklogItem(
                image_id=image_id,
                candidate_id=None,
                filename=str(image["filename"]),
                fill=float(image.get("fill", 0)),
            )
        )

    return sorted(
        items,
        key=lambda item: (item.fill, item.image_id or item.candidate_id or ""),
        reverse=True,
    )


def _discover_recovery_handoffs(client: ApiClient) -> list[RecoveryHandoff]:
    handoffs: list[RecoveryHandoff] = []
    for image in _images_missing_copy_coverage(client):
        image_id = str(image["id"])
        if _is_standard_burn_backlog_image(client, image_id):
            continue
        try:
            payload = client.get_recovery_session_for_image(image_id)
        except NotFound:
            continue
        handoffs.append(
            RecoveryHandoff(
                image_id=image_id,
                session_id=str(payload["id"]),
                state=str(payload["state"]),
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
    typer.echo("ordinary burn backlog is clear, but image rebuild work remains")
    for handoff in handoffs:
        typer.echo(
            f"{handoff.image_id}: recovery session {handoff.session_id} is {handoff.state}"
        )
        if handoff.latest_message:
            typer.echo(handoff.latest_message)


def _recovery_session_hint_from_payload(payload: dict[str, Any]) -> RecoverySessionHint:
    images_payload = payload.get("images", [])
    images = tuple(
        RecoverySessionImageHint(
            image_id=str(image["id"]),
            filename=str(image["filename"]),
        )
        for image in images_payload
        if isinstance(image, dict)
    )
    return RecoverySessionHint(
        session_id=str(payload["id"]),
        type=str(payload.get("type", "image_rebuild")),
        state=str(payload["state"]),
        latest_message=(
            str(payload["latest_message"]) if payload.get("latest_message") is not None else None
        ),
        images=images,
    )


def _discover_active_recovery_sessions(client: ApiClient) -> list[RecoverySessionHint]:
    sessions_by_id: dict[str, RecoverySessionHint] = {}
    for image in _images_missing_copy_coverage(client):
        image_id = str(image["id"])
        try:
            payload = client.get_recovery_session_for_image(image_id)
        except NotFound:
            continue
        hint = _recovery_session_hint_from_payload(payload)
        if hint.state not in _ACTIVE_RECOVERY_SESSION_STATES:
            continue
        sessions_by_id.setdefault(hint.session_id, hint)
    return sorted(sessions_by_id.values(), key=lambda current: current.session_id)


def _report_recovery_sessions(sessions: list[RecoverySessionHint]) -> None:
    if not sessions:
        typer.echo("no active recovery sessions")
        return
    for session in sessions:
        image_ids = ", ".join(image.image_id for image in session.images) or "(no images)"
        typer.echo(f"recovery session {session.session_id} is {session.state}")
        typer.echo(f"type: {session.type}")
        typer.echo(f"images: {image_ids}")
        if session.latest_message:
            typer.echo(session.latest_message)


def _clear_recovery_artifacts(
    session_state: BurnSessionState,
    *,
    staging_dir: Path,
    images: tuple[RecoverySessionImageHint, ...],
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


def _image_requires_recovery_burn(client: ApiClient, image_id: str) -> bool:
    copies_payload = client.list_copies(image_id)
    return any(
        isinstance(copy_payload, dict)
        and str(copy_payload.get("state")) in _PENDING_BURN_STATES
        for copy_payload in copies_payload.get("copies", [])
    )


def _can_resume_expired_recovery_session(
    recovery_session: RecoverySessionHint,
    *,
    client: ApiClient,
    staging_dir: Path,
) -> bool:
    for image in recovery_session.images:
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


def _recover_session_image(
    image: RecoverySessionImageHint,
    *,
    client: ApiClient,
    staging_dir: Path,
    session_state: BurnSessionState,
    iso_verifier: Any,
    burner: Any,
    media_verifier: Any,
    prompts: Any,
    device: str,
    recovery_session_id: str,
) -> list[str]:
    typer.echo(f"recovering image {image.image_id}", err=True)
    completed: list[str] = []
    while True:
        copies_payload = client.list_copies(image.image_id)
        pending_copies = [
            copy_payload
            for copy_payload in copies_payload.get("copies", [])
            if isinstance(copy_payload, dict)
            and str(copy_payload.get("state")) in _PENDING_BURN_STATES
        ]
        if not pending_copies:
            return completed
        completed.append(
            _burn_pending_copy(
                pending_copies[0],
                client=client,
                image_id=image.image_id,
                filename=image.filename,
                staging_dir=staging_dir,
                session_state=session_state,
                iso_verifier=iso_verifier,
                burner=burner,
                media_verifier=media_verifier,
                prompts=prompts,
                device=device,
                recovery_session_id=recovery_session_id,
            )
        )


def _stage_recovery_session_images(
    recovery_session: RecoverySessionHint,
    *,
    client: ApiClient,
    staging_dir: Path,
    session_state: BurnSessionState,
    iso_verifier: Any,
) -> None:
    for image in recovery_session.images:
        if not _image_requires_recovery_burn(client, image.image_id):
            continue
        _ensure_staged_iso(
            client,
            image.image_id,
            image.filename,
            staging_dir=staging_dir,
            verifier=iso_verifier,
            session_state=session_state,
            recovery_session_id=recovery_session.session_id,
        )


def _process_recovery_session(
    recovery_session: RecoverySessionHint,
    *,
    client: ApiClient,
    staging_dir: Path,
    session_state: BurnSessionState,
    iso_verifier: Any,
    burner: Any,
    media_verifier: Any,
    prompts: Any,
    device: str,
) -> tuple[RecoverySessionHint, list[str]]:
    session_id = recovery_session.session_id
    state = recovery_session.state
    typer.echo(f"selected recovery session {session_id}", err=True)

    if state == "expired":
        if not _can_resume_expired_recovery_session(
            recovery_session,
            client=client,
            staging_dir=staging_dir,
        ):
            raise RuntimeError(
                recovery_session.latest_message
                or f"recovery session expired and must be re-initiated: {session_id}"
            )
        typer.echo(
            "restore window expired remotely; resuming from local staged ISO artifacts",
            err=True,
        )
        state = "ready"
    if state == "completed":
        return (recovery_session, [])

    if state == "pending_approval":
        typer.echo(f"approving recovery session {session_id}", err=True)
        payload = client.approve_recovery_session(session_id)
        recovery_session = _recovery_session_hint_from_payload(payload)
        state = recovery_session.state

    if state == "restore_requested":
        return (recovery_session, [])

    if state != "ready":
        raise RuntimeError(f"unsupported recovery session state for {session_id}: {state}")

    _stage_recovery_session_images(
        recovery_session,
        client=client,
        staging_dir=staging_dir,
        session_state=session_state,
        iso_verifier=iso_verifier,
    )

    completed_copy_ids: list[str] = []
    for image in recovery_session.images:
        completed_copy_ids.extend(
            _recover_session_image(
                image,
                client=client,
                staging_dir=staging_dir,
                session_state=session_state,
                iso_verifier=iso_verifier,
                burner=burner,
                media_verifier=media_verifier,
                prompts=prompts,
                device=device,
                recovery_session_id=recovery_session.session_id,
            )
        )
    client.complete_recovery_session(session_id)
    _clear_recovery_artifacts(
        session_state,
        staging_dir=staging_dir,
        images=recovery_session.images,
    )
    return (
        RecoverySessionHint(
            session_id=recovery_session.session_id,
            type=recovery_session.type,
            state="completed",
            latest_message=(
                "Recovery session completed and restored ISO cleanup was recorded."
            ),
            images=recovery_session.images,
        ),
        completed_copy_ids,
    )


def _ensure_staged_iso(
    client: ApiClient,
    image_id: str,
    filename: str,
    *,
    staging_dir: Path,
    verifier: Any,
    session_state: BurnSessionState,
    recovery_session_id: str | None = None,
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

    if recovery_session_id is None:
        typer.echo(f"downloading ISO {image_id} to {iso_path}", err=True)
        client.download_iso(image_id, iso_path)
    else:
        typer.echo(f"downloading restored ISO {image_id} to {iso_path}", err=True)
        client.download_recovered_iso(recovery_session_id, image_id, iso_path)
    typer.echo(f"verifying staged ISO {iso_path}", err=True)
    verifier.verify(iso_path)
    image_progress.verified_sha256 = _sha256_file(iso_path)
    session_state.save()
    return iso_path


def _register_burned_copy(
    client: ApiClient,
    image_id: str,
    copy_id: str,
    *,
    location: str,
) -> None:
    client.register_copy(image_id, location, copy_id=copy_id)
    client.update_copy(
        image_id,
        copy_id,
        location=location,
        state="verified",
        verification_state="verified",
    )


def _burn_pending_copy(
    copy_payload: dict[str, Any],
    *,
    client: ApiClient,
    image_id: str,
    filename: str,
    staging_dir: Path,
    session_state: BurnSessionState,
    iso_verifier: Any,
    burner: Any,
    media_verifier: Any,
    prompts: Any,
    device: str,
    recovery_session_id: str | None = None,
) -> str:
    copy_id = str(copy_payload["id"])
    progress = session_state.copy_progress(image_id, copy_id)

    if progress.burned and not progress.label_confirmed:
        typer.echo(
            f"checking whether the unlabeled disc for {copy_id} is still available",
            err=True,
        )
        if not prompts.confirm_unlabeled_copy_available(copy_id):
            typer.echo(
                f"unlabeled disc for {copy_id} is unavailable; restarting burn",
                err=True,
            )
            progress = BurnCopyProgress()
            session_state.image_progress(image_id).copies[copy_id] = progress
            session_state.save()

    iso_path = _ensure_staged_iso(
        client,
        image_id,
        filename,
        staging_dir=staging_dir,
        verifier=iso_verifier,
        session_state=session_state,
        recovery_session_id=recovery_session_id,
    )

    if not progress.burned:
        prompts.wait_for_blank_disc(copy_id, device=device)
        typer.echo(f"burning copy {copy_id} from {iso_path}", err=True)
        try:
            burner.burn(iso_path, device=device, copy_id=copy_id)
        except RuntimeError as exc:
            raise _device_lost_during_work() from exc
        progress.burned = True
        session_state.save()

    if not progress.media_verified:
        typer.echo(f"verifying burned media for {copy_id}", err=True)
        try:
            media_verifier.verify(iso_path, device=device, copy_id=copy_id)
        except RuntimeError as exc:
            raise _device_lost_during_work() from exc
        progress.media_verified = True
        session_state.save()

    if progress.label_confirmed:
        typer.echo(f"resuming label confirmation for {copy_id}", err=True)
    else:
        if progress.burned and progress.media_verified:
            typer.echo(f"resuming label confirmation for {copy_id}", err=True)
        else:
            typer.echo(f"awaiting label confirmation for {copy_id}", err=True)
        typer.echo(f"label text: {_copy_label(copy_payload)}", err=True)
        typer.echo(f"storage guidance: {_storage_guidance(copy_id)}", err=True)
        prompts.confirm_label(copy_id, label_text=_copy_label(copy_payload))
        progress.label_confirmed = True
        progress.location = prompts.prompt_location(copy_id)
        session_state.save()

    if progress.location is None:
        raise RuntimeError(f"storage location required for {copy_id}")
    _register_burned_copy(client, image_id, copy_id, location=progress.location)
    return copy_id


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
) -> list[str]:
    if item.image_id is None:
        assert item.candidate_id is not None
        typer.echo(
            f"selected candidate {item.candidate_id} for finalization (fill={item.fill:.3f})",
            err=True,
        )
        image_payload = client.finalize_image(item.candidate_id)
        image_id = str(image_payload["id"])
        filename = str(image_payload["filename"])
    else:
        image_id = item.image_id
        filename = item.filename
        typer.echo(f"selected image {image_id} (fill={item.fill:.3f})", err=True)

    payload = client.list_copies(image_id)
    completed: list[str] = []
    for copy_payload in payload.get("copies", []):
        if not isinstance(copy_payload, dict):
            continue
        if str(copy_payload.get("state")) not in _PENDING_BURN_STATES:
            continue
        completed.append(
            _burn_pending_copy(
                copy_payload,
                client=client,
                image_id=image_id,
                filename=filename,
                staging_dir=staging_dir,
                session_state=session_state,
                iso_verifier=iso_verifier,
                burner=burner,
                media_verifier=media_verifier,
                prompts=prompts,
                device=device,
            )
        )
    return completed


def _run_burn_backlog(
    *,
    device: str,
    staging_dir: Path | None,
    preflight_statechart: str,
) -> tuple[list[str], list[RecoveryHandoff]]:
    _check_optical_device_ready(device, statechart=preflight_statechart)
    client = ApiClient()
    iso_verifier = build_iso_verifier()
    burner = build_disc_burner()
    media_verifier = build_burned_media_verifier()
    prompts = build_burn_prompts()
    resolved_staging_dir = (staging_dir or _default_staging_dir()).expanduser()
    session_state = BurnSessionState.load(_burn_state_path(resolved_staging_dir))
    completed_copy_ids: list[str] = []

    while True:
        backlog = _discover_burn_backlog(client)
        if not backlog:
            break
        completed_copy_ids.extend(
            _process_burn_backlog_item(
                backlog[0],
                client=client,
                staging_dir=resolved_staging_dir,
                session_state=session_state,
                iso_verifier=iso_verifier,
                burner=burner,
                media_verifier=media_verifier,
                prompts=prompts,
                device=device,
            )
        )

    return completed_copy_ids, _discover_recovery_handoffs(client)


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
                f"reset byte-complete upload for {entry.path}; "
                "try another registered copy or recovered media"
            ),
            err=True,
        )
    if reset_entries:
        typer.echo(
            (
                "fetch remains active and incomplete; if every registered copy fails, "
                "report the damaged copies and use the Glacier recovery session before retrying"
            ),
            err=True,
        )
    return reset_entries


@app.command("fetch")
def fetch_cmd(
    fetch_id: Annotated[str, typer.Argument(help="Fetch id")],
    device: Annotated[str, typer.Option("--device", help="Optical device path")] = (
        _DEFAULT_OPTICAL_DEVICE
    ),
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

        try:
            payload = client.complete_fetch(fetch_id)
        except HashMismatch as exc:
            _reset_byte_complete_uploads(client, fetch_id, entries, progress)
            raise RuntimeError(f"final fetch verification failed: {exc}") from exc
    except (ArcError, RuntimeError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    emit(payload, json_mode=json_mode)


@app.command("burn")
def burn_cmd(
    device: Annotated[str, typer.Option("--device", help="Optical device path")] = (
        _DEFAULT_OPTICAL_DEVICE
    ),
    staging_dir: Annotated[
        Path | None,
        typer.Option("--staging-dir", help="Local staging directory for ISO downloads"),
    ] = None,
) -> None:
    try:
        completed_copy_ids, recovery_handoffs = _run_burn_backlog(
            device=device,
            staging_dir=staging_dir,
            preflight_statechart="arc_disc.burn",
        )
    except OpticalDeviceProblem as exc:
        typer.echo(exc.copy_text, err=True)
        raise typer.Exit(code=1) from exc
    except (ArcError, RuntimeError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if completed_copy_ids:
        typer.echo("burn backlog cleared")
        for copy_id in completed_copy_ids:
            typer.echo(copy_id)
        _report_recovery_handoffs(recovery_handoffs)
        return
    typer.echo("burn backlog already clear")
    _report_recovery_handoffs(recovery_handoffs)


@app.command("recover")
def recover_cmd(
    session_id: Annotated[
        str | None,
        typer.Argument(help="Recovery session id", show_default=False),
    ] = None,
    device: Annotated[str, typer.Option("--device", help="Optical device path")] = (
        _DEFAULT_OPTICAL_DEVICE
    ),
    staging_dir: Annotated[
        Path | None,
        typer.Option("--staging-dir", help="Local staging directory for ISO downloads"),
    ] = None,
) -> None:
    try:
        client = ApiClient()
        sessions = _discover_active_recovery_sessions(client)
        if session_id is None:
            _report_recovery_sessions(sessions)
            return

        payload = client.get_recovery_session(session_id)
        recovery_session = _recovery_session_hint_from_payload(payload)
        iso_verifier = build_iso_verifier()
        burner = build_disc_burner()
        media_verifier = build_burned_media_verifier()
        prompts = build_burn_prompts()
        resolved_staging_dir = (staging_dir or _default_staging_dir()).expanduser()
        session_state = BurnSessionState.load(_burn_state_path(resolved_staging_dir))
        recovery_session, completed_copy_ids = _process_recovery_session(
            recovery_session,
            client=client,
            staging_dir=resolved_staging_dir,
            session_state=session_state,
            iso_verifier=iso_verifier,
            burner=burner,
            media_verifier=media_verifier,
            prompts=prompts,
            device=device,
        )
    except (ArcError, RuntimeError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if recovery_session.state == "restore_requested":
        label = (
            "rebuild session"
            if recovery_session.type == "image_rebuild"
            else "recovery session"
        )
        typer.echo(f"{label} {session_id} is restore_requested")
        if recovery_session.latest_message:
            typer.echo(recovery_session.latest_message)
        return

    label = (
        "rebuild session"
        if recovery_session.type == "image_rebuild"
        else "recovery session"
    )
    typer.echo(f"{label} {session_id} completed")
    for copy_id in completed_copy_ids:
        typer.echo(copy_id)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
