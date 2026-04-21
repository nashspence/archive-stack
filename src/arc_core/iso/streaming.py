from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

from arc_core.domain.errors import Conflict

XORRISO = shutil.which("xorriso") or "xorriso"
CHUNK_BYTES = 1024 * 1024
ISO_BLOCK_BYTES = 2048
_PRINT_SIZE_RE = re.compile(r"(?:^|\b)(?:size=)?(?P<blocks>\d+)(?:\b|$)")


@dataclass(frozen=True)
class IsoEntry:
    iso_path: str
    disk_path: Path


@dataclass(frozen=True)
class IsoVolume:
    volume_id: str
    filename: str
    entries: list[IsoEntry]


@dataclass(frozen=True)
class IsoStream:
    body: AsyncIterator[bytes]
    media_type: str = "application/octet-stream"
    headers: dict[str, str] | None = None



def _base_xorriso_cmd(*, volume_id: str) -> list[str]:
    return [
        XORRISO,
        "-abort_on",
        "FAILURE",
        "-outdev",
        "-",
        "-volid",
        volume_id,
        "-joliet",
        "on",
        "-hardlinks",
        "on",
        "-acl",
        "on",
        "-xattr",
        "user",
        "-md5",
        "on",
    ]



def build_iso_cmd(volume: IsoVolume) -> list[str]:
    cmd = _base_xorriso_cmd(volume_id=volume.volume_id)
    for entry in volume.entries:
        if not entry.iso_path.startswith("/"):
            raise Conflict(f"bad iso path: {entry.iso_path}")
        if not entry.disk_path.exists():
            raise Conflict(f"missing source path: {entry.disk_path}")
        cmd += ["-map", str(entry.disk_path), entry.iso_path]
    cmd += ["-commit"]
    return cmd



def build_iso_cmd_from_root(*, image_root: Path, volume_id: str) -> list[str]:
    if not image_root.exists() or not image_root.is_dir():
        raise Conflict(f"image root does not exist: {image_root}")
    return [
        *_base_xorriso_cmd(volume_id=volume_id),
        "-map",
        str(image_root),
        "/",
        "-commit",
    ]



def build_iso_print_size_cmd_from_root(*, image_root: Path, volume_id: str) -> list[str]:
    cmd = build_iso_cmd_from_root(image_root=image_root, volume_id=volume_id)
    return [*cmd[:-1], "-print-size", "-end"]



def _parse_print_size_blocks(output: str) -> int:
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        match = _PRINT_SIZE_RE.search(stripped)
        if match is None:
            continue
        if stripped == match.group("blocks") or stripped.startswith("size="):
            return int(match.group("blocks"))
    raise RuntimeError("xorriso did not report a parseable -print-size value")



def estimate_iso_size_from_root(*, image_root: Path, volume_id: str, fallback_bytes: int) -> int:
    cmd = build_iso_print_size_cmd_from_root(image_root=image_root, volume_id=volume_id)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return fallback_bytes

    combined = "\n".join(part for part in (proc.stdout, proc.stderr) if part).strip()
    if proc.returncode != 0:
        detail = combined[-1500:] or f"xorriso exited {proc.returncode}"
        raise RuntimeError(detail)

    try:
        blocks = _parse_print_size_blocks(combined)
    except RuntimeError:
        if not combined:
            return fallback_bytes
        raise
    return blocks * ISO_BLOCK_BYTES


async def _drain_stderr(stream: asyncio.StreamReader | None, *, limit: int = 1_000_000) -> bytes:
    if stream is None:
        return b""
    buffer = bytearray()
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            break
        if len(buffer) < limit:
            buffer.extend(chunk[: limit - len(buffer)])
    return bytes(buffer)


async def _kill_proc(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        if os.name == "nt":
            proc.terminate()
        else:
            os.killpg(proc.pid, signal.SIGTERM)
        await asyncio.wait_for(proc.wait(), timeout=2)
        return
    except Exception:
        pass
    with contextlib.suppress(Exception):
        if proc.returncode is None:
            if os.name == "nt":
                proc.kill()
            else:
                os.killpg(proc.pid, signal.SIGKILL)
        await proc.wait()


async def _stream_process(cmd: list[str], *, filename: str) -> IsoStream:
    kwargs: dict[str, object] = {
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
    }
    if os.name != "nt":
        kwargs["start_new_session"] = True

    proc = await asyncio.create_subprocess_exec(*cmd, **kwargs)
    assert proc.stdout is not None
    stderr_task = asyncio.create_task(_drain_stderr(proc.stderr))

    first = await proc.stdout.read(CHUNK_BYTES)
    if not first:
        rc = await proc.wait()
        stderr = await stderr_task
        detail = (stderr.decode("utf-8", errors="replace") or f"xorriso exited {rc}")[-1500:]
        raise Conflict(detail)

    async def body() -> AsyncIterator[bytes]:
        try:
            yield first
            while True:
                chunk = await proc.stdout.read(CHUNK_BYTES)
                if not chunk:
                    break
                yield chunk
            await proc.wait()
        except asyncio.CancelledError:  # pragma: no cover - exercised through ASGI server behavior
            await _kill_proc(proc)
            raise
        finally:
            if proc.returncode is None:
                await _kill_proc(proc)
            stderr_task.cancel()
            with contextlib.suppress(Exception):
                await stderr_task

    return IsoStream(
        body=body(),
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


async def stream_iso_from_entries(volume: IsoVolume) -> IsoStream:
    return await _stream_process(build_iso_cmd(volume), filename=volume.filename)


async def stream_iso_from_root(*, image_root: Path, volume_id: str, filename: str) -> IsoStream:
    return await _stream_process(build_iso_cmd_from_root(image_root=image_root, volume_id=volume_id), filename=filename)
