from __future__ import annotations

import secrets
from urllib.parse import unquote, urlsplit

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from riverhog_core.app_permissions import COLLECTIONS_UPLOAD
from riverhog_core.runtime_config import load_runtime_config
from riverhog_protocol.errors import BadRequest
from starlette.concurrency import run_in_threadpool

from riverhog_api.auth import authenticate_authorization_header
from riverhog_api.deps import ContainerDep, default_container

router = APIRouter(tags=["internal"], include_in_schema=False)
_HOOK_SECRET_HEADER = "x-riverhog-tusd-hook-secret"
_TUSD_FILE_METHODS = {"HEAD", "PATCH", "DELETE", "OPTIONS"}


def _json_response(payload: dict[str, object], *, status_code: int = 200) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=payload)


def _hook_error(message: str, *, status_code: int = 400) -> JSONResponse:
    return _json_response(
        {
            "RejectUpload": True,
            "HTTPResponse": {
                "StatusCode": status_code,
                "Body": message,
                "Header": {"Content-Type": "text/plain"},
            },
        }
    )


def _upload_id_from_metadata(metadata: dict[object, object]) -> str:
    return str(metadata.get("upload_id", ""))


def _authorized_tusd_hook(request: Request) -> bool:
    config = load_runtime_config()
    supplied = request.headers.get(_HOOK_SECRET_HEADER, "")
    return bool(supplied) and secrets.compare_digest(supplied, config.tusd_hook_secret)


@router.get("/internal/tusd/auth")
def authorize_tusd_data_plane(request: Request, container: ContainerDep) -> Response:
    principal = authenticate_authorization_header(
        request.headers.get("authorization"),
        container,
    )
    if principal is None:
        return Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})
    if not principal.allows(COLLECTIONS_UPLOAD):
        return Response(status_code=403)

    method = request.headers.get("x-original-method", "").upper()
    if method not in _TUSD_FILE_METHODS:
        return Response(status_code=405)

    original_uri = request.headers.get("x-original-uri") or request.headers.get(
        "x-original-url", ""
    )
    original_path = unquote(urlsplit(original_uri).path)
    if not original_path.startswith("/files/.riverhog/uploads/by-target/"):
        return Response(status_code=403)
    upload_id = original_path.removeprefix("/files/")
    collection_id = container.collections.collection_id_for_upload_id(upload_id)
    if collection_id is None or not principal.allows_collection(
        COLLECTIONS_UPLOAD, collection_id
    ):
        return Response(status_code=403)
    return Response(status_code=204)


@router.post("/internal/tusd/hooks")
async def handle_tusd_hook(request: Request) -> JSONResponse:
    payload = await request.json()
    hook_type = payload.get("Type")
    if hook_type not in {"pre-create", "post-finish"}:
        return _json_response({})

    container = None
    principal = None
    hook_authorized = False
    if hook_type == "pre-create":
        authorized = _authorized_tusd_hook(request)
    else:
        container = default_container()
        hook_authorized = _authorized_tusd_hook(request)
        principal = authenticate_authorization_header(
            request.headers.get("authorization"),
            container,
        )
        authorized = hook_authorized or bool(
            principal is not None and principal.allows(COLLECTIONS_UPLOAD)
        )
    if not authorized:
        return _json_response({"RejectUpload": True}, status_code=403)

    event = payload.get("Event", {})
    upload = event.get("Upload", {}) if isinstance(event, dict) else {}
    metadata = upload.get("MetaData", {}) if isinstance(upload, dict) else {}
    if not isinstance(metadata, dict):
        return _hook_error("missing target_path metadata")

    upload_id = _upload_id_from_metadata(metadata)
    if not upload_id.startswith(".riverhog/uploads/by-target/"):
        return _hook_error("missing or invalid upload_id metadata")
    digest = upload_id.removeprefix(".riverhog/uploads/by-target/")
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        return _hook_error("missing or invalid upload_id metadata")

    if hook_type == "pre-create":
        return _json_response({"ChangeFileInfo": {"ID": upload_id}})

    if not hook_authorized:
        assert container is not None
        assert principal is not None
        collection_id = container.collections.collection_id_for_upload_id(upload_id)
        if collection_id is None or not principal.allows_collection(
            COLLECTIONS_UPLOAD, collection_id
        ):
            return _json_response({"RejectUpload": True}, status_code=403)

    try:
        assert container is not None
        await run_in_threadpool(
            container.collections.sync_finished_upload_id,
            upload_id,
        )
    except BadRequest as exc:
        return _hook_error(exc.message)
    return _json_response({})
