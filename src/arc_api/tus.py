from __future__ import annotations

from fastapi import Request

from arc_core.domain.errors import BadRequest

TUS_RESUMABLE = "1.0.0"
TUS_EXTENSIONS = "checksum,expiration,termination"
TUS_CHECKSUM_ALGORITHMS = "sha256"
TUS_CHUNK_CONTENT_TYPE = "application/offset+octet-stream"


def tus_upload_headers(payload: dict[str, object], *, request: Request) -> dict[str, str]:
    headers = {
        "Tus-Resumable": TUS_RESUMABLE,
        "Cache-Control": "no-store",
        "Upload-Offset": str(payload["offset"]),
        "Upload-Length": str(payload["length"]),
        "Location": str(request.url),
    }
    if payload.get("expires_at") is not None:
        headers["Upload-Expires"] = str(payload["expires_at"])
    return headers


def tus_delete_headers() -> dict[str, str]:
    return {
        "Tus-Resumable": TUS_RESUMABLE,
        "Cache-Control": "no-store",
    }


def tus_options_headers() -> dict[str, str]:
    return {
        "Tus-Resumable": TUS_RESUMABLE,
        "Tus-Version": TUS_RESUMABLE,
        "Tus-Extension": TUS_EXTENSIONS,
        "Tus-Checksum-Algorithm": TUS_CHECKSUM_ALGORITHMS,
    }


def validate_tus_chunk_request(request: Request) -> tuple[int, str]:
    raw_offset = request.headers.get("Upload-Offset")
    raw_checksum = request.headers.get("Upload-Checksum")
    tus_resumable = request.headers.get("Tus-Resumable")
    if raw_offset is None:
        raise BadRequest("missing Upload-Offset header")
    if raw_checksum is None:
        raise BadRequest("missing Upload-Checksum header")
    if tus_resumable != TUS_RESUMABLE:
        raise BadRequest(f"Tus-Resumable header must be {TUS_RESUMABLE}")

    content_type = request.headers.get("Content-Type", "")
    media_type = content_type.split(";", 1)[0].strip().casefold()
    if media_type != TUS_CHUNK_CONTENT_TYPE:
        raise BadRequest(f"Content-Type header must be {TUS_CHUNK_CONTENT_TYPE}")

    try:
        offset = int(raw_offset)
    except ValueError as exc:
        raise BadRequest("Upload-Offset header must be an integer") from exc
    return offset, raw_checksum
