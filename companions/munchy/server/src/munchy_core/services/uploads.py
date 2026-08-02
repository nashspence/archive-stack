from __future__ import annotations

import base64
import errno
import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, cast
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import httpx
from munchy_api_client.filesystem_metadata import (
    load_filesystem_metadata_map,
    write_filesystem_metadata_map,
)
from munchy_target_support.source_artifact_bridge import (
    build_preserve_source_artifacts,
)
from time_formats import utc_timestamp_now

import munchy_core.domain.models as domain_models
import munchy_core.persistence.sqlite_state as state_store
import munchy_core.runtime.config as runtime_config
import munchy_core.runtime.execution as execution_runtime
from munchy_core.domain.errors import ServiceError


def ensure_dirs() -> None:
    for path in (
        runtime_config.STATE_DIR,
        runtime_config.WORK_DIR,
        runtime_config.TUSD_DIR,
        runtime_config.GPU_RUNTIME_DIR,
    ):
        path.mkdir(parents=True, exist_ok=True)


def tusd_upload_id_for_target_path(target_path: str) -> str:
    normalized = target_path.lstrip("/")
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f".munchy-server/uploads/by-target/{digest}"


def tusd_data_path(upload_id: str) -> Path:
    return domain_models.path_under(
        runtime_config.TUSD_DIR,
        upload_id,
        label="TUS upload",
    )


def safe_local_id(value: str) -> str:
    cleaned = "".join(
        ch if ch in domain_models.SAFE_GROUP_NAME_CHARS else "-" for ch in value
    ).strip(".-_")
    digest = hashlib.sha256(value.encode()).hexdigest()[:12]
    prefix = (cleaned or "upload")[:96].strip(".-_") or "upload"
    return f"{prefix}-{digest}"


def shared_input_upload_root(upload_id: str) -> Path:
    return runtime_config.GPU_RUNTIME_DIR / "input-uploads" / safe_local_id(upload_id)


def shared_review_plan_path(upload_id: str, group_name: str, task_name: str) -> Path:
    domain_models.validate_group_name(group_name)
    return shared_input_upload_root(upload_id) / f".munchy-{task_name}-{group_name}-plan.json"


def gpu_runtime_container_path(path: Path) -> str:
    rel = path.resolve().relative_to(runtime_config.GPU_RUNTIME_DIR)
    return f"/data/{rel.as_posix()}"


def target_path_for(upload_id: str, rel_path: str) -> str:
    return f".munchy-server/uploads/{upload_id}/{rel_path}"


def upload_id_from_target_path(target_path: str) -> str | None:
    normalized = target_path.lstrip("/")
    prefix = ".munchy-server/uploads/"
    if not normalized.startswith(prefix):
        return None
    rest = normalized.removeprefix(prefix)
    upload_id, sep, _rel_path = rest.partition("/")
    if not sep or not upload_id:
        return None
    return upload_id


def rel_path_from_target_path(target_path: str) -> str | None:
    normalized = target_path.lstrip("/")
    prefix = ".munchy-server/uploads/"
    if not normalized.startswith(prefix):
        return None
    rest = normalized.removeprefix(prefix)
    _upload_id, sep, rel_path = rest.partition("/")
    if not sep or not rel_path:
        return None
    return rel_path


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def path_device(path: Path) -> int:
    path.mkdir(parents=True, exist_ok=True)
    return path.stat().st_dev


def free_bytes(path: Path) -> int:
    path.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(path).free


def input_upload_storage_hint(upload: dict[str, Any]) -> domain_models.InputUploadStorageHint:
    raw = upload.get("storage_hint")
    if not isinstance(raw, dict):
        raise RuntimeError(f"input upload {upload.get('input_upload_id')} is missing storage_hint")
    return domain_models.InputUploadStorageHint.model_validate(raw)


def upload_file_resolved_group(file_state: dict[str, Any]) -> str:
    group = file_state.get("resolved_group")
    if isinstance(group, str) and group.strip():
        return domain_models.validate_group_name(group)
    if file_state.get("structured_routing"):
        return ""
    try:
        return domain_models.input_path_group(str(file_state["path"]))
    except ValueError:
        return ""


def upload_file_group_rel_for_state(file_state: dict[str, Any], group_name: str) -> Path:
    resolved = file_state.get("resolved_group_rel")
    if isinstance(resolved, str) and resolved.strip():
        if upload_file_resolved_group(file_state) != group_name:
            raise RuntimeError(
                f"input file {file_state.get('path')!r} is not in group {group_name!r}"
            )
        return Path(domain_models.normalize_posix(resolved))
    return upload_file_group_rel(str(file_state["path"]), group_name)


