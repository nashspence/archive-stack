from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from config_validation import ConfigError


def normalize_authoring_routing(
    raw: Mapping[str, Any],
    *,
    label: str,
) -> dict[str, Any]:
    routing = deepcopy(dict(raw))
    sidecars = routing.get("sidecars")
    if isinstance(sidecars, Mapping):
        normalized_sidecars: list[dict[str, Any]] = []
        for sidecar_id, sidecar_raw in sidecars.items():
            if not isinstance(sidecar_raw, Mapping):
                raise ConfigError(f"{label}.sidecars.{sidecar_id} must be a mapping")
            sidecar = deepcopy(dict(sidecar_raw))
            if "id" in sidecar:
                raise ConfigError(f"{label}.sidecars.{sidecar_id} must not repeat id")
            sidecar["id"] = str(sidecar_id)
            normalized_sidecars.append(sidecar)
        routing["sidecars"] = normalized_sidecars
    return routing


def normalize_munchy_job_authoring(
    raw: Mapping[str, Any],
    *,
    label: str,
) -> dict[str, Any]:
    job = deepcopy(dict(raw))
    routing = job.get("routing")
    if routing is not None:
        if not isinstance(routing, Mapping):
            raise ConfigError(f"{label}.routing must be a mapping")
        job["routing"] = normalize_authoring_routing(routing, label=f"{label}.routing")
    return job
