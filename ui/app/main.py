from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import REQUEST_TIMEOUT_SECONDS, RIVERHOG_API_BASE_URL, RIVERHOG_API_TOKEN

APP_ROOT = Path(__file__).resolve().parent

app = FastAPI(title="Riverhog UI", version="0.1.0")
app.mount("/static", StaticFiles(directory=str(APP_ROOT / "static")), name="static")

templates = Jinja2Templates(directory=str(APP_ROOT / "templates"))


class ApiError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(message)


def human_bytes(value: int | None) -> str:
    if value is None:
        return "-"
    size = float(value)
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    unit = units[0]
    for unit in units:
        if size < 1024 or unit == units[-1]:
            break
        size /= 1024
    if unit == "B":
        return f"{int(size)} {unit}"
    return f"{size:.1f} {unit}"


def quote_path(value: str) -> str:
    return quote(value, safe="/")


templates.env.filters["human_bytes"] = human_bytes
templates.env.globals["quote_path"] = quote_path

ALREADY_UPLOADED_DETAIL = "file already uploaded for this collection"


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {RIVERHOG_API_TOKEN}"}


def _api_url(path: str) -> str:
    return f"{RIVERHOG_API_BASE_URL}{path}"


def _api_client() -> httpx.Client:
    return httpx.Client(
        headers=_auth_headers(),
        timeout=REQUEST_TIMEOUT_SECONDS,
        follow_redirects=True,
    )


def _error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except json.JSONDecodeError:
        text = response.text.strip()
        return text or f"request failed with status {response.status_code}"
    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, str):
            return detail
        if isinstance(detail, list):
            return "; ".join(str(item) for item in detail)
        message = body.get("message")
        if isinstance(message, str):
            return message
    return f"request failed with status {response.status_code}"


def _api_json(method: str, path: str, **kwargs: Any) -> Any:
    with _api_client() as client:
        response = client.request(method, _api_url(path), **kwargs)
    if response.status_code >= 400:
        raise ApiError(response.status_code, _error_message(response))
    if not response.content:
        return {}
    return response.json()


def _load_json(path: str) -> tuple[Any | None, str | None]:
    try:
        return _api_json("GET", path), None
    except ApiError as exc:
        return None, exc.message


def _redirect(url: str, *, message: str | None = None, error: str | None = None) -> RedirectResponse:
    split = urlsplit(url)
    params = dict(parse_qsl(split.query, keep_blank_values=True))
    if message:
        params["message"] = message
    if error:
        params["error"] = error
    target = urlunsplit((split.scheme, split.netloc, split.path, urlencode(params), split.fragment))
    return RedirectResponse(url=target, status_code=303)


def _render(request: Request, template_name: str, **context: Any) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name=template_name,
        context={
            "flash_message": request.query_params.get("message"),
            "flash_error": request.query_params.get("error"),
            **context,
        },
    )


def _tus_metadata_header(metadata: dict[str, str]) -> str:
    items = []
    for key, value in metadata.items():
        encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
        items.append(f"{key} {encoded}")
    return ",".join(items)


def _upload_to_tusd(slot: dict[str, Any], payload: bytes) -> None:
    headers = {
        "Tus-Resumable": "1.0.0",
        "Upload-Length": str(len(payload)),
        "Upload-Metadata": _tus_metadata_header(slot["tus_metadata"]),
    }
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=True) as client:
            create_response = client.post(slot["tus_create_url"], headers=headers)
            if create_response.status_code >= 400:
                raise ApiError(create_response.status_code, _error_message(create_response))

            location = create_response.headers.get("Location")
            if not location:
                raise ApiError(502, "tusd did not return an upload location")
            upload_url = urljoin(slot["tus_create_url"], location)

            patch_response = client.patch(
                upload_url,
                headers={
                    "Tus-Resumable": "1.0.0",
                    "Content-Type": "application/offset+octet-stream",
                    "Upload-Offset": "0",
                },
                content=payload,
            )
            if patch_response.status_code >= 400:
                raise ApiError(patch_response.status_code, _error_message(patch_response))
    except httpx.HTTPError as exc:
        raise ApiError(502, f"could not reach tusd upload service at {slot['tus_create_url']}") from exc