def materialized_input_rel_path(file_state: dict[str, Any]) -> Path:
    group_name = upload_file_resolved_group(file_state)
    if not group_name:
        raise RuntimeError(f"input file has not been routed yet: {file_state.get('path')!r}")
    return Path(group_name) / upload_file_group_rel_for_state(file_state, group_name)


def shared_input_file_path(file_state: dict[str, Any]) -> Path | None:
    input_upload_id = str(file_state.get("input_upload_id") or "")
    rel_path = str(file_state.get("path") or "")
    if not input_upload_id or not rel_path:
        return None
    root = shared_input_upload_root(input_upload_id)
    original_path = domain_models.path_under(root, rel_path, label="input file")
    group_name = upload_file_resolved_group(file_state)
    routed_path: Path | None = None
    if group_name:
        resolved = file_state.get("resolved_group_rel")
        if isinstance(resolved, str) and resolved.strip():
            routed_path = domain_models.path_under(
                root,
                Path(group_name) / domain_models.normalize_posix(resolved),
                label="routed input file",
            )
        elif rel_path.startswith(f"{group_name}/"):
            routed_path = original_path
    if routed_path is not None and routed_path.exists():
        return routed_path
    if original_path.exists():
        return original_path
    return routed_path or original_path


def file_matches_size(path: Path, expected_bytes: int) -> bool:
    try:
        return path.stat().st_size >= expected_bytes
    except FileNotFoundError:
        return False


def upload_file_status(file_state: dict[str, Any]) -> dict[str, Any]:
    file_state = dict(file_state)
    upload_id = str(file_state["file_upload_id"])
    data_path = tusd_data_path(upload_id)
    expected = int(file_state["bytes"])
    if file_state.get("consumed_at"):
        uploaded = expected
        state: domain_models.UploadState = "consumed"
    else:
        uploaded = data_path.stat().st_size if data_path.exists() else 0
        if uploaded >= expected:
            state = "uploaded"
        elif uploaded > 0:
            state = "partial"
        elif (shared_path := shared_input_file_path(file_state)) is not None and file_matches_size(
            shared_path,
            expected,
        ):
            uploaded = expected
            state = "uploaded"
        else:
            state = "pending"
    out = dict(file_state)
    out["uploaded_bytes"] = min(uploaded, expected)
    out["upload_state"] = state
    out["complete"] = state in {"uploaded", "consumed"}
    return out


def normalized_input_upload(upload: dict[str, Any]) -> dict[str, Any]:
    out = dict(upload)
    input_upload_id = str(out.get("input_upload_id") or "")
    files: list[dict[str, Any]] = []
    for file_state in out.get("files", []):
        if not isinstance(file_state, dict):
            continue
        item = dict(file_state)
        if input_upload_id:
            item.setdefault("input_upload_id", input_upload_id)
        files.append(item)
    out["files"] = files
    return out


def refresh_input_upload(upload: dict[str, Any]) -> dict[str, Any]:
    upload = normalized_input_upload(upload)
    files = [upload_file_status(file_state) for file_state in upload.get("files", [])]
    out = dict(upload)
    out["files"] = files
    out["files_total"] = len(files)
    out["files_uploaded"] = sum(1 for item in files if item["complete"])
    out["bytes_total"] = sum(int(item["bytes"]) for item in files)
    out["uploaded_bytes"] = sum(int(item["uploaded_bytes"]) for item in files)
    out["state"] = "uploaded" if out["files_uploaded"] == out["files_total"] else "uploading"
    return out


def save_input_upload(upload: dict[str, Any]) -> dict[str, Any]:
    upload = refresh_input_upload(upload)
    return save_input_upload_raw(upload)


def load_input_upload_raw(upload_id: str) -> dict[str, Any]:
    upload = state_store.read_state("input-upload", upload_id)
    if upload is None:
        raise ServiceError(status_code=404, detail=f"unknown input upload: {upload_id}")
    return normalized_input_upload(upload)


def save_input_upload_raw(upload: dict[str, Any]) -> dict[str, Any]:
    upload = normalized_input_upload(upload)
    upload_id = str(upload["input_upload_id"])
    expected_updated_at = str(upload.get("updated_at") or "")
    payload = dict(upload)
    payload["updated_at"] = utc_timestamp_now()
    encoded = json.dumps(payload, sort_keys=True)
    with (
        execution_runtime.input_upload_state_lock(upload_id),
        closing(state_store.state_db()) as conn,
    ):
        if expected_updated_at:
            result = conn.execute(
                """
                UPDATE states
                SET payload = ?, updated_at = ?
                WHERE kind = 'input-upload' AND id = ? AND updated_at = ?
                """,
                (encoded, payload["updated_at"], upload_id, expected_updated_at),
            )
            if result.rowcount != 1:
                conn.rollback()
                raise RuntimeError(f"stale input upload state: {upload_id}")
        else:
            try:
                conn.execute(
                    """
                    INSERT INTO states(kind, id, payload, updated_at)
                    VALUES('input-upload', ?, ?, ?)
                    """,
                    (upload_id, encoded, payload["updated_at"]),
                )
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                raise RuntimeError(f"stale input upload state: {upload_id}") from exc
        conn.commit()
    return payload


