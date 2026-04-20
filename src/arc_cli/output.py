from __future__ import annotations

import json
from typing import Any

import typer


def emit(payload: Any, *, json_mode: bool) -> None:
    if json_mode:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    if isinstance(payload, dict):
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    typer.echo(str(payload))
