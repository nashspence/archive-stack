from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from munchy.profiles import EncodeProfile, ProfileError, load_encode_profile
from munchy.runner_client import (
    MunchyRunnerClient,
    format_job_status_line,
    format_job_summary_line,
    runner_url_setting,
)

app = typer.Typer(help="munchy media ingest CLI")
profile_app = typer.Typer(help="encode profile operations")
job_app = typer.Typer(help="runner job operations")
app.add_typer(profile_app, name="profile")
app.add_typer(job_app, name="job")


def _load_profile_or_exit(path: Path) -> EncodeProfile:
    try:
        return load_encode_profile(path)
    except (OSError, ProfileError, ValidationError) as exc:
        raise typer.BadParameter(str(exc), param_hint=str(path)) from exc


@profile_app.command("validate")
def validate_profile(
    path: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
) -> None:
    """Validate an encode profile file."""

    profile = _load_profile_or_exit(path)
    typer.echo(
        f"{path}: ok target={profile.target} "
        f"container={profile.archive.container} quality={profile.archive.video.quality}"
    )


@profile_app.command("dump-json")
def dump_profile_json(
    path: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
) -> None:
    """Validate and print a normalized encode profile as JSON."""

    profile = _load_profile_or_exit(path)
    typer.echo(json.dumps(profile.runner_payload(), indent=2, sort_keys=True))


@job_app.command("list")
def list_jobs(
    runner_url: Annotated[
        str | None,
        typer.Option("--runner-url", help="Munchy runner URL; defaults to MUNCHY_RUNNER_URL"),
    ] = None,
    all_jobs: Annotated[bool, typer.Option("--all", help="Include terminal jobs")] = False,
    limit: Annotated[int, typer.Option("--limit", min=1, max=500)] = 50,
    json_mode: Annotated[bool, typer.Option("--json", help="Emit JSON")] = False,
) -> None:
    """List runner jobs."""

    client = MunchyRunnerClient(runner_url_setting(runner_url))
    jobs = client.list_jobs(include_terminal=all_jobs, limit=limit)
    if json_mode:
        typer.echo(json.dumps({"jobs": jobs}, indent=2, sort_keys=True))
        return
    if not jobs:
        typer.echo("no runner jobs")
        return
    for job in jobs:
        typer.echo(format_job_summary_line(job))


@job_app.command("watch")
def watch_job(
    job_id: Annotated[str, typer.Argument(help="Runner job id")],
    runner_url: Annotated[
        str | None,
        typer.Option("--runner-url", help="Munchy runner URL; defaults to MUNCHY_RUNNER_URL"),
    ] = None,
    interval: Annotated[float, typer.Option("--interval", min=0.5)] = 10.0,
) -> None:
    """Monitor a runner job until it reaches a terminal state."""

    final = MunchyRunnerClient(runner_url_setting(runner_url)).wait_for_job(
        job_id,
        interval=interval,
    )
    if final.get("state") != "succeeded":
        raise typer.Exit(1)


@job_app.command("cancel")
def cancel_job(
    job_id: Annotated[str, typer.Argument(help="Runner job id")],
    runner_url: Annotated[
        str | None,
        typer.Option("--runner-url", help="Munchy runner URL; defaults to MUNCHY_RUNNER_URL"),
    ] = None,
    cleanup: Annotated[
        bool,
        typer.Option("--cleanup", help="Also clean runner-side artifacts"),
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Confirm cancellation")] = False,
    wait: Annotated[bool, typer.Option("--wait", help="Wait for cancellation to settle")] = False,
    interval: Annotated[float, typer.Option("--interval", min=0.5)] = 10.0,
) -> None:
    """Cancel a runner job."""

    if not yes:
        raise typer.BadParameter("add --yes to confirm job cancellation", param_hint="--yes")
    client = MunchyRunnerClient(runner_url_setting(runner_url))
    job = client.cancel_job(job_id, cleanup=cleanup)
    typer.echo(format_job_status_line(job), err=True)
    if wait or cleanup:
        final = client.wait_for_job(job_id, interval=interval)
        if cleanup and final.get("state") == "cancelled":
            final = client.cancel_job(job_id, cleanup=True)
            typer.echo(format_job_status_line(final), err=True)
        if final.get("state") != "cancelled":
            raise typer.Exit(1)


def main() -> None:
    app()