def remove_input_upload_data(upload: dict[str, Any]) -> None:
    upload = normalized_input_upload(upload)
    for file_state in upload.get("files", []):
        remove_input_file_data(file_state)
    shutil.rmtree(shared_input_upload_root(str(upload["input_upload_id"])), ignore_errors=True)


def remove_tusd_file_data(file_state: dict[str, Any]) -> None:
    tus_path = tusd_data_path(str(file_state["file_upload_id"]))
    tus_path.unlink(missing_ok=True)
    tus_path.with_suffix(tus_path.suffix + ".info").unlink(missing_ok=True)
    tus_path.with_suffix(tus_path.suffix + ".lock").unlink(missing_ok=True)


def remove_empty_parents(path: Path, root: Path) -> None:
    parent = path.parent
    while parent != root:
        try:
            parent.rmdir()
        except OSError:
            return
        parent = parent.parent
    try:
        root.rmdir()
    except OSError:
        pass


def remove_shared_input_file_data(file_state: dict[str, Any]) -> None:
    shared_path = shared_input_file_path(file_state)
    if shared_path is None:
        return
    root = shared_input_upload_root(str(file_state["input_upload_id"]))
    shared_path.unlink(missing_ok=True)
    remove_empty_parents(shared_path, root)


def remove_input_file_data(file_state: dict[str, Any]) -> None:
    remove_tusd_file_data(file_state)
    remove_shared_input_file_data(file_state)


def input_upload_last_activity(upload: dict[str, Any]) -> datetime:
    timestamps = [
        parsed
        for value in (upload.get("updated_at"), upload.get("created_at"))
        if (parsed := state_store.safe_parse_timestamp(value)) is not None
    ]
    for file_state in upload.get("files", []):
        tus_path = tusd_data_path(str(file_state["file_upload_id"]))
        for path in (
            tus_path,
            tus_path.with_suffix(tus_path.suffix + ".info"),
            tus_path.with_suffix(tus_path.suffix + ".lock"),
        ):
            if path.exists():
                timestamps.append(datetime.fromtimestamp(path.stat().st_mtime, UTC))
    return max(timestamps) if timestamps else datetime.now(UTC)


def input_upload_data_last_activity(upload: dict[str, Any]) -> datetime:
    timestamps = [
        parsed
        for value in (upload.get("created_at"),)
        if (parsed := state_store.safe_parse_timestamp(value)) is not None
    ]
    for file_state in upload.get("files", []):
        tus_path = tusd_data_path(str(file_state["file_upload_id"]))
        for path in (
            tus_path,
            tus_path.with_suffix(tus_path.suffix + ".info"),
            tus_path.with_suffix(tus_path.suffix + ".lock"),
        ):
            if path.exists():
                timestamps.append(datetime.fromtimestamp(path.stat().st_mtime, UTC))
    return max(timestamps) if timestamps else datetime.now(UTC)


def load_input_upload(upload_id: str) -> dict[str, Any]:
    return refresh_input_upload(load_input_upload_raw(upload_id))


def normalize_public_tusd_url(location: str) -> str:
    joined = urljoin(f"{runtime_config.TUSD_PUBLIC_BASE_URL}/", location)
    parsed = urlsplit(joined)
    public = urlsplit(runtime_config.TUSD_PUBLIC_BASE_URL)
    base_path = public.path.rstrip("/")
    prefix = f"{base_path}/"
    if not parsed.path.startswith(prefix):
        return joined
    upload_id = parsed.path.removeprefix(prefix)
    normalized_path = f"{prefix}{quote(upload_id, safe='+%')}"
    return urlunsplit(
        (
            public.scheme,
            public.netloc,
            normalized_path,
            parsed.query,
            parsed.fragment,
        )
    )


def tus_headers(**headers: str) -> dict[str, str]:
    out = {"Tus-Resumable": "1.0.0", **headers}
    if runtime_config.TUSD_HOOK_SECRET:
        out["X-Munchy-Tusd-Hook-Secret"] = runtime_config.TUSD_HOOK_SECRET
    return out


