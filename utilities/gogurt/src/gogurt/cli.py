from __future__ import annotations

import importlib.metadata
import json
import sys
from pathlib import Path
from typing import Annotated

import typer
from config_validation import ConfigError
from gogurt_core.core import (
    DEFAULT_GOGURT_CONFIG_FILENAME,
    DEFAULT_GOGURT_MARKER_NAME,
    GOGURT_EMOJI,
    execute_gogurt_action,
    load_gogurt_actions,
    plan_gogurt_action,
    plan_gogurt_marker,
    write_gogurt_marker,
)
from gogurt_core.mounts import (
    MAX_GOGURT_INTERVAL_SECONDS,
    MIN_GOGURT_INTERVAL_SECONDS,
    iter_new_mounts,
)
from gogurt_listener_runtime.listener import (
    ListenerConfig,
    ListenerError,
    install_listener,
    listener_status,
    restart_listener,
    run_listener,
    start_listener,
    stop_listener,
    uninstall_listener,
)
from gogurt_listener_runtime.platform import ListenerPlatformError
from riverhog_cli_support.output import json_text

from gogurt.providers import (
    ResolvedListenerHostProvider,
    ResolvedMountProvider,
    list_listener_host_providers,
    list_mount_providers,
    resolve_listener_host_provider,
    resolve_mount_provider,
)

app = typer.Typer(help="Portable mounted-volume marker actions.")
listener_app = typer.Typer(help="Install and manage the per-user Gogurt listener.")
provider_app = typer.Typer(help="Inspect explicitly composable host providers.")
mount_provider_app = typer.Typer(help="Inspect mount-discovery providers.")
listener_host_provider_app = typer.Typer(help="Inspect listener-host providers.")
app.add_typer(listener_app, name="listener")
app.add_typer(provider_app, name="provider")
provider_app.add_typer(mount_provider_app, name="mount")
provider_app.add_typer(listener_host_provider_app, name="listener-host")

MountProviderOption = Annotated[
    str | None,
    typer.Option(
        "--mount-provider",
        envvar="GOGURT_MOUNT_PROVIDER",
        help="Exact installed mount-provider name.",
    ),
]
ListenerHostProviderOption = Annotated[
    str | None,
    typer.Option(
        "--listener-host-provider",
        envvar="GOGURT_LISTENER_HOST_PROVIDER",
        help="Exact installed listener-host provider name.",
    ),
]


def _version_callback(value: bool) -> None:
    if not value:
        return
    typer.echo(_product_version())
    raise typer.Exit()


def _product_version() -> str:
    return importlib.metadata.version("gogurt")


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


def _emit_listener(payload: dict[str, object], *, json_mode: bool, operation: str) -> None:
    if json_mode:
        emit(payload, json_mode=True)
        return
    typer.echo(f"gogurt listener {operation}")
    typer.echo(f"health: {payload.get('health', 'unknown')}")
    typer.echo(f"installed: {str(bool(payload.get('installed'))).lower()}")
    typer.echo(f"enabled: {str(bool(payload.get('enabled'))).lower()}")
    typer.echo(f"running: {str(bool(payload.get('running'))).lower()}")
    for field in ("mount_provider", "listener_host_provider"):
        reference = payload.get(field)
        if isinstance(reference, dict):
            typer.echo(f"{field.replace('_', ' ')}: {reference.get('name', 'unknown')}")
    diagnostic = payload.get("diagnostic")
    if diagnostic:
        typer.echo(f"diagnostic: {diagnostic}")


def _resolve_mount(name: str | None) -> ResolvedMountProvider:
    if name is None:
        raise ConfigError("an explicit --mount-provider is required")
    try:
        return resolve_mount_provider(name)
    except (TypeError, ValueError) as exc:
        raise ConfigError(str(exc)) from exc


def _resolve_listener_host(name: str | None) -> ResolvedListenerHostProvider:
    if name is None:
        raise ConfigError("an explicit --listener-host-provider is required")
    try:
        return resolve_listener_host_provider(name)
    except (TypeError, ValueError) as exc:
        raise ConfigError(str(exc)) from exc


