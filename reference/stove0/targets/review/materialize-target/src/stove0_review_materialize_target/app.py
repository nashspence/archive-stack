"""Authenticated review materialization target process."""

from __future__ import annotations

import argparse
import contextlib
import importlib.metadata
import os
from collections.abc import Sequence
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from stove0_review_target_support import (
    SamplerRegistration,
    create_target_app,
    load_sampler_registrations,
    parse_sampler_registrations,
)
from stove0_target_support import terminal_state_retention_seconds

from stove0_review_materialize_target.target import ReviewMaterializeTargetService

SERVICE = "stove0-review-materialize-target"
PREFIX = "STOVE0_REVIEW_MATERIALIZE_TARGET"


def _sampler_registrations() -> tuple[SamplerRegistration, ...]:
    direct = os.getenv(f"{PREFIX}_SAMPLERS_JSON")
    path = os.getenv(f"{PREFIX}_SAMPLERS_JSON_FILE")
    if bool(direct) == bool(path):
        raise ValueError("set exactly one review materialize target sampler configuration source")
    if direct is not None:
        return parse_sampler_registrations(direct)
    return load_sampler_registrations(Path(str(path)))


def _secret() -> str:
    direct = os.getenv(f"{PREFIX}_TOKEN")
    path = os.getenv(f"{PREFIX}_TOKEN_FILE")
    if bool(direct) == bool(path):
        raise ValueError("set exactly one review materialize target token source")
    value = direct if direct is not None else Path(str(path)).read_text(encoding="utf-8")
    if not value.strip():
        raise ValueError("review materialize target token must be nonempty")
    return value.strip()


def _image_digest() -> str:
    value = os.getenv(f"{PREFIX}_IMAGE_DIGEST", "").strip()
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{PREFIX}_IMAGE_DIGEST must be a lowercase SHA-256")
    return value


def create_app(*, token: str, target: ReviewMaterializeTargetService) -> FastAPI:
    return create_target_app(
        service=SERVICE,
        title="Stove0 review materialize target",
        token=token,
        target=target,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=SERVICE)
    parser.add_argument("--version", action="version", version=importlib.metadata.version(SERVICE))
    parser.add_argument("--host", default=os.getenv(f"{PREFIX}_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv(f"{PREFIX}_PORT", "8080")))
    args = parser.parse_args(argv)
    version = importlib.metadata.version(SERVICE)
    target = ReviewMaterializeTargetService(
        state_root=Path(
            os.getenv(f"{PREFIX}_STATE_ROOT", "/var/lib/stove0-review-materialize-target")
        ),
        workspace_root=Path(os.getenv(f"{PREFIX}_WORKSPACE", "/run/stove0-review")),
        samplers=_sampler_registrations(),
        source_revision=os.getenv(f"{PREFIX}_SOURCE_REVISION", "unknown"),
        image_digest=_image_digest(),
        implementation_version=version,
        terminal_state_retention_seconds=terminal_state_retention_seconds(),
    )
    token = _secret()
    with contextlib.suppress(KeyError):
        os.environ.pop(f"{PREFIX}_TOKEN")
    uvicorn.run(create_app(token=token, target=target), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["create_app", "main"]