def create_tusd_upload(target_path: str, length: int) -> str:
    encoded = base64.b64encode(target_path.encode("utf-8")).decode("ascii")
    metadata = f"target_path {encoded}"
    with httpx.Client(timeout=300.0) as client:
        response = client.post(
            runtime_config.TUSD_INTERNAL_BASE_URL,
            headers=tus_headers(**{"Upload-Length": str(length), "Upload-Metadata": metadata}),
        )
        response.raise_for_status()
        return normalize_public_tusd_url(response.headers["Location"])


def head_tusd_upload(upload_url: str) -> int:
    internal = upload_url.replace(
        runtime_config.TUSD_PUBLIC_BASE_URL, runtime_config.TUSD_INTERNAL_BASE_URL, 1
    )
    with httpx.Client(timeout=60.0) as client:
        response = client.head(internal, headers=tus_headers())
    if response.status_code == 404:
        return -1
    response.raise_for_status()
    return int(response.headers.get("Upload-Offset", "0"))


def find_upload_file(upload: dict[str, Any], rel_path: str) -> dict[str, Any]:
    files = upload.get("files")
    if not isinstance(files, list):
        files = []
    for file_state in files:
        if not isinstance(file_state, dict):
            continue
        if file_state.get("path") == rel_path:
            return cast(dict[str, Any], file_state)
    raise ServiceError(status_code=404, detail=f"unknown upload file: {rel_path}")


LINK_COPY_FALLBACK_ERRNOS = {
    errno.EXDEV,
    errno.EPERM,
    errno.EACCES,
}


