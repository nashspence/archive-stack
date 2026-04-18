from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .config import ISO_AUTHORING_COMMAND
from .storage import iso_volume_label, registered_iso_storage_path

ISO_BLOCK_SIZE = 2048


def _require_iso_authoring_tool() -> str:
    tool = shutil.which(ISO_AUTHORING_COMMAND)
    if tool is None:
        raise RuntimeError(f"{ISO_AUTHORING_COMMAND} is not installed")
    return tool


def _authoring_command_args(tool: str, label: str, *extra: str) -> list[str]:
    return [
        tool,
        "-as",
        "mkisofs",
        "-iso-level",
        "3",
        "-full-iso9660-filenames",
        "-joliet",
        "-joliet-long",
        "-rational-rock",
        "-no-pad",
        "-volid",
        label,
        *extra,
        ".",
    ]


def _parse_print_size_bytes(output: str) -> int:
    blocks = next((int(line.strip()) for line in reversed(output.splitlines()) if line.strip().isdigit()), None)
    if blocks is None:
        message = output.strip() or "empty output"
        raise RuntimeError(f"could not parse ISO size from {ISO_AUTHORING_COMMAND} output: {message}")
    return blocks * ISO_BLOCK_SIZE


def estimate_iso_size_from_container_root(
    root: Path,
    *,
    requested_label: str | None = None,
) -> int:
    tool = _require_iso_authoring_tool()
    if not root.exists() or not root.is_dir():
        raise RuntimeError(f"container root {root} is missing")

    label = iso_volume_label(requested_label or root.name)
    result = subprocess.run(
        _authoring_command_args(tool, label, "-quiet", "-print-size"),
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip() or "iso size estimation failed"
        raise RuntimeError(message)
    return _parse_print_size_bytes(result.stdout)


def create_iso_from_container_root(
    container_id: str,
    root: Path,
    *,
    requested_label: str | None = None,
) -> Path:
    tool = _require_iso_authoring_tool()
    if not root.exists() or not root.is_dir():
        raise RuntimeError(f"container root {root} is missing")

    output = registered_iso_storage_path(container_id)
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix(".iso.tmp")
    temp.unlink(missing_ok=True)

    label = iso_volume_label(requested_label or container_id)
    result = subprocess.run(
        _authoring_command_args(tool, label, "-o", str(temp)),
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        temp.unlink(missing_ok=True)
        message = (result.stderr or result.stdout).strip() or "iso authoring failed"
        raise RuntimeError(message)

    temp.replace(output)
    return output
