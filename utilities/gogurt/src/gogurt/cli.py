from __future__ import annotations

import importlib.metadata
import json
import sys
from pathlib import Path
from typing import Annotated

import typer
from config_validation import ConfigError
from riverhog_cli_support.output import json_text

from gogurt.core import (
    DEFAULT_GOGURT_CONFIG_FILENAME,
    DEFAULT_GOGURT_MARKER_NAME,
    GOGURT_EMOJI,
    execute_gogurt_action,
    load_gogurt_actions,
    plan_gogurt_action,
    plan_gogurt_marker,
    write_gogurt_marker,
)
from gogurt.mounts import discover_mount_points, iter_new_mounts

app = typer.Typer(help="Portable mounted-volume marker actions.")


def _version_callback(value: bool) -> None:
    if not value:
        return
    typer.echo(importlib.metadata.version("gogurt"))
    raise typer.Exit()


@app.callback()
def _root(
    _version: Annotated[
        bool,
        typer.Option(
            "--version",
            help="Show the installed Gogurt version",
            callback=_version_callback,
            is_eager=True,
        ),
    ] = False,
) -> None:
    """Portable mounted-volume marker actions."""


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
    payload = [{"route": action.route, "command": list(action.command)} for action in actions]
    if json_mode:
        emit(payload, json_mode=True)
        return
    for action in actions:
        typer.echo(f"{action.route}: {json.dumps(action.command)}")


def _run_mount(
    mount_point: Path,
    *,
    config: Path,
    actions_dir: Path | None,
    marker_name: str,
    autorun: bool,
    dry_run: bool,
) -> int:
    plan = plan_gogurt_action(
        config,
        mount_point,
        actions_dir=actions_dir,
        marker_name=marker_name,
    )
    if plan["status"] == "unmarked":
        return 0

    route = str(plan["route"])
    raw_command = plan["command"]
    if not isinstance(raw_command, list):
        raise RuntimeError("Gogurt returned an invalid action plan")
    command = [str(token) for token in raw_command]
    typer.echo(
        f"{GOGURT_EMOJI} gogurt action available: route={route} mount={mount_point}",
        err=True,
    )
    if dry_run:
        typer.echo(f"{GOGURT_EMOJI} gogurt would run: {json.dumps(command)}", err=True)
        return 0
    if not autorun:
        if not sys.stdin.isatty():
            typer.echo(
                f"{GOGURT_EMOJI} gogurt action awaiting confirmation; "
                "use --autorun for unattended execution.",
                err=True,
            )
            return 0
        if not typer.confirm(f"{GOGURT_EMOJI} gogurt run this action?", default=False):
            typer.echo(f"{GOGURT_EMOJI} gogurt action declined", err=True)
            return 0

    typer.echo(f"{GOGURT_EMOJI} gogurt launching: route={route} mount={mount_point}", err=True)
    return execute_gogurt_action(plan).returncode


@app.command("run")
def run_cmd(
    mount_point: Annotated[Path, typer.Argument(help="Mounted volume root.")],
    config: Annotated[
        Path,
        typer.Option("--config", help="Gogurt route YAML file."),
    ] = Path(DEFAULT_GOGURT_CONFIG_FILENAME),
    actions_dir: Annotated[
        Path | None,
        typer.Option("--actions-dir", help="Directory containing configured action commands."),
    ] = None,
    marker_name: Annotated[
        str,
        typer.Option("--marker-name", help="Marker file name at the mount root."),
    ] = DEFAULT_GOGURT_MARKER_NAME,
    autorun: Annotated[
        bool,
        typer.Option("--autorun", help="Explicitly allow unattended action execution."),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Resolve and report the action without executing it."),
    ] = False,
) -> None:
    """Run the configured action for one marked mounted volume."""

    return_code = _run_mount(
        mount_point,
        config=config,
        actions_dir=actions_dir,
        marker_name=marker_name,
        autorun=autorun,
        dry_run=dry_run,
    )
    if return_code:
        raise typer.Exit(return_code)


@app.command("mounts")
def mounts_cmd(
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """List mounted roots observed through the native platform adapter."""

    mount_points = [str(path) for path in discover_mount_points()]
    if json_mode:
        emit(mount_points, json_mode=True)
        return
    for mount_point in mount_points:
        typer.echo(mount_point)


@app.command("watch")
def watch_cmd(
    config: Annotated[
        Path,
        typer.Option("--config", help="Gogurt route YAML file."),
    ] = Path(DEFAULT_GOGURT_CONFIG_FILENAME),
    actions_dir: Annotated[
        Path | None,
        typer.Option("--actions-dir", help="Directory containing configured action commands."),
    ] = None,
    marker_name: Annotated[
        str,
        typer.Option("--marker-name", help="Marker file name at each mount root."),
    ] = DEFAULT_GOGURT_MARKER_NAME,
    interval_seconds: Annotated[
        float,
        typer.Option("--interval", min=0.1, help="Mount polling interval in seconds."),
    ] = 2.0,
    include_existing: Annotated[
        bool,
        typer.Option("--include-existing", help="Inspect existing mounts before watching."),
    ] = False,
    autorun: Annotated[
        bool,
        typer.Option("--autorun", help="Explicitly allow unattended action execution."),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Resolve and report actions without executing them."),
    ] = False,
) -> None:
    """Watch for newly mounted volumes and apply their configured routes."""

    typer.echo(f"{GOGURT_EMOJI} gogurt watcher started", err=True)
    try:
        for mount_point in iter_new_mounts(
            interval_seconds=interval_seconds,
            include_existing=include_existing,
        ):
            try:
                return_code = _run_mount(
                    mount_point,
                    config=config,
                    actions_dir=actions_dir,
                    marker_name=marker_name,
                    autorun=autorun,
                    dry_run=dry_run,
                )
            except (
                ConfigError,
                FileNotFoundError,
                NotADirectoryError,
                PermissionError,
                UnicodeError,
            ) as exc:
                typer.echo(f"{GOGURT_EMOJI} gogurt skipped {mount_point}: {exc}", err=True)
                continue
            if return_code:
                typer.echo(
                    f"{GOGURT_EMOJI} gogurt action failed: mount={mount_point} exit={return_code}",
                    err=True,
                )
    except KeyboardInterrupt:
        typer.echo(f"{GOGURT_EMOJI} gogurt watcher stopped", err=True)


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
        typer.echo(json_text({"error": {"code": "config_error", "message": message}}))
        return
    typer.echo(f"gogurt: {message}", err=True)


def main() -> None:
    try:
        app()
    except (
        ConfigError,
        FileExistsError,
        FileNotFoundError,
        NotADirectoryError,
        PermissionError,
    ) as exc:
        _emit_cli_error(exc, json_mode=_json_requested(sys.argv[1:]))
        raise typer.Exit(1) from None


if __name__ == "__main__":
    main()
