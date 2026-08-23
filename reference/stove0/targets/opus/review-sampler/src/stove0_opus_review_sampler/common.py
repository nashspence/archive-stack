"""Private FFmpeg helpers for the Opus review sampler."""

from __future__ import annotations

import hashlib
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path


class OpusContentError(RuntimeError):
    pass


def run_ffmpeg(
    command: Sequence[str], *, log_root: Path, timeout_seconds: int, canceled: Callable[[], bool]
) -> None:
    log_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    log = log_root / f".ffmpeg-{threading.get_ident()}.log"
    started = time.monotonic()
    detail = ""
    try:
        with log.open("wb") as errors:
            process = subprocess.Popen(
                list(command), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errors
            )
            try:
                while process.poll() is None:
                    if canceled():
                        raise InterruptedError("Opus review sampling was canceled")
                    if time.monotonic() - started > timeout_seconds:
                        raise subprocess.TimeoutExpired(command, timeout_seconds)
                    threading.Event().wait(0.2)
            except BaseException:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise
        if process.returncode:
            detail = log.read_bytes()[-8192:].decode("utf-8", "replace").strip()
    finally:
        log.unlink(missing_ok=True)
    if process.returncode:
        raise OpusContentError(detail or f"FFmpeg exited with status {process.returncode}")


def file_identity(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(8 * 1024**2):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def tool_version(command: str) -> str:
    try:
        result = subprocess.run(
            [command, "-version"], check=False, capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return "unavailable"
    lines = (result.stdout or result.stderr).splitlines()
    return lines[0][:200] if lines else "unavailable"


__all__ = ["OpusContentError", "file_identity", "run_ffmpeg", "tool_version"]