def _proxy_stream(path: str, request: Request | None = None) -> Response:
    client = _api_client()
    forwarded_headers: dict[str, str] = {}
    method = "GET"
    if request is not None:
        method = request.method
        for name in ["range", "if-none-match", "if-modified-since"]:
            value = request.headers.get(name)
            if value:
                forwarded_headers[name] = value

    upstream_request = client.build_request(method, _api_url(path), headers=forwarded_headers)
    upstream = client.send(upstream_request, stream=True)
    if upstream.status_code >= 400:
        message = _error_message(upstream)
        upstream.close()
        client.close()
        raise HTTPException(status_code=upstream.status_code, detail=message)

    headers = {}
    for name in [
        "accept-ranges",
        "cache-control",
        "content-disposition",
        "content-length",
        "content-range",
        "content-type",
        "etag",
        "last-modified",
    ]:
        value = upstream.headers.get(name)
        if value:
            headers[name] = value

    if method == "HEAD":
        upstream.close()
        client.close()
        return Response(status_code=upstream.status_code, headers=headers)

    def iterator():
        try:
            for chunk in upstream.iter_bytes():
                yield chunk
        finally:
            upstream.close()
            client.close()

    return StreamingResponse(iterator(), status_code=upstream.status_code, headers=headers)


def _collection_summary(collection_id: str) -> tuple[dict[str, Any] | None, str | None]:
    payload, error = _load_json("/v1/collections")
    if error or payload is None:
        return None, error
    for collection in payload.get("collections", []):
        if collection["collection_id"] == collection_id:
            return collection, None
    return None, "collection not found"


def _container_summary(container_id: str) -> tuple[dict[str, Any] | None, str | None]:
    payload, error = _load_json("/v1/containers")
    if error or payload is None:
        return None, error
    for container in payload.get("containers", []):
        if container["container_id"] == container_id:
            return container, None
    return None, "container not found"


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    collections_payload, collections_error = _load_json("/v1/collections")
    containers_payload, containers_error = _load_json("/v1/containers")
    return _render(
        request,
        "dashboard.html",
        collections=(collections_payload or {}).get("collections", []),
        containers=(containers_payload or {}).get("containers", []),
        collections_error=collections_error,
        containers_error=containers_error,
    )


@app.post("/collections")
def create_collection(
    root_node_name: str = Form(...),
    description: str = Form(""),
    keep_buffer_after_archive: bool = Form(False),
):
    try:
        payload = _api_json(
            "POST",
            "/v1/collections",
            json={
                "root_node_name": root_node_name,
                "description": description or None,
                "keep_buffer_after_archive": keep_buffer_after_archive,
            },
        )
    except ApiError as exc:
        return _redirect("/", error=exc.message)
    return _redirect(f"/collections/{payload['collection_id']}", message="Collection created.")


@app.post("/containers/flush")
def flush_containers(force: bool = Form(False)):
    try:
        payload = _api_json("POST", f"/v1/containers/flush?force={'true' if force else 'false'}")
    except ApiError as exc:
        return _redirect("/", error=exc.message)
    count = len(payload.get("closed_containers", []))
    return _redirect("/", message=f"Flush completed. Closed containers: {count}.")


@app.post("/containers/finalization-webhooks")
def create_webhook(
    webhook_url: str = Form(...),
    reminder_interval_seconds: int | None = Form(None),
):
    body: dict[str, Any] = {"webhook_url": webhook_url}
    if reminder_interval_seconds:
        body["reminder_interval_seconds"] = reminder_interval_seconds
    try:
        payload = _api_json("POST", "/v1/containers/finalization-webhooks", json=body)
    except ApiError as exc:
        return _redirect("/", error=exc.message)
    return _redirect("/", message=f"Webhook created. Pending containers: {payload['pending_container_count']}.")


@app.get("/collections/{collection_id}", response_class=HTMLResponse)
def collection_page(request: Request, collection_id: str) -> HTMLResponse:
    collection, collection_error = _collection_summary(collection_id)
    tree_payload, tree_error = _load_json(f"/v1/collections/{collection_id}/tree")
    if collection is None and collection_error == "collection not found":
        raise HTTPException(status_code=404, detail=collection_error)
    return _render(
        request,
        "collection.html",
        collection=collection,
        collection_error=collection_error,
        tree=(tree_payload or {}).get("nodes", []),
        tree_error=tree_error,
    )


@app.post("/collections/{collection_id}/directories")
def add_collection_directory(collection_id: str, relative_path: str = Form(...)):
    try:
        _api_json("POST", f"/v1/collections/{collection_id}/directories", json={"relative_path": relative_path})
    except ApiError as exc:
        return _redirect(f"/collections/{collection_id}", error=exc.message)
    return _redirect(f"/collections/{collection_id}", message="Directory created.")


