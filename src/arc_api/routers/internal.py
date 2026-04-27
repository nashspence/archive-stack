from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from arc_core.runtime_config import load_runtime_config

router = APIRouter(tags=["internal"], include_in_schema=False)
_HOOK_SECRET_HEADER = "x-arc-tusd-hook-secret"


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


@router.post("/internal/tusd/hooks")
async def handle_tusd_hook(request: Request) -> JSONResponse:
    config = load_runtime_config()
    if request.headers.get(_HOOK_SECRET_HEADER) != config.tusd_hook_secret:
        return _json_response({"RejectUpload": True}, status_code=403)

    payload = await request.json()
    if payload.get("Type") != "pre-create":
        return _json_response({})

    event = payload.get("Event", {})
    upload = event.get("Upload", {}) if isinstance(event, dict) else {}
    metadata = upload.get("MetaData", {}) if isinstance(upload, dict) else {}
    if not isinstance(metadata, dict):
        return _hook_error("missing target_path metadata")

    raw_target_path = str(metadata.get("target_path", "")).lstrip("/")
    if not raw_target_path:
        return _hook_error("missing target_path metadata")
    if raw_target_path.startswith("collections/"):
        return _hook_error("target_path must not point into committed hot storage")
    if not raw_target_path.startswith(".arc/uploads/"):
        return _hook_error("target_path must stay within .arc/uploads/")
    if any(part in {"", ".", ".."} for part in raw_target_path.split("/")):
        return _hook_error("target_path must be normalized")

    return _json_response({"ChangeFileInfo": {"ID": raw_target_path}})
