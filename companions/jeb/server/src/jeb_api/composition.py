"""Jeb server configuration and concrete target composition."""

from __future__ import annotations

import os
from collections.abc import Mapping

from jeb_core.adapters.munchy import MunchyTargetAdapter
from jeb_core.collector import (
    Collector,
    JebConfig,
    TargetAdapter,
    TargetConfig,
    env_bool,
    env_int,
    env_value_from,
)
from jeb_core.collector import (
    config_from_env as core_config_from_env,
)


def config_from_env(env: Mapping[str, str] | None = None) -> JebConfig:
    values = os.environ if env is None else env
    target = TargetConfig(
        name="munchy",
        url=(
            env_value_from(values, "JEB_MUNCHY_URL", "http://munchy-server:8080")
            or "http://munchy-server:8080"
        ).rstrip("/"),
        token=env_value_from(values, "JEB_MUNCHY_TOKEN", "") or "",
        upload_workers=max(1, env_int(values, "JEB_MUNCHY_UPLOAD_WORKERS", 4)),
        upload_chunk_bytes=max(1, env_int(values, "JEB_MUNCHY_UPLOAD_CHUNK_MIB", 64)) * 1024 * 1024,
        wait_for_safe_delete=env_bool(values, "JEB_MUNCHY_WAIT_FOR_SAFE_DELETE", True),
    )
    return core_config_from_env(values, targets={target.name: target})


def create_collector(
    config: JebConfig,
    *,
    target_adapters: Mapping[str, TargetAdapter] | None = None,
) -> Collector:
    adapters: dict[str, TargetAdapter] = {"munchy": MunchyTargetAdapter()}
    adapters.update(target_adapters or {})
    return Collector(config, target_adapters=adapters)