def link_or_copy(source: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(f".{dest.name}.{uuid.uuid4().hex}.part")
    try:
        try:
            os.link(source, part)
        except OSError as exc:
            if exc.errno not in LINK_COPY_FALLBACK_ERRNOS:
                raise RuntimeError(f"failed to link {source} to {dest}: {exc}") from exc
            try:
                shutil.copy2(source, part)
            except OSError as copy_exc:
                raise RuntimeError(f"failed to copy {source} to {dest}: {copy_exc}") from copy_exc
        part.replace(dest)
    finally:
        part.unlink(missing_ok=True)


def file_matches_expected(
    path: Path,
    expected_bytes: int,
    *,
    expected_sha256: str | None = None,
    verify_sha256: bool = False,
) -> bool:
    try:
        if path.stat().st_size != expected_bytes:
            return False
        return not verify_sha256 or not expected_sha256 or file_sha256(path) == expected_sha256
    except OSError:
        return False


def copy_tree_files(source_root: Path, dest_root: Path) -> None:
    if not source_root.is_dir():
        raise RuntimeError(f"input group is missing: {source_root}")
    for source in source_root.rglob("*"):
        if not source.is_file():
            continue
        rel_path = source.relative_to(source_root)
        dest = dest_root / rel_path
        if dest.exists() and dest.stat().st_size == source.stat().st_size:
            continue
        link_or_copy(source, dest)


def copy_preserve_group_files(
    upload: dict[str, Any],
    *,
    group_name: str,
    source_root: Path,
    dest_root: Path,
) -> None:
    if not source_root.is_dir():
        raise RuntimeError(f"input group is missing: {source_root}")
    for file_state in primary_upload_files_for_groups(upload, {group_name}):
        rel_path = upload_file_group_rel_for_state(file_state, group_name)
        source = source_root / rel_path
        dest = dest_root / rel_path
        if not source.is_file():
            raise RuntimeError(f"preserve source file is missing: {source}")
        if dest.exists() and dest.stat().st_size == source.stat().st_size:
            continue
        link_or_copy(source, dest)


def build_preserve_group_source_artifacts(
    upload: dict[str, Any],
    *,
    group_name: str,
    source_root: Path,
    output_root: Path,
    allow_missing_filesystem_metadata: bool = False,
) -> dict[str, Any]:
    filesystem_metadata = load_filesystem_metadata_map(source_root)
    if not filesystem_metadata and not allow_missing_filesystem_metadata:
        raise RuntimeError(
            "unresumable: source filesystem metadata sidecar is missing for preserve group"
        )
    items: list[dict[str, Any]] = []
    for file_state in primary_upload_files_for_groups(upload, {group_name}):
        rel_path = upload_file_group_rel_for_state(file_state, group_name)
        source = source_root / rel_path
        output = output_root / rel_path
        metadata = filesystem_metadata.get(rel_path.as_posix())
        if (not isinstance(metadata, Mapping) or not metadata) and not (
            allow_missing_filesystem_metadata
        ):
            raise RuntimeError(
                "unresumable: source filesystem metadata sidecar is missing entries for "
                f"{rel_path.as_posix()}"
            )
        artifacts = build_preserve_source_artifacts(
            source=source,
            output=output,
            source_filesystem_metadata=metadata,
            allow_missing_filesystem_metadata=allow_missing_filesystem_metadata,
            source_sidecars=source_artifacts_sidecar_entries(
                upload,
                [file_state],
                group_name=group_name,
                materialized_group_root=source_root,
            ).get(rel_path.as_posix(), []),
        )
        file_state["source_artifacts"] = artifacts
        items.append(
            {
                "source": str(source),
                "output": str(output),
                "source_artifacts": artifacts,
            }
        )
    return {"status": "succeeded", "items": items, "count": len(items)}


def upload_file_group(rel_path: str) -> str:
    return domain_models.input_path_group(rel_path)


def upload_file_group_rel(rel_path: str, group_name: str) -> Path:
    prefix = f"{group_name}/"
    if not rel_path.startswith(prefix):
        raise RuntimeError(f"input file {rel_path!r} is not in group {group_name!r}")
    group_rel = rel_path[len(prefix) :]
    if not group_rel:
        raise RuntimeError(f"input file {rel_path!r} does not include a file name")
    return Path(group_rel)


def upload_files_for_groups(
    upload: dict[str, Any],
    group_names: set[str],
) -> list[dict[str, Any]]:
    upload = normalized_input_upload(upload)
    return [
        file_state
        for file_state in upload.get("files", [])
        if upload_file_resolved_group(file_state) in group_names
    ]


def mutable_upload_files_for_groups(
    upload: dict[str, Any],
    group_names: set[str],
) -> list[dict[str, Any]]:
    return [
        file_state
        for file_state in upload.get("files", [])
        if isinstance(file_state, dict) and upload_file_resolved_group(file_state) in group_names
    ]


def upload_file_is_sidecar_evidence(file_state: Mapping[str, Any]) -> bool:
    return str(file_state.get("route_action") or "") == "evidence"


def primary_upload_files_for_groups(
    upload: dict[str, Any],
    group_names: set[str],
) -> list[dict[str, Any]]:
    return [
        file_state
        for file_state in upload_files_for_groups(upload, group_names)
        if not upload_file_is_sidecar_evidence(file_state)
    ]


def mutable_primary_upload_files_for_groups(
    upload: dict[str, Any],
    group_names: set[str],
) -> list[dict[str, Any]]:
    return [
        file_state
        for file_state in mutable_upload_files_for_groups(upload, group_names)
        if not upload_file_is_sidecar_evidence(file_state)
    ]


def sidecar_evidence_files_for_primary(
    upload: dict[str, Any],
    primary_file_state: Mapping[str, Any],
) -> list[dict[str, Any]]:
    primary_path = str(primary_file_state.get("path") or "")
    evidence = [
        file_state
        for file_state in upload.get("files", [])
        if isinstance(file_state, dict)
        and upload_file_is_sidecar_evidence(file_state)
        and str(file_state.get("sidecar_for") or "") == primary_path
    ]
    return sorted(evidence, key=lambda item: str(item.get("path") or ""))


def sidecar_evidence_files_for_primaries(
    upload: dict[str, Any],
    primary_file_states: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    by_path: dict[str, dict[str, Any]] = {}
    for primary in primary_file_states:
        for evidence in sidecar_evidence_files_for_primary(upload, primary):
            by_path[str(evidence["path"])] = evidence
    return [by_path[path] for path in sorted(by_path)]


def source_artifacts_sidecar_entries(
    upload: dict[str, Any],
    primary_file_states: Sequence[Mapping[str, Any]],
    *,
    group_name: str,
    materialized_group_root: Path,
    container_group_root: str | Path | None = None,
) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    root_for_payload = (
        Path(container_group_root) if container_group_root else materialized_group_root
    )
    for primary in primary_file_states:
        primary_rel = upload_file_group_rel_for_state(cast(dict[str, Any], primary), group_name)
        entries: list[dict[str, Any]] = []
        for evidence in sidecar_evidence_files_for_primary(upload, primary):
            evidence_rel = upload_file_group_rel_for_state(evidence, group_name)
            evidence_path = materialized_group_root / evidence_rel
            if not evidence_path.is_file():
                raise RuntimeError(f"source sidecar evidence is missing: {evidence_path}")
            entries.append(
                {
                    "id": str(evidence.get("sidecar_id") or ""),
                    "format": str(evidence.get("sidecar_format") or "opaque"),
                    "path": str(root_for_payload / evidence_rel),
                    "arcname": domain_models.normalize_posix(
                        PurePosixPath("sidecars", evidence_rel.as_posix()).as_posix()
                    ),
                    "source_rel_path": str(evidence.get("path") or ""),
                }
            )
        if entries:
            out[primary_rel.as_posix()] = entries
    return out


def upload_bytes_for_groups(upload: dict[str, Any], group_names: set[str]) -> int:
    return sum(
        int(file_state["bytes"]) for file_state in upload_files_for_groups(upload, group_names)
    )


def upload_group_names_with_files(upload: dict[str, Any], group_names: set[str]) -> set[str]:
    present_groups = input_upload_routed_groups(upload)
    return {str(group_name) for group_name in group_names if str(group_name) in present_groups}


def upload_groups_complete(upload: dict[str, Any], group_names: set[str]) -> bool:
    files = upload_files_for_groups(upload, group_names)
    return bool(files) and all(upload_file_status(file_state)["complete"] for file_state in files)


def shared_input_tree_progress(upload: dict[str, Any], group_names: set[str]) -> dict[str, int]:
    root = shared_input_upload_root(str(upload["input_upload_id"]))
    files_ready = 0
    bytes_ready = 0
    for file_state in upload_files_for_groups(upload, group_names):
        expected_bytes = int(file_state["bytes"])
        if file_state.get("consumed_at"):
            files_ready += 1
            bytes_ready += expected_bytes
            continue
        dest = root / materialized_input_rel_path(file_state)
        if file_matches_expected(dest, expected_bytes):
            files_ready += 1
            bytes_ready += expected_bytes
    return {
        "input_tree_files_ready": files_ready,
        "input_tree_bytes_ready": bytes_ready,
    }


def upload_group_progress(upload: dict[str, Any], group_names: set[str]) -> dict[str, Any]:
    files = [
        upload_file_status(file_state)
        for file_state in upload_files_for_groups(upload, group_names)
    ]
    bytes_total = sum(int(item["bytes"]) for item in files)
    uploaded_bytes = sum(int(item["uploaded_bytes"]) for item in files)
    tree_progress = shared_input_tree_progress(upload, group_names)
    return {
        "files_total": len(files),
        "files_uploaded": sum(1 for item in files if item["complete"]),
        "bytes_total": bytes_total,
        "uploaded_bytes": uploaded_bytes,
        **tree_progress,
    }


def cleanup_consumed_shared_input_files(
    upload: dict[str, Any],
    group_names: set[str] | None = None,
) -> int:
    upload = normalized_input_upload(upload)
    selected_groups = set(group_names or input_upload_groups(upload))
    removed = 0
    for file_state in upload_files_for_groups(upload, selected_groups):
        if not file_state.get("consumed_at"):
            continue
        shared_path = shared_input_file_path(file_state)
        if shared_path is None or not shared_path.exists():
            continue
        remove_shared_input_file_data(file_state)
        removed += 1
    return removed


def materialize_upload_file(
    file_state: dict[str, Any],
    dest_root: Path,
    *,
    verify_sha256: bool = False,
    consume_upload_source: bool = False,
) -> None:
    rel_path = str(file_state["path"])
    expected_bytes = int(file_state["bytes"])
    expected_sha256 = file_state.get("sha256")
    dest = dest_root / materialized_input_rel_path(file_state)
    if file_matches_expected(
        dest,
        expected_bytes,
        expected_sha256=expected_sha256,
        verify_sha256=verify_sha256,
    ):
        if consume_upload_source:
            remove_tusd_file_data(file_state)
        return
    status = upload_file_status(file_state)
    if status["upload_state"] == "consumed":
        raise RuntimeError(f"input file has already been consumed: {rel_path}")
    if not status["complete"]:
        raise RuntimeError(f"input file is incomplete: {rel_path}")
    tusd_source = tusd_data_path(str(file_state["file_upload_id"]))
    shared_source = shared_input_file_path(file_state)
    source = tusd_source if tusd_source.exists() else shared_source
    if source is None:
        raise RuntimeError(f"input file data is missing: {rel_path} ({tusd_source})")
    try:
        source_bytes = source.stat().st_size
    except FileNotFoundError as exc:
        if file_matches_expected(
            dest,
            expected_bytes,
            expected_sha256=expected_sha256,
            verify_sha256=verify_sha256,
        ):
            return
        raise RuntimeError(f"input file data is missing: {rel_path} ({source})") from exc
    if source_bytes < expected_bytes:
        raise RuntimeError(f"input file is incomplete: {rel_path}")
    if verify_sha256 and expected_sha256 and file_sha256(source) != expected_sha256:
        raise RuntimeError(f"input file sha256 mismatch: {rel_path}")
    try:
        link_or_copy(source, dest)
    except RuntimeError:
        alternate_source = shared_source if source == tusd_source else tusd_source
        if not source.exists() and alternate_source is not None and alternate_source.exists():
            link_or_copy(alternate_source, dest)
        else:
            raise
    if not file_matches_expected(
        dest,
        expected_bytes,
        expected_sha256=expected_sha256,
        verify_sha256=verify_sha256,
    ):
        raise RuntimeError(f"input file materialization failed: {rel_path}")
    if consume_upload_source:
        remove_tusd_file_data(file_state)


def materialize_upload_groups(
    upload: dict[str, Any],
    dest_root: Path,
    group_names: set[str],
) -> None:
    upload = refresh_input_upload(upload)
    if not upload_groups_complete(upload, group_names):
        raise RuntimeError("input upload groups are not complete")
    for file_state in upload_files_for_groups(upload, group_names):
        materialize_upload_file(file_state, dest_root)


def write_group_filesystem_metadata(
    root: Path,
    group_name: str,
    file_states: list[dict[str, Any]],
) -> None:
    records: dict[str, dict[str, Any]] = {}
    for file_state in file_states:
        metadata = file_state.get("filesystem_metadata")
        if not isinstance(metadata, dict):
            continue
        rel_path = upload_file_group_rel_for_state(file_state, group_name).as_posix()
        records[rel_path] = metadata
    write_filesystem_metadata_map(root / group_name, records, created_at=utc_timestamp_now())


def sync_shared_input_tree(
    upload: dict[str, Any],
    group_names: set[str] | None = None,
    *,
    job: dict[str, Any] | None = None,
) -> dict[str, int]:
    upload = refresh_input_upload(upload)
    selected_groups = set(group_names or input_upload_groups(upload))
    input_upload_id = str(upload["input_upload_id"])
    with execution_runtime.shared_input_tree_lock(input_upload_id):
        upload = refresh_input_upload(upload)
        root = shared_input_upload_root(input_upload_id)
        files = upload_files_for_groups(upload, selected_groups)
        root.mkdir(parents=True, exist_ok=True)
        cleanup_consumed_shared_input_files(upload, selected_groups)
        linked = 0
        skipped = 0
        for index, file_state in enumerate(files, start=1):
            status = upload_file_status(file_state)
            if status["upload_state"] == "consumed":
                remove_shared_input_file_data(file_state)
                skipped += 1
                continue
            if not status["complete"]:
                skipped += 1
                continue
            materialize_upload_file(file_state, root, consume_upload_source=True)
            linked += 1
            if job is not None and (index == len(files) or index % 100 == 0):
                progress = shared_input_tree_progress(upload, selected_groups)
                job["phase"] = f"preparing_input:{progress['input_tree_files_ready']}/{len(files)}"
                job["upload_progress"] = upload_group_progress(upload, selected_groups)
                state_store.save_job(job)
        for group_name in selected_groups:
            group_files = upload_files_for_groups(upload, {group_name})
            write_group_filesystem_metadata(root, group_name, group_files)
        return {"linked": linked, "skipped": skipped, "files": len(files)}


def sync_shared_input_file(upload_id: str, rel_path: str) -> bool:
    upload = load_input_upload_raw(upload_id)
    file_state = find_upload_file(upload, rel_path)
    if input_upload_storage_hint(upload).structured_routing and not file_state.get(
        "resolved_group"
    ):
        return False
    with execution_runtime.shared_input_tree_lock(upload_id):
        root = shared_input_upload_root(upload_id)
        root.mkdir(parents=True, exist_ok=True)
        status = upload_file_status(file_state)
        if status["upload_state"] == "consumed":
            remove_shared_input_file_data(file_state)
            return False
        if not status["complete"]:
            return False
        materialize_upload_file(file_state, root, consume_upload_source=True)
        return True


def shared_input_tree_metadata(
    upload: dict[str, Any],
    group_names: set[str],
) -> dict[str, Any]:
    files = upload_files_for_groups(upload, group_names)
    return {
        "input_upload_id": str(upload["input_upload_id"]),
        "groups": sorted(group_names),
        "files": len(files),
        "bytes": sum(int(file_state["bytes"]) for file_state in files),
    }


def shared_input_tree_ready(
    root: Path,
    upload: dict[str, Any],
    group_names: set[str],
) -> bool:
    marker = root / ".munchy-input-upload.json"
    if not marker.is_file():
        return False
    try:
        metadata = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    expected = shared_input_tree_metadata(upload, group_names)
    return all(metadata.get(key) == value for key, value in expected.items())


def prepare_shared_input_tree(
    upload: dict[str, Any],
    group_names: set[str],
    *,
    job: dict[str, Any] | None = None,
) -> Path:
    upload = refresh_input_upload(upload)
    if not upload_groups_complete(upload, group_names):
        raise RuntimeError("input upload groups are not complete")
    upload_id = str(upload["input_upload_id"])
    root = shared_input_upload_root(upload_id)
    files = upload_files_for_groups(upload, group_names)
    if shared_input_tree_ready(root, upload, group_names):
        return root
    sync_shared_input_tree(upload, group_names, job=job)
    progress = shared_input_tree_progress(upload, group_names)
    if progress["input_tree_files_ready"] != len(files):
        raise RuntimeError(
            "input upload groups are complete but shared input tree is incomplete: "
            f"{progress['input_tree_files_ready']}/{len(files)}"
        )
    metadata = {
        **shared_input_tree_metadata(upload, group_names),
        "prepared_at": utc_timestamp_now(),
    }
    marker = root / ".munchy-input-upload.json"
    part = marker.with_suffix(marker.suffix + ".part")
    part.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
    part.replace(marker)
    return root


def load_shared_review_plan(
    input_upload_id: str,
    group_name: str,
    task_name: str,
) -> dict[str, Any] | None:
    path = shared_review_plan_path(input_upload_id, group_name, task_name)
    if not path.is_file():
        return None
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return plan if isinstance(plan, dict) else None


def store_shared_review_plan(
    input_upload_id: str,
    group_name: str,
    task_name: str,
    plan: dict[str, Any],
) -> None:
    path = shared_review_plan_path(input_upload_id, group_name, task_name)
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        **plan,
        "shared_plan": {
            "input_upload_id": input_upload_id,
            "group": group_name,
            "task": task_name,
            "stored_at": utc_timestamp_now(),
        },
    }
    part = path.with_suffix(path.suffix + ".part")
    part.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    try:
        part.replace(path)
    finally:
        part.unlink(missing_ok=True)


def remember_review_plans_from_gpu_result(
    job: dict[str, Any],
    group_name: str,
    gpu_result: dict[str, Any],
) -> None:
    upload_id = str(job.get("input_upload_id") or "")
    if not upload_id:
        return
    items = gpu_result.get("items")
    if not isinstance(items, dict):
        return
    for task_name in ("qcut_video", "audio_review"):
        item = items.get(task_name)
        if not isinstance(item, dict):
            continue
        plan = item.get("plan")
        if isinstance(plan, dict):
            store_shared_review_plan(upload_id, group_name, task_name, plan)


def materialize_upload(upload: dict[str, Any], dest_root: Path) -> None:
    upload = refresh_input_upload(upload)
    if upload["state"] != "uploaded":
        raise RuntimeError("input upload is not complete")
    for file_state in upload["files"]:
        materialize_upload_file(file_state, dest_root)


def input_upload_groups(upload: dict[str, Any]) -> list[str]:
    groups = sorted(
        group
        for group in {
            upload_file_resolved_group(file_state) for file_state in upload.get("files", [])
        }
        if group
    )
    if not groups:
        raise RuntimeError("input upload does not contain any files")
    return groups


def input_upload_routed_groups(upload: dict[str, Any]) -> set[str]:
    return {
        group
        for group in {
            upload_file_resolved_group(file_state) for file_state in upload.get("files", [])
        }
        if group
    }


def profile_name_for(encode_profile: dict[str, Any] | None) -> str:
    if isinstance(encode_profile, dict) and encode_profile.get("name"):
        return str(encode_profile["name"])
    return "av1-nvenc-high"


def group_dump(group: domain_models.GroupConfig) -> dict[str, Any]:
    encode_profile = (
        group.encode_profile.server_payload() if group.encode_profile is not None else None
    )
    metadata_projection: bool | dict[str, Any]
    if group.metadata_projection is False:
        metadata_projection = False
    else:
        metadata_projection = group.metadata_projection.model_dump(exclude_none=True)
    payload: dict[str, Any] = {
        "output_mode": group.output_mode,
        "tasks": group.tasks,
        "profile": profile_name_for(encode_profile),
        "encode_profile": encode_profile,
        "allow_missing_filesystem_metadata": group.allow_missing_filesystem_metadata,
        "metadata_projection": metadata_projection,
    }
    if group.max_parallel_encodes is not None:
        payload["max_parallel_encodes"] = group.max_parallel_encodes
    if group.eager_pipeline_batches is not None:
        payload["eager_pipeline_batches"] = group.eager_pipeline_batches
    return payload


def default_group_config(req: domain_models.CreateJobRequest) -> domain_models.GroupConfig:
    return domain_models.GroupConfig(
        output_mode=req.output_mode,
        tasks=req.tasks,
        encode_profile=req.encode_profile,
        allow_missing_filesystem_metadata=req.allow_missing_filesystem_metadata,
    )
