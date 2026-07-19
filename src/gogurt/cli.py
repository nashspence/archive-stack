from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import typer

from cli_support.output import json_text
from gogurt.core import (
    DEFAULT_GOGURT_CONFIG_FILENAME,
    DEFAULT_GOGURT_MARKER_NAME,
    load_gogurt_actions,
    plan_gogurt_marker,
    render_gogurt_triggers,
    write_gogurt_marker,
)
from riverhog_core.config_yaml import ConfigError

app = typer.Typer(help="Gogurt route and trigger utility.")


def emit(payload: object, *, json_mode: bool) -> None:
    if json_mode:
        typer.echo(json_text(payload))
        return
    typer.echo(str(payload))


@app.command("list")
def list_cmd(
    config: Annotated[
        Path,
        typer.Option("--config", help="Gogurt route YAML file."),
    ] = Path(DEFAULT_GOGURT_CONFIG_FILENAME),
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """List configured Gogurt routes."""

    actions = load_gogurt_actions(config)
    payload = [
        {"route": action.route, "script": action.script, "args": list(action.args)}
        for action in actions
    ]
    if json_mode:
        emit(payload, json_mode=True)
        return
    for action in actions:
        args = " ".join(action.args)
        suffix = f" {args}" if args else ""
        typer.echo(f"{action.route}: {action.script}{suffix}")


@app.command("render")
def render_cmd(
    config: Annotated[
        Path,
        typer.Option("--config", help="Gogurt route YAML file."),
    ] = Path(DEFAULT_GOGURT_CONFIG_FILENAME),
    dest_dir: Annotated[
        Path,
        typer.Option("--dest-dir", help="Directory where trigger scripts are written."),
    ] = Path("gogurt-triggers"),
    scripts_dir: Annotated[
        Path,
        typer.Option("--scripts-dir", help="Directory containing action scripts."),
    ] = Path("scripts"),
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """Render executable trigger scripts from Gogurt routes."""

    written = render_gogurt_triggers(config, dest_dir, scripts_dir)
    payload = [str(path) for path in written]
    emit(payload if json_mode else "\n".join(payload), json_mode=json_mode)


@app.command("write")
def write_cmd(
    route: Annotated[str, typer.Argument(help="Configured Gogurt route key.")],
    mount_point: Annotated[Path, typer.Argument(help="Mounted volume root.")],
    config: Annotated[
        Path,
        typer.Option("--config", help="Gogurt route YAML file."),
    ] = Path(DEFAULT_GOGURT_CONFIG_FILENAME),
    marker_name: Annotated[
        str,
        typer.Option("--marker-name", help="Marker file name written at the mount root."),
    ] = DEFAULT_GOGURT_MARKER_NAME,
    force: Annotated[
        bool,
        typer.Option("--force", help="Replace a different marker value."),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Preview the marker write without changing the volume."),
    ] = False,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Write a Gogurt marker file to a mounted volume."""

    if dry_run:
        plan = plan_gogurt_marker(
            config,
            route,
            mount_point,
            marker_name=marker_name,
            force=force,
        )
        if json_mode:
            emit(plan, json_mode=True)
            return
        typer.echo("gogurt write dry-run")
        typer.echo(f"status: {plan.get('status', 'unknown')}")
        typer.echo(f"route: {plan.get('route', 'unknown')}")
        typer.echo(f"marker: {plan.get('marker', 'unknown')}")
        typer.echo(f"content: {str(plan.get('content', '')).rstrip()}")
        return
    marker = write_gogurt_marker(config, route, mount_point, marker_name=marker_name, force=force)
    emit({"marker": str(marker)} if json_mode else str(marker), json_mode=json_mode)


def _json_requested(argv: list[str]) -> bool:
    return "--json" in argv


def _emit_cli_error(exc: BaseException, *, json_mode: bool) -> None:
    message = str(exc) or type(exc).__name__
    if json_mode:
        typer.echo(
            json_text({"error": {"code": "config_error", "message": message}})
        )
        return
    typer.echo(f"gogurt: {message}", err=True)


def main() -> None:
    try:
        app()
    except (ConfigError, FileExistsError, FileNotFoundError, NotADirectoryError) as exc:
        _emit_cli_error(exc, json_mode=_json_requested(sys.argv[1:]))
        raise typer.Exit(1) from None


if __name__ == "__main__":
    main()
