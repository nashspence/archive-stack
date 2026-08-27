"""Exercise one resumable write across a real storage-adapter process restart."""

from __future__ import annotations

import argparse
import json
import os
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from riverhog_core.runtime_config import load_runtime_config
from riverhog_storage_adapter_protocol import (
    AbortIncompleteWritesRequest,
    AdapterDescriptor,
    CompletedWriteLookupRequest,
    DeletePrefixRequest,
    ObjectLocator,
    ObjectReadRequest,
    WriteCompleteRequest,
    WriteSegmentReceipt,
    WriteSession,
    WriteStartRequest,
    validate_completed_write_response,
)
from riverhog_storage_adapter_support import StorageAdapterClient
from time_formats import format_utc_timestamp

_STATE_FORMAT = "riverhog-storage-adapter-restart-probe/v1"
_SECOND_SEGMENT = b"continued after adapter restart\n"


def _client() -> StorageAdapterClient:
    config = load_runtime_config()
    registration = config.archive_store(config.archive_write_store)
    return StorageAdapterClient.from_token_file(
        registration.base_url,
        token_file=registration.token_file,
        allow_insecure_http=registration.allow_insecure_http,
        timeout=registration.timeout_seconds,
        maximum_connections=registration.maximum_connections,
    )


def _write_state(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def prepare(path: Path) -> None:
    client = _client()
    prefix = f"adapter-restart-probe/{uuid.uuid4().hex}"
    request = WriteStartRequest(
        object_path=f"{prefix}/continued.bin",
        content_type="application/octet-stream",
        required_identity_assertions={"riverhog-conformance": "restart-continuation/v1"},
        placement="immediate",
    )
    abort_request = request.model_copy(update={"object_path": f"{prefix}/aborted.bin"})
    try:
        descriptor = client.descriptor()
        first_content = b"a" * descriptor.minimum_nonfinal_segment_bytes
        session = client.begin_write(request)
        first_segment = client.write_segment(
            session=session,
            number=1,
            stored_bytes=len(first_content),
            content=first_content,
        )
        abort_session = client.begin_write(abort_request)
        _write_state(
            path,
            {
                "format": _STATE_FORMAT,
                "prefix": prefix,
                "descriptor": descriptor.model_dump(mode="json"),
                "request": request.model_dump(mode="json"),
                "session": session.model_dump(mode="json"),
                "first_segment": first_segment.model_dump(mode="json"),
                "first_segment_bytes": len(first_content),
                "abort_session": abort_session.model_dump(mode="json"),
            },
        )
    except BaseException:
        _cleanup(client, prefix)
        raise
    finally:
        client.close()


def resume(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or set(payload) != {
        "format",
        "prefix",
        "descriptor",
        "request",
        "session",
        "first_segment",
        "first_segment_bytes",
        "abort_session",
    }:
        raise ValueError("storage-adapter restart probe state is invalid")
    if payload["format"] != _STATE_FORMAT:
        raise ValueError("storage-adapter restart probe state format is invalid")
    prefix = str(payload["prefix"])
    descriptor = AdapterDescriptor.model_validate(payload["descriptor"])
    request = WriteStartRequest.model_validate(payload["request"])
    session = WriteSession.model_validate(payload["session"])
    first_segment = WriteSegmentReceipt.model_validate(payload["first_segment"])
    first_segment_bytes = int(str(payload["first_segment_bytes"]))
    abort_session = WriteSession.model_validate(payload["abort_session"])
    if first_segment_bytes != first_segment.stored_bytes:
        raise ValueError("storage-adapter restart probe segment length changed")

    client = _client()
    try:
        if client.descriptor() != descriptor:
            raise RuntimeError("restarted storage adapter descriptor changed")
        if client.list_segments(session).segments != (first_segment,):
            raise RuntimeError("restarted storage adapter lost its committed segment")
        second_segment = client.write_segment(
            session=session,
            number=2,
            stored_bytes=len(_SECOND_SEGMENT),
            content=_SECOND_SEGMENT,
        )
        segments = (first_segment, second_segment)
        completion = WriteCompleteRequest(
            session=session,
            segments=segments,
            expected_bytes=first_segment_bytes + len(_SECOND_SEGMENT),
            expected_content_type=request.content_type,
            required_identity_assertions=request.required_identity_assertions,
            expected_placement=request.placement,
        )
        completed = client.complete_write(completion)
        validate_completed_write_response(completion, completed)
        recovered = client.find_completed_write(
            CompletedWriteLookupRequest(
                object_path=request.object_path,
                expected_content_type=request.content_type,
                required_identity_assertions=request.required_identity_assertions,
                expected_placement=request.placement,
            )
        )
        if recovered != completed:
            raise RuntimeError("restart completion reconciliation changed its receipt")
        stored = b"".join(
            client.read_object(
                ObjectReadRequest(
                    object=ObjectLocator(
                        object_path=completed.object_path,
                        revision=completed.revision,
                    ),
                    expected_bytes=completed.stored_bytes,
                )
            ).content
        )
        if stored != b"a" * first_segment_bytes + _SECOND_SEGMENT:
            raise RuntimeError("restart continuation changed the stored bytes")
        client.abort_write(abort_session)
        client.abort_write(abort_session)
        return {
            "format": _STATE_FORMAT,
            "checks": [
                "adapter-process-restart",
                "segment-reconciliation",
                "continued-write",
                "completion-reconciliation",
                "idempotent-abort",
            ],
            "stored_bytes": completed.stored_bytes,
        }
    finally:
        _cleanup(client, prefix)
        client.close()


def _cleanup(client: StorageAdapterClient, prefix: str) -> None:
    cutoff = format_utc_timestamp(datetime.now(UTC) + timedelta(seconds=1))
    client.abort_incomplete_writes(
        AbortIncompleteWritesRequest(
            object_prefix=f"{prefix}/",
            initiated_before=cutoff,
        )
    )
    client.delete_prefix(DeletePrefixRequest(object_prefix=f"{prefix}/"))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="storage-adapter-restart-probe")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("prepare", "resume"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("state", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "prepare":
        prepare(args.state)
        return 0
    print(json.dumps(resume(args.state), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main", "prepare", "resume"]