def _installed_listener_composition(
    name: str | None,
) -> tuple[ResolvedListenerHostProvider, ResolvedMountProvider | None]:
    host = _resolve_listener_host(name)
    paths = host.paths()
    if not paths.config_file.is_file():
        return host, None
    config = ListenerConfig.read(paths.config_file, product_version=_product_version())
    try:
        exact_host = resolve_listener_host_provider(
            config.listener_host_provider.name,
            expected=config.listener_host_provider,
        )
        mount = resolve_mount_provider(
            config.mount_provider.name,
            expected=config.mount_provider,
        )
    except (TypeError, ValueError) as exc:
        raise ConfigError(str(exc)) from exc
    if exact_host.reference != host.reference:
        raise ConfigError("selected listener-host provider differs from installed listener")
    return host, mount


def _with_provider_status(
    payload: dict[str, object],
    *,
    host: ResolvedListenerHostProvider,
    mount: ResolvedMountProvider | None,
) -> dict[str, object]:
    return {
        **payload,
        "mount_provider": mount.reference.as_dict() if mount is not None else None,
        "listener_host_provider": host.reference.as_dict(),
    }


def _provider_list(
    providers: list[dict[str, str | None]],
    *,
    kind: str,
    ids: bool,
    json_mode: bool,
) -> None:
    if ids and json_mode:
        raise ConfigError("--ids and --json cannot be used together")
    payload = {"format": "gogurt-provider-list/v1", "kind": kind, "providers": providers}
    if ids:
        emit("\n".join(str(item["name"]) for item in providers), json_mode=False)
        return
    human = [f"Gogurt {kind} providers: {len(providers)}"]
    human.extend(
        f"- {item['name']}  distribution={item['distribution'] or 'unknown'}  "
        f"version={item['version'] or 'unknown'}"
        for item in providers
    )
    emit(payload if json_mode else "\n".join(human), json_mode=json_mode)


