from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from munchy.profiles import EncodeProfile, ProfileError, load_encode_profile

app = typer.Typer(help="munchy media ingest CLI")
profile_app = typer.Typer(help="encode profile operations")
app.add_typer(profile_app, name="profile")


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


def main() -> None:
    app()
