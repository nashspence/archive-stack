"""Consumer-runnable conformance probe for independently maintained adapters."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import uuid
from collections.abc import Sequence
from pathlib import Path

from riverhog_storage_adapter_protocol import (
    CompleteUploadRequest,
    ObjectLocator,
    ReadRequest,
    UploadDeclaration,
    UploadDeclarationPayload,
    WriteCondition,
    normalize_object_path,
)

from riverhog_storage_adapter_support.client import StorageAdapterClient

_DISTRIBUTION = "riverhog-storage-adapter-support"


def _version() -> str:
    try:
        return importlib.metadata.version(_DISTRIBUTION)
    except importlib.metadata.PackageNotFoundError:
        return "development"


def conformance_report(
    client: StorageAdapterClient,
    *,
    object_prefix: str | None = None,
) -> dict[str, object]:
    """Inspect a descriptor and optionally exercise one disposable exact object."""

    descriptor = client.descriptor()
    report: dict[str, object] = {
        "format": "riverhog-storage-adapter-conformance/v1",
        "descriptor": descriptor.model_dump(mode="json"),
        "status": "ok",
    }
    if object_prefix is None:
        return report

    prefix = normalize_object_path(object_prefix, allow_prefix=True)
    transfer_id = f"conformance-{uuid.uuid4()}"
    object_path = f"{prefix}/objects/{transfer_id}.bin"
    content = b"riverhog-storage-adapter-conformance\n"
    declaration = UploadDeclaration.seal(
        UploadDeclarationPayload(
            transfer_id=transfer_id,
            object_path=object_path,
            content_type="application/octet-stream",
            stored_bytes=len(content),
            runtime_descriptor_sha256=descriptor.runtime_descriptor_sha256,
            condition=WriteCondition(),
        )
    )
    accepted = client.put_upload(declaration)
    repeated = client.put_upload(declaration)
    if accepted.declaration != repeated.declaration:
        raise RuntimeError("repeated upload declaration changed identity")
    part = client.put_part(
        transfer_id=transfer_id,
        number=1,
        content=content,
    )
    completion = CompleteUploadRequest(
        parts=(part,),
        stored_bytes=len(content),
        stored_sha256=hashlib.sha256(content).hexdigest(),
    )
    receipt = client.complete_upload(
        transfer_id=transfer_id,
        completion=completion,
    )
    if client.complete_upload(transfer_id=transfer_id, completion=completion) != receipt:
        raise RuntimeError("repeated upload completion changed identity")
    locator = ObjectLocator(object_path=object_path, revision=receipt.revision)
    metadata = client.object_metadata(locator)
    if metadata != receipt:
        raise RuntimeError("object metadata differs from its completion receipt")
    read_request = ReadRequest(objects=(locator,))
    prepared = client.prepare_read(read_request)
    observed = client.read_status(read_request)
    if observed.state == "ready":
        recovered = b"".join(client.iter_object_content(locator, chunk_bytes=7))
        ranged = b"".join(client.iter_object_content(locator, offset=3, size=11, chunk_bytes=4))
        if recovered != content or ranged != content[3:14]:
            raise RuntimeError("adapter content differs from the written bytes")
    client.cleanup_read(read_request)
    client.delete_object(locator)
    client.delete_upload(transfer_id)
    report["object_probe"] = {
        "request_sha256": declaration.request_sha256,
        "stored_sha256": receipt.stored_sha256,
        "stored_bytes": receipt.stored_bytes,
        "prepared_state": prepared.state,
        "observed_state": observed.state,
    }
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="riverhog-storage-adapter-conformance",
        description="Check one deployed Riverhog storage adapter's v1 contract.",
    )
    parser.add_argument("--version", action="version", version=_version())
    parser.add_argument("base_url")
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument(
        "--allow-insecure-http",
        action="store_true",
        help="Explicitly permit plaintext transport to a trusted single-tenant LAN adapter.",
    )
    parser.add_argument(
        "--object-prefix",
        help="Run the destructive disposable-object probe beneath this owned prefix.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    client = StorageAdapterClient.from_token_file(
        args.base_url,
        token_file=args.token_file,
        allow_insecure_http=args.allow_insecure_http,
    )
    try:
        report = conformance_report(client, object_prefix=args.object_prefix)
    finally:
        client.close()
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


__all__ = ["conformance_report", "main"]
