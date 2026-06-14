from __future__ import annotations

import base64
import binascii
import os
import secrets
from urllib.parse import unquote, urlsplit

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from riverhog_api.deps import default_container
from riverhog_core.domain.errors import BadRequest, HashMismatch
from riverhog_core.runtime_config import load_runtime_config
from riverhog_core.tusd_ids import tusd_upload_id_for_target_path

router = APIRouter(tags=["internal"], include_in_schema=False)
_HOOK_SECRET_HEADER = "x-riverhog-tusd-hook-secret"
_TUSD_FILE_METHODS = {"HEAD", "PATCH", "DELETE", "OPTIONS"}


def _json_response(payload: dict[str, object], *, status_code: int = 200) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=payload)


def _hook_error(message: str) -> JSONResponse:
    return _json_response(
        {
            "RejectUpload": True,
            "HTTPResponse": {
                "StatusCode": 400,
                "Body": message,
                "Header": {"Content-Type": "text/plain"},
            },
        }
    )


def _target_path_from_metadata(metadata: dict[object, object]) -> str:
    target_path_b64 = metadata.get("target_path_b64")
    if target_path_b64:
        try:
            return base64.b64decode(str(target_path_b64), validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return ""
    return str(metadata.get("target_path", ""))


def _authorized_bearer(request: Request) -> bool:
    expected = os.getenv("RIVERHOG_API_TOKEN", "")
    if not expected:
        return True
    raw = request.headers.get("authorization", "")
    scheme, _, token = raw.partition(" ")
    return scheme.casefold() == "bearer" and secrets.compare_digest(token, expected)


@router.get("/internal/tusd/auth")
def authorize_tusd_data_plane(request: Request) -> Response:
    if not _authorized_bearer(request):
        return Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})

    method = request.headers.get("x-original-method", "").upper()
    if method not in _TUSD_FILE_METHODS:
        return Response(status_code=405)

    original_uri = request.headers.get("x-original-uri") or request.headers.get(
        "x-original-url", ""
    )
    original_path = unquote(urlsplit(original_uri).path)
    if not original_path.startswith("/files/.riverhog/uploads/by-target/"):
        return Response(status_code=403)
    return Response(status_code=204)


@router.post("/internal/tusd/hooks")
async def handle_tusd_hook(request: Request) -> JSONResponse:
    config = load_runtime_config()
    if request.headers.get(_HOOK_SECRET_HEADER) != config.tusd_hook_secret:
        return _json_response({"RejectUpload": True}, status_code=403)

    payload = await request.json()
    hook_type = payload.get("Type")
    if hook_type not in {"pre-create", "post-finish"}:
        return _json_response({})

    event = payload.get("Event", {})
    upload = event.get("Upload", {}) if isinstance(event, dict) else {}
    metadata = upload.get("MetaData", {}) if isinstance(upload, dict) else {}
    if not isinstance(metadata, dict):
        return _hook_error("missing target_path metadata")

    raw_target_path = _target_path_from_metadata(metadata).lstrip("/")
    if not raw_target_path:
        return _hook_error("missing target_path metadata")
    if raw_target_path.startswith("collections/"):
        return _hook_error("target_path must not point into committed hot storage")
    if not raw_target_path.startswith(".riverhog/uploads/"):
        return _hook_error("target_path must stay within .riverhog/uploads/")
    if any(part in {"", ".", ".."} for part in raw_target_path.split("/")):
        return _hook_error("target_path must be normalized")

    if hook_type == "pre-create":
        return _json_response(
            {"ChangeFileInfo": {"ID": tusd_upload_id_for_target_path(raw_target_path)}}
        )

    try:
        default_container().collections.sync_finished_upload_target(raw_target_path)
    except BadRequest as exc:
        return _hook_error(exc.message)
    except HashMismatch as exc:
        return _hook_error(exc.message)
    return _json_response({})
