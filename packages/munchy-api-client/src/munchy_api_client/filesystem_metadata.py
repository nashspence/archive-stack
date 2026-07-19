from __future__ import annotations

import base64
import grp
import hashlib
import json
import os
import pathlib
import platform
import pwd
import shutil
import stat as stat_module
import subprocess
from datetime import UTC, datetime
from typing import Any, cast

SOURCE_FILESYSTEM_METADATA_FILENAME = ".munchy-source-filesystem-metadata.json"


def _ns_to_iso(value: int | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1_000_000_000, UTC).isoformat()


def _user_name(uid: int) -> str | None:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return None


def _group_name(gid: int) -> str | None:
    try:
        return grp.getgrgid(gid).gr_name
    except KeyError:
        return None


def _stat_birthtime_ns(stat_result: os.stat_result) -> int | None:
    value = getattr(stat_result, "st_birthtime_ns", None)
    if isinstance(value, int):
        return value
    seconds = getattr(stat_result, "st_birthtime", None)
    if isinstance(seconds, (int, float)):
        return int(seconds * 1_000_000_000)
    return None


def _stat_flags(stat_result: os.stat_result) -> int | None:
    value = getattr(stat_result, "st_flags", None)
    return value if isinstance(value, int) else None


def _json_scalar(value: object) -> object | None:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return None


def _raw_stat_payload(stat_result: os.stat_result) -> dict[str, object]:
    raw: dict[str, object] = {}
    for name in sorted(dir(stat_result)):
        if not name.startswith("st_"):
            continue
        try:
            value = getattr(stat_result, name)
        except AttributeError:
            continue
        scalar = _json_scalar(value)
        if scalar is not None:
            raw[name] = scalar
    return raw


def _stat_payload(path: pathlib.Path) -> dict[str, Any]:
    stat_result = path.stat()
    mode = int(stat_result.st_mode)
    birthtime_ns = _stat_birthtime_ns(stat_result)
    flags = _stat_flags(stat_result)
    payload: dict[str, Any] = {
        "size": int(stat_result.st_size),
        "mode": mode,
        "mode_octal": oct(stat_module.S_IMODE(mode)),
        "filemode": stat_module.filemode(mode),
        "file_type": "regular_file" if stat_module.S_ISREG(mode) else "other",
        "uid": int(stat_result.st_uid),
        "gid": int(stat_result.st_gid),
        "user": _user_name(int(stat_result.st_uid)),
        "group": _group_name(int(stat_result.st_gid)),
        "inode": int(stat_result.st_ino),
        "device": int(stat_result.st_dev),
        "nlink": int(stat_result.st_nlink),
        "atime_ns": int(stat_result.st_atime_ns),
        "atime": _ns_to_iso(int(stat_result.st_atime_ns)),
        "mtime_ns": int(stat_result.st_mtime_ns),
        "mtime": _ns_to_iso(int(stat_result.st_mtime_ns)),
        "ctime_ns": int(stat_result.st_ctime_ns),
        "ctime": _ns_to_iso(int(stat_result.st_ctime_ns)),
        "raw_stat": _raw_stat_payload(stat_result),
    }
    if birthtime_ns is not None:
        payload["birthtime_ns"] = birthtime_ns
        payload["birthtime"] = _ns_to_iso(birthtime_ns)
    if flags is not None:
        payload["flags"] = flags
    return payload


def _list_xattrs(path: pathlib.Path) -> list[str]:
    listxattr = getattr(os, "listxattr", None)
    if listxattr is not None:
        try:
            names = listxattr(path, follow_symlinks=True)
        except TypeError:
            names = listxattr(path)
        return sorted(str(name) for name in names)
    if shutil.which("xattr") is None:
        return []
    proc = subprocess.run(
        ["xattr", str(path)],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise OSError(detail or f"xattr exited with {proc.returncode}")
    names = proc.stdout.decode("utf-8", "surrogateescape").splitlines()
    return sorted(str(name) for name in names)


def _get_xattr(path: pathlib.Path, name: str) -> bytes:
    getxattr = getattr(os, "getxattr", None)
    if getxattr is not None:
        try:
            return cast(bytes, getxattr(path, name, follow_symlinks=True))
        except TypeError:
            return cast(bytes, getxattr(path, name))
    if shutil.which("xattr") is None:
        raise OSError("xattr support is not available")
    proc = subprocess.run(
        ["xattr", "-px", name, str(path)],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise OSError(detail or f"xattr exited with {proc.returncode}")
    hex_text = b"".join(proc.stdout.split())
    try:
        return bytes.fromhex(hex_text.decode("ascii"))
    except ValueError as exc:
        raise OSError(f"xattr returned non-hex data for {name}") from exc


def _xattrs_payload(path: pathlib.Path) -> dict[str, Any]:
    listxattr = getattr(os, "listxattr", None)
    getxattr = getattr(os, "getxattr", None)
    if (listxattr is None or getxattr is None) and shutil.which("xattr") is None:
        return {"available": False, "items": []}
    items: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    try:
        names = _list_xattrs(path)
    except OSError as exc:
        return {"available": True, "items": [], "errors": [{"error": str(exc)}]}
    for name in names:
        try:
            value = _get_xattr(path, name)
        except OSError as exc:
            errors.append({"name": name, "error": str(exc)})
            continue
        items.append(
            {
                "name": name,
                "bytes": len(value),
                "sha256": hashlib.sha256(value).hexdigest(),
                "value_base64": base64.b64encode(value).decode("ascii"),
            }
        )
    payload: dict[str, Any] = {"available": True, "items": items}
    if errors:
        payload["errors"] = errors
    return payload


def collect_filesystem_metadata(path: pathlib.Path) -> dict[str, Any]:
    source = pathlib.Path(path)
    return {
        "schema_version": 1,
        "kind": "munchy.source-filesystem-metadata",
        "captured_from": str(source),
        "basename": source.name,
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "stat": _stat_payload(source),
        "extended_attributes": _xattrs_payload(source),
    }


def write_filesystem_metadata_map(
    directory: pathlib.Path,
    files: dict[str, dict[str, Any]],
    *,
    created_at: str,
) -> pathlib.Path | None:
    path = directory / SOURCE_FILESYSTEM_METADATA_FILENAME
    records = {path: metadata for path, metadata in sorted(files.items()) if metadata}
    if not records:
        path.unlink(missing_ok=True)
        return None
    payload = {
        "schema_version": 1,
        "kind": "munchy.input-filesystem-metadata-map",
        "created_at": created_at,
        "files": records,
    }
    part = path.with_suffix(path.suffix + ".part")
    directory.mkdir(parents=True, exist_ok=True)
    part.write_text(json_dumps(payload), encoding="utf-8")
    part.replace(path)
    return path


def load_filesystem_metadata_map(directory: pathlib.Path) -> dict[str, dict[str, Any]]:
    path = directory / SOURCE_FILESYSTEM_METADATA_FILENAME
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    files = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(files, dict):
        return {}
    return {
        str(rel_path): metadata
        for rel_path, metadata in files.items()
        if isinstance(metadata, dict)
    }


def json_dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"