@app.post("/collections/{collection_id}/seal")
def seal_collection(collection_id: str):
    try:
        payload = _api_json("POST", f"/v1/collections/{collection_id}/seal")
    except ApiError as exc:
        return _redirect(f"/collections/{collection_id}", error=exc.message)
    closed_count = len(payload.get("closed_containers", []))
    return _redirect(f"/collections/{collection_id}", message=f"Collection sealed. Closed containers: {closed_count}.")


@app.post("/collections/{collection_id}/release-buffer")
def release_collection_buffer(collection_id: str):
    try:
        _api_json("POST", f"/v1/collections/{collection_id}/buffer/release")
    except ApiError as exc:
        return _redirect(f"/collections/{collection_id}", error=exc.message)
    return _redirect(f"/collections/{collection_id}", message="Collection buffer released.")


@app.post("/collections/{collection_id}/upload-files")
async def upload_collection_file(
    collection_id: str,
    file: UploadFile = File(...),
    relative_path: str = Form(...),
    size_bytes: int = Form(...),
    mode: str = Form("0644"),
    mtime: str = Form(...),
    uid: int | None = Form(None),
    gid: int | None = Form(None),
):
    payload = await file.read()
    if len(payload) != size_bytes:
        return JSONResponse(status_code=400, content={"detail": "uploaded bytes did not match the declared size"})
    try:
        slot = _api_json(
            "POST",
            f"/v1/collections/{collection_id}/uploads",
            json={
                "relative_path": relative_path,
                "size_bytes": size_bytes,
                "mode": mode,
                "mtime": mtime,
                "uid": uid,
                "gid": gid,
            },
        )
        _upload_to_tusd(slot, payload)
    except ApiError as exc:
        if exc.status_code == 409 and exc.message == ALREADY_UPLOADED_DETAIL:
            return JSONResponse({"status": "skipped", "relative_path": relative_path, "detail": exc.message})
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})
    return JSONResponse({"status": "ok", "relative_path": relative_path})


@app.api_route("/collections/{collection_id}/content/{relative_path:path}", methods=["GET", "HEAD"])
def download_collection_content(request: Request, collection_id: str, relative_path: str):
    return _proxy_stream(f"/v1/collections/{collection_id}/content/{quote_path(relative_path)}", request)


@app.api_route("/collections/{collection_id}/hash-manifest-proof", methods=["GET", "HEAD"])
def download_collection_hash_manifest(request: Request, collection_id: str):
    return _proxy_stream(f"/v1/collections/{collection_id}/hash-manifest-proof", request)


@app.get("/containers/{container_id}", response_class=HTMLResponse)
def container_page(request: Request, container_id: str) -> HTMLResponse:
    container, container_error = _container_summary(container_id)
    tree_payload, tree_error = _load_json(f"/v1/containers/{container_id}/tree")
    if container is None and container_error == "container not found":
        raise HTTPException(status_code=404, detail=container_error)

    activation_session = request.query_params.get("activation_session")
    activation_expected = None
    activation_error = None
    if activation_session:
        activation_expected, activation_error = _load_json(
            f"/v1/containers/{container_id}/activation/sessions/{activation_session}/expected"
        )

    download_session = request.query_params.get("download_session")

    return _render(
        request,
        "container.html",
        container=container,
        container_error=container_error,
        tree=(tree_payload or {}).get("nodes", []),
        tree_error=tree_error,
        activation_session=activation_session,
        activation_expected=activation_expected,
        activation_error=activation_error,
        download_session=download_session,
    )


@app.post("/containers/{container_id}/activation-sessions")
def create_activation_session(container_id: str):
    try:
        payload = _api_json("POST", f"/v1/containers/{container_id}/activation/sessions")
    except ApiError as exc:
        return _redirect(f"/containers/{container_id}", error=exc.message)
    return _redirect(
        f"/containers/{container_id}?{urlencode({'activation_session': payload['session_id']})}",
        message="Activation session created.",
    )


