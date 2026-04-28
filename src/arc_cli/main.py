from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Annotated

import typer

from arc_cli.client import ApiClient
from arc_cli.output import (
    emit,
    format_collection_files,
    format_collection_upload,
    format_copies,
    format_copy,
    format_fetch,
    format_files,
    format_images,
    format_pin,
    format_plan,
)
from arc_core.domain.errors import NotFound

app = typer.Typer(help="arc archival control CLI")
iso_app = typer.Typer(help="ISO operations")
copy_app = typer.Typer(help="copy registration")
app.add_typer(iso_app, name="iso")
app.add_typer(copy_app, name="copy")

PLAN_QUERY_HELP = (
    "Substring match over candidate id, collection ids, and represented projected file paths"
)
IMAGE_QUERY_HELP = "Substring match over id, filename, and collection ids"


def client() -> ApiClient:
    return ApiClient()


def _local_collection_manifest(root: Path) -> list[dict[str, object]]:
    files: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        content = path.read_bytes()
        files.append(
            {
                "path": path.relative_to(root).as_posix(),
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    if not files:
        raise typer.BadParameter("collection source must contain at least one file")
    return files


def _finalized_collection_upload_payload(
    collection_id: str,
    manifest: list[dict[str, object]],
    collection: dict[str, object],
) -> dict[str, object]:
    bytes_total = sum(int(item["bytes"]) for item in manifest)
    files = [
        {
            "path": str(item["path"]),
            "bytes": int(item["bytes"]),
            "sha256": str(item["sha256"]),
            "upload_state": "uploaded",
            "uploaded_bytes": int(item["bytes"]),
            "upload_state_expires_at": None,
        }
        for item in manifest
    ]
    return {
        "collection_id": collection_id,
        "ingest_source": collection.get("ingest_source"),
        "state": "finalized",
        "files_total": len(files),
        "files_pending": 0,
        "files_partial": 0,
        "files_uploaded": len(files),
        "bytes_total": bytes_total,
        "uploaded_bytes": bytes_total,
        "missing_bytes": 0,
        "upload_state_expires_at": None,
        "files": files,
        "collection": collection,
    }


@app.command("upload")
def upload_cmd(
    collection_id: Annotated[str, typer.Argument(help="Canonical collection id")],
    root: Annotated[Path, typer.Argument(help="Local collection root directory")],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    resolved_root = root.expanduser().resolve()
    if not resolved_root.is_dir():
        raise typer.BadParameter("collection source must be a directory")

    api = client()
    manifest = _local_collection_manifest(resolved_root)
    payload = api.create_or_resume_collection_upload(
        collection_id,
        manifest,
        ingest_source=str(resolved_root),
    )
    files = {item["path"]: (resolved_root / str(item["path"])).read_bytes() for item in manifest}

    for file_payload in payload["files"]:
        if file_payload["upload_state"] == "uploaded":
            continue
        session = api.create_or_resume_collection_file_upload(
            collection_id,
            str(file_payload["path"]),
        )
        content = files[str(file_payload["path"])]
        offset = int(session["offset"])
        if offset < len(content):
            api.append_upload_chunk(
                str(session["upload_url"]),
                offset=offset,
                checksum_algorithm=str(session["checksum_algorithm"]),
                content=content[offset:],
            )

    try:
        collection = api.get_collection(collection_id)
        final_payload = _finalized_collection_upload_payload(collection_id, manifest, collection)
    except NotFound:
        final_payload = api.get_collection_upload(collection_id)
    emit(
        final_payload if json_mode else format_collection_upload(final_payload),
        json_mode=json_mode,
    )


@app.command("find")
def find_cmd(
    query: Annotated[str, typer.Argument(help="Search query")],
    limit: Annotated[int, typer.Option("--limit", min=1, max=100)] = 25,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    emit(client().search(query, limit), json_mode=json_mode)


@app.command("show")
def show_cmd(
    collection: Annotated[str, typer.Argument(help="Collection id")],
    files: Annotated[bool, typer.Option("--files", help="List files in the collection")] = False,
    page: Annotated[int, typer.Option("--page", min=1)] = 1,
    per_page: Annotated[int, typer.Option("--per-page", min=1, max=100)] = 25,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    if files:
        payload = client().list_collection_files(collection, page=page, per_page=per_page)
        emit(payload if json_mode else format_collection_files(payload), json_mode=json_mode)
    else:
        emit(client().get_collection(collection), json_mode=json_mode)


@app.command("status")
def status_cmd(
    target: Annotated[str, typer.Argument(help="Target selector")],
    page: Annotated[int, typer.Option("--page", min=1)] = 1,
    per_page: Annotated[int, typer.Option("--per-page", min=1, max=100)] = 25,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    payload = client().query_files(target, page=page, per_page=per_page)
    emit(payload if json_mode else format_files(payload), json_mode=json_mode)


@app.command("get")
def get_cmd(
    target: Annotated[str, typer.Argument(help="File target selector")],
    output: Annotated[Path | None, typer.Option("-o", "--output", help="Output path")] = None,
) -> None:
    import sys

    content = client().get_file_content(target, output)
    if output is None:
        sys.stdout.buffer.write(content)
    else:
        typer.echo(f"wrote {len(content)} bytes to {output}")


@app.command("plan")
def plan_cmd(
    page: Annotated[int, typer.Option("--page", min=1)] = 1,
    per_page: Annotated[int, typer.Option("--per-page", min=1, max=100)] = 25,
    sort: Annotated[str, typer.Option("--sort", help="Sort field")] = "fill",
    order: Annotated[str, typer.Option("--order", help="Sort order")] = "desc",
    query: Annotated[
        str | None,
        typer.Option(
            "--query",
            help=PLAN_QUERY_HELP,
        ),
    ] = None,
    collection: Annotated[
        str | None, typer.Option("--collection", help="Filter by exact contained collection id")
    ] = None,
    iso_ready: Annotated[
        bool | None,
        typer.Option(
            "--iso-ready/--not-ready", help="Filter by whether the candidate is ready to finalize"
        ),
    ] = None,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    payload = client().get_plan(
        page=page,
        per_page=per_page,
        sort=sort,
        order=order,
        query=query,
        collection=collection,
        iso_ready=iso_ready,
    )
    emit(payload if json_mode else format_plan(payload), json_mode=json_mode)


@app.command("images")
def images_cmd(
    page: Annotated[int, typer.Option("--page", min=1)] = 1,
    per_page: Annotated[int, typer.Option("--per-page", min=1, max=100)] = 25,
    sort: Annotated[str, typer.Option("--sort", help="Sort field")] = "finalized_at",
    order: Annotated[str, typer.Option("--order", help="Sort order")] = "desc",
    query: Annotated[
        str | None,
        typer.Option("--query", help=IMAGE_QUERY_HELP),
    ] = None,
    collection: Annotated[
        str | None, typer.Option("--collection", help="Filter by exact contained collection id")
    ] = None,
    has_copies: Annotated[
        bool | None,
        typer.Option(
            "--has-copies/--no-copies", help="Filter by whether the image has registered copies"
        ),
    ] = None,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    payload = client().list_images(
        page=page,
        per_page=per_page,
        sort=sort,
        order=order,
        query=query,
        collection=collection,
        has_copies=has_copies,
    )
    emit(payload if json_mode else format_images(payload), json_mode=json_mode)


@iso_app.command("get")
def iso_get_cmd(
    image_id: Annotated[str, typer.Argument(help="Image id")],
    output: Annotated[Path | None, typer.Option("-o", "--output", help="Output path")] = None,
) -> None:
    content = client().download_iso(image_id, output)
    if output is None:
        raise typer.Exit(code=0)
    typer.echo(f"wrote {len(content)} bytes to {output}")


@copy_app.command("add")
def copy_add_cmd(
    image_id: Annotated[str, typer.Argument(help="Finalized image id")],
    at: Annotated[str, typer.Option("--at", help="Physical location label")],
    copy_id: Annotated[
        str | None,
        typer.Option("--copy-id", help="Generated copy id to claim explicitly"),
    ] = None,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    payload = client().register_copy(image_id, at, copy_id=copy_id)
    emit(payload if json_mode else format_copy(payload["copy"]), json_mode=json_mode)


@copy_app.command("list")
def copy_list_cmd(
    image_id: Annotated[str, typer.Argument(help="Finalized image id")],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    payload = client().list_copies(image_id)
    emit(payload if json_mode else format_copies(payload), json_mode=json_mode)


@copy_app.command("move")
def copy_move_cmd(
    image_id: Annotated[str, typer.Argument(help="Finalized image id")],
    copy_id: Annotated[str, typer.Argument(help="Generated copy id")],
    to: Annotated[str, typer.Option("--to", help="New physical location label")],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    payload = client().update_copy(image_id, copy_id, location=to)
    emit(payload if json_mode else format_copy(payload["copy"]), json_mode=json_mode)


@copy_app.command("mark")
def copy_mark_cmd(
    image_id: Annotated[str, typer.Argument(help="Finalized image id")],
    copy_id: Annotated[str, typer.Argument(help="Generated copy id")],
    state: Annotated[str, typer.Option("--state", help="Copy lifecycle state")],
    verification_state: Annotated[
        str | None,
        typer.Option("--verification-state", help="Verification state"),
    ] = None,
    at: Annotated[
        str | None,
        typer.Option("--at", help="Updated physical location label"),
    ] = None,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    payload = client().update_copy(
        image_id,
        copy_id,
        location=at,
        state=state,
        verification_state=verification_state,
    )
    emit(payload if json_mode else format_copy(payload["copy"]), json_mode=json_mode)


@app.command("pin")
def pin_cmd(
    target: Annotated[str, typer.Argument(help="Target selector")],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    payload = client().pin(target)
    emit(payload if json_mode else format_pin(payload), json_mode=json_mode)


@app.command("release")
def release_cmd(
    target: Annotated[str, typer.Argument(help="Target selector")],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    emit(client().release(target), json_mode=json_mode)


@app.command("pins")
def pins_cmd(
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    emit(client().list_pins(), json_mode=json_mode)


@app.command("fetch")
def fetch_cmd(
    fetch_id: Annotated[str, typer.Argument(help="Fetch id")],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    summary = client().get_fetch(fetch_id)
    if json_mode:
        emit(summary, json_mode=True)
        return
    manifest = client().get_fetch_manifest(fetch_id)
    emit(format_fetch(summary, manifest), json_mode=False)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
