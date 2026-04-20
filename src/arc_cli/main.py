from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from arc_cli.client import ApiClient
from arc_cli.output import emit

app = typer.Typer(help="arc archival control CLI")
iso_app = typer.Typer(help="ISO operations")
copy_app = typer.Typer(help="copy registration")
app.add_typer(iso_app, name="iso")
app.add_typer(copy_app, name="copy")


def client() -> ApiClient:
    return ApiClient()


@app.command("close")
def close_cmd(
    path: Annotated[str, typer.Argument(help="Path to staged collection directory")],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    emit(client().close_collection(path), json_mode=json_mode)


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
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    emit(client().get_collection(collection), json_mode=json_mode)


@app.command("plan")
def plan_cmd(
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    emit(client().get_plan(), json_mode=json_mode)


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
    image_id: Annotated[str, typer.Argument(help="Image id")],
    copy_id: Annotated[str, typer.Argument(help="Physical copy id")],
    at: Annotated[str, typer.Option("--at", help="Physical location label")],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    emit(client().register_copy(image_id, copy_id, at), json_mode=json_mode)


@app.command("pin")
def pin_cmd(
    target: Annotated[str, typer.Argument(help="Target selector")],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    emit(client().pin(target), json_mode=json_mode)


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
    emit(client().get_fetch(fetch_id), json_mode=json_mode)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