@mount_provider_app.command("list")
def mount_provider_list_cmd(
    ids: Annotated[bool, typer.Option("--ids", help="Emit one provider name per line.")] = False,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """List installed mount-provider metadata without loading provider code."""

    _provider_list(
        [item.as_dict() for item in list_mount_providers()],
        kind="mount",
        ids=ids,
        json_mode=json_mode,
    )


@listener_host_provider_app.command("list")
def listener_host_provider_list_cmd(
    ids: Annotated[bool, typer.Option("--ids", help="Emit one provider name per line.")] = False,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """List installed listener-host metadata without loading provider code."""

    _provider_list(
        [item.as_dict() for item in list_listener_host_providers()],
        kind="listener-host",
        ids=ids,
        json_mode=json_mode,
    )


def _provider_show(payload: dict[str, object], *, json_mode: bool) -> None:
    reference = payload["reference"]
    assert isinstance(reference, dict)
    human = "\n".join(
        (
            f"Gogurt {payload['kind']} provider {payload['name']}",
            f"provider identity: {reference['provider_id']}",
            f"distribution: {payload['distribution'] or 'unknown'}",
            f"version: {payload['version'] or 'unknown'}",
        )
    )
    emit(payload if json_mode else human, json_mode=json_mode)


@mount_provider_app.command("show")
def mount_provider_show_cmd(
    name: Annotated[str, typer.Argument(help="Exact installed provider name.")],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Load and report one exact mount provider."""

    _provider_show(_resolve_mount(name).as_dict(), json_mode=json_mode)


@listener_host_provider_app.command("show")
def listener_host_provider_show_cmd(
    name: Annotated[str, typer.Argument(help="Exact installed provider name.")],
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Load and report one exact listener-host provider."""

    _provider_show(_resolve_listener_host(name).as_dict(), json_mode=json_mode)


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
    mount_provider: MountProviderOption = None,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """List mounted roots observed through the native platform adapter."""

    mount_points = [str(path) for path in _resolve_mount(mount_provider).discover()]
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
        typer.Option(
            "--interval",
            min=MIN_GOGURT_INTERVAL_SECONDS,
            max=MAX_GOGURT_INTERVAL_SECONDS,
            help="Mount polling interval in seconds.",
        ),
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
    mount_provider: MountProviderOption = None,
) -> None:
    """Watch for newly mounted volumes and apply their configured routes."""

    typer.echo(f"{GOGURT_EMOJI} gogurt watcher started", err=True)
    provider = _resolve_mount(mount_provider)
    try:
        for mount_point in iter_new_mounts(
            discover=provider.discover,
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


@listener_app.command("install")
def listener_install_cmd(
    config: Annotated[
        Path,
        typer.Option("--config", help="Absolute Gogurt route YAML file."),
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
        typer.Option(
            "--interval",
            min=MIN_GOGURT_INTERVAL_SECONDS,
            max=MAX_GOGURT_INTERVAL_SECONDS,
            help="Mount polling interval in seconds.",
        ),
    ] = 2.0,
    autorun: Annotated[
        bool,
        typer.Option("--autorun", help="Explicitly enable unattended action execution."),
    ] = False,
    mount_provider: MountProviderOption = None,
    listener_host_provider: ListenerHostProviderOption = None,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Install and start the current-user listener."""

    if not autorun:
        raise ConfigError("gogurt listener install requires explicit --autorun")
    mount = _resolve_mount(mount_provider)
    host = _resolve_listener_host(listener_host_provider)
    payload = install_listener(
        config,
        actions_dir=actions_dir,
        marker_name=marker_name,
        interval_seconds=interval_seconds,
        executable=host.executable(),
        paths=host.paths(),
        adapter=host.adapter(),
        product_version=_product_version(),
        mount_provider=mount.reference,
        listener_host_provider=host.reference,
    )
    _emit_listener(
        _with_provider_status(payload, host=host, mount=mount),
        json_mode=json_mode,
        operation="installed",
    )


@listener_app.command("status")
def listener_status_cmd(
    listener_host_provider: ListenerHostProviderOption = None,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Report native registration, health, and durable dispatch state."""

    host, mount = _installed_listener_composition(listener_host_provider)
    _emit_listener(
        _with_provider_status(
            listener_status(
                paths=host.paths(),
                adapter=host.adapter(),
                product_version=_product_version(),
            ),
            host=host,
            mount=mount,
        ),
        json_mode=json_mode,
        operation="status",
    )


@listener_app.command("start")
def listener_start_cmd(
    listener_host_provider: ListenerHostProviderOption = None,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Start an installed current-user listener."""

    host, mount = _installed_listener_composition(listener_host_provider)
    _emit_listener(
        _with_provider_status(
            start_listener(
                paths=host.paths(),
                adapter=host.adapter(),
                product_version=_product_version(),
            ),
            host=host,
            mount=mount,
        ),
        json_mode=json_mode,
        operation="started",
    )


@listener_app.command("stop")
def listener_stop_cmd(
    listener_host_provider: ListenerHostProviderOption = None,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Stop the current-user listener without removing login persistence."""

    host, mount = _installed_listener_composition(listener_host_provider)
    _emit_listener(
        _with_provider_status(
            stop_listener(
                paths=host.paths(),
                adapter=host.adapter(),
                product_version=_product_version(),
            ),
            host=host,
            mount=mount,
        ),
        json_mode=json_mode,
        operation="stopped",
    )


@listener_app.command("restart")
def listener_restart_cmd(
    listener_host_provider: ListenerHostProviderOption = None,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Restart the installed current-user listener."""

    host, mount = _installed_listener_composition(listener_host_provider)
    _emit_listener(
        _with_provider_status(
            restart_listener(
                paths=host.paths(),
                adapter=host.adapter(),
                product_version=_product_version(),
            ),
            host=host,
            mount=mount,
        ),
        json_mode=json_mode,
        operation="restarted",
    )


@listener_app.command("uninstall")
def listener_uninstall_cmd(
    listener_host_provider: ListenerHostProviderOption = None,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Stop the listener and remove its registration, state, and bounded logs."""

    host, mount = _installed_listener_composition(listener_host_provider)
    _emit_listener(
        _with_provider_status(
            uninstall_listener(
                paths=host.paths(),
                adapter=host.adapter(),
                product_version=_product_version(),
            ),
            host=host,
            mount=mount,
        ),
        json_mode=json_mode,
        operation="uninstalled",
    )


@listener_app.command("_run", hidden=True)
def listener_run_cmd(
    runtime_config: Annotated[
        Path,
        typer.Option("--runtime-config", help="Installed listener runtime config."),
    ],
) -> None:
    """Run the registered listener process."""

    config = ListenerConfig.read(runtime_config, product_version=_product_version())
    try:
        mount = resolve_mount_provider(
            config.mount_provider.name,
            expected=config.mount_provider,
        )
    except (TypeError, ValueError) as exc:
        raise ConfigError(str(exc)) from exc
    run_listener(
        runtime_config,
        discover=mount.discover,
        product_version=_product_version(),
    )


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
        code = (
            "listener_error"
            if isinstance(exc, (ListenerError, ListenerPlatformError))
            else "config_error"
        )
        typer.echo(json_text({"error": {"code": code, "message": message}}))
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
        ListenerError,
        ListenerPlatformError,
    ) as exc:
        _emit_cli_error(exc, json_mode=_json_requested(sys.argv[1:]))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