@app.post("/containers/{container_id}/activation-sessions/{session_id}/upload-files")
async def upload_activation_file(
    container_id: str,
    session_id: str,
    file: UploadFile = File(...),
    relative_path: str = Form(...),
):
    payload = await file.read()
    try:
        slot = _api_json(
            "POST",
            f"/v1/containers/{container_id}/activation/sessions/{session_id}/uploads",
            json={"relative_path": relative_path},
        )
        _upload_to_tusd(slot, payload)
    except ApiError as exc:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})
    return JSONResponse({"status": "ok", "relative_path": relative_path})


@app.post("/containers/{container_id}/activation-sessions/{session_id}/complete")
def complete_activation_session(container_id: str, session_id: str):
    try:
        payload = _api_json("POST", f"/v1/containers/{container_id}/activation/sessions/{session_id}/complete")
    except ApiError as exc:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})
    return JSONResponse(
        {
            "status": "ok",
            "redirect_url": f"/containers/{container_id}?message={quote('Activation completed.', safe='')}",
            "result": payload,
        }
    )


@app.post("/containers/{container_id}/deactivate")
def deactivate_container(container_id: str):
    try:
        _api_json("DELETE", f"/v1/containers/{container_id}/activation")
    except ApiError as exc:
        return _redirect(f"/containers/{container_id}", error=exc.message)
    return _redirect(f"/containers/{container_id}", message="Container deactivated.")


@app.post("/containers/{container_id}/iso/register")
def register_iso(container_id: str, server_path: str = Form(...)):
    try:
        _api_json("POST", f"/v1/containers/{container_id}/iso/register", json={"server_path": server_path})
    except ApiError as exc:
        return _redirect(f"/containers/{container_id}", error=exc.message)
    return _redirect(f"/containers/{container_id}", message="ISO registered.")


@app.post("/containers/{container_id}/iso/create")
def create_iso(container_id: str, volume_label: str = Form(""), overwrite: bool = Form(False)):
    body: dict[str, Any] = {"overwrite": overwrite}
    if volume_label:
        body["volume_label"] = volume_label
    try:
        _api_json("POST", f"/v1/containers/{container_id}/iso/create", json=body)
    except ApiError as exc:
        return _redirect(f"/containers/{container_id}", error=exc.message)
    return _redirect(f"/containers/{container_id}", message="ISO created.")


@app.post("/containers/{container_id}/burn/confirm")
def confirm_burn(container_id: str):
    try:
        payload = _api_json("POST", f"/v1/containers/{container_id}/burn/confirm")
    except ApiError as exc:
        return _redirect(f"/containers/{container_id}", error=exc.message)
    released = len(payload.get("released_collection_ids", []))
    return _redirect(f"/containers/{container_id}", message=f"Burn confirmed. Released collections: {released}.")


@app.post("/containers/{container_id}/download-sessions")
def create_download_session(container_id: str):
    try:
        payload = _api_json("POST", f"/v1/containers/{container_id}/download-sessions")
    except ApiError as exc:
        return _redirect(f"/containers/{container_id}", error=exc.message)
    return _redirect(
        f"/containers/{container_id}?{urlencode({'download_session': payload['session_id']})}",
        message="Download session created.",
    )


@app.api_route("/containers/{container_id}/content/{relative_path:path}", methods=["GET", "HEAD"])
def download_container_content(request: Request, container_id: str, relative_path: str):
    return _proxy_stream(f"/v1/containers/{container_id}/content/{quote_path(relative_path)}", request)


@app.api_route("/containers/{container_id}/iso/content", methods=["GET", "HEAD"])
def download_registered_iso(request: Request, container_id: str):
    return _proxy_stream(f"/v1/containers/{container_id}/iso/content", request)


@app.api_route("/downloads/{session_id}/content", methods=["GET", "HEAD"])
def download_session_content(request: Request, session_id: str):
    return _proxy_stream(f"/v1/containers/downloads/{session_id}/content", request)


@app.get("/progress/uploads/{upload_id}/stream")
def upload_progress(upload_id: str):
    return _proxy_stream(f"/v1/progress/uploads/{upload_id}/stream")


@app.get("/progress/collections/{collection_id}/stream")
def collection_progress(collection_id: str):
    return _proxy_stream(f"/v1/progress/collections/{collection_id}/stream")


@app.get("/progress/activation-sessions/{session_id}/stream")
def activation_progress(session_id: str):
    return _proxy_stream(f"/v1/progress/activation-sessions/{session_id}/stream")


@app.get("/progress/downloads/{session_id}/stream")
def download_progress(session_id: str):
    return _proxy_stream(f"/v1/progress/downloads/{session_id}/stream")
