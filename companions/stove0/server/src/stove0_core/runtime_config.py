"""Small, fully connected runtime configuration for stove0."""

from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

DEFAULT_OPERATIONAL_STATE_RETENTION_SECONDS = 30 * 24 * 60 * 60


@dataclass(frozen=True, slots=True)
class EndpointRegistration:
    base_url: str
    token: str | None
    allow_insecure_http: bool
    semantic_validator_providers: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Stove0RuntimeConfig:
    database_url: str
    api_token: str | None
    riverhog_base_url: str
    riverhog_token: str
    riverhog_allow_insecure_http: bool
    recipes_path: Path
    observers: dict[str, EndpointRegistration]
    targets: dict[str, EndpointRegistration]
    workspace_assurance: Literal["encrypted", "ephemeral"]
    claim_lease_seconds: int
    capability_ttl_seconds: int
    scheduler_interval_seconds: float
    operational_state_retention_seconds: int

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        require_api_token: bool = True,
    ) -> Stove0RuntimeConfig:
        values = dict(os.environ if environ is None else environ)
        database_url = cast(str, _secret(values, "STOVE0_DATABASE_URL", required=True))
        api_token = (
            _secret(values, "STOVE0_API_TOKEN", required=True)
            if require_api_token
            else _secret(values, "STOVE0_API_TOKEN", required=False)
        )
        riverhog_base_url = _required(values, "RIVERHOG_BASE_URL")
        riverhog_token = cast(str, _secret(values, "RIVERHOG_TOKEN", required=True))
        recipes_path = Path(_required(values, "STOVE0_RECIPES_PATH")).resolve()
        if not recipes_path.is_file():
            raise ValueError("STOVE0_RECIPES_PATH must name a readable recipe document")
        assurance = values.get("STOVE0_WORKSPACE_ASSURANCE", "encrypted").strip().casefold()
        if assurance not in {"encrypted", "ephemeral"}:
            raise ValueError("STOVE0_WORKSPACE_ASSURANCE must be encrypted or ephemeral")
        return cls(
            database_url=database_url,
            api_token=api_token,
            riverhog_base_url=riverhog_base_url,
            riverhog_token=riverhog_token,
            riverhog_allow_insecure_http=_boolean(
                values,
                "RIVERHOG_ALLOW_INSECURE_HTTP",
                False,
            ),
            recipes_path=recipes_path,
            observers=_registrations(
                values,
                "STOVE0_OBSERVERS_JSON",
                semantic_validators=True,
            ),
            targets=_registrations(values, "STOVE0_TARGETS_JSON"),
            workspace_assurance=cast(
                Literal["encrypted", "ephemeral"],
                assurance,
            ),
            claim_lease_seconds=_integer(
                values,
                "STOVE0_CLAIM_LEASE_SECONDS",
                1800,
                minimum=30,
            ),
            capability_ttl_seconds=_integer(
                values,
                "STOVE0_CAPABILITY_TTL_SECONDS",
                900,
                minimum=30,
            ),
            scheduler_interval_seconds=_number(
                values,
                "STOVE0_SCHEDULER_INTERVAL_SECONDS",
                5.0,
                minimum=0.1,
            ),
            operational_state_retention_seconds=_integer(
                values,
                "STOVE0_OPERATIONAL_STATE_RETENTION_SECONDS",
                DEFAULT_OPERATIONAL_STATE_RETENTION_SECONDS,
                minimum=1,
            ),
        )


def database_url_from_environment(environ: Mapping[str, str] | None = None) -> str:
    values = dict(os.environ if environ is None else environ)
    return cast(str, _secret(values, "STOVE0_DATABASE_URL", required=True))


def _required(values: Mapping[str, str], name: str) -> str:
    value = values.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _secret(
    values: Mapping[str, str],
    name: str,
    *,
    required: bool,
) -> str | None:
    direct = values.get(name, "").strip()
    file_name = values.get(f"{name}_FILE", "").strip()
    if direct and file_name:
        raise ValueError(f"{name} and {name}_FILE are mutually exclusive")
    if file_name:
        value = Path(file_name).expanduser().read_text(encoding="utf-8").strip()
    else:
        value = direct
    if not value:
        if required:
            raise ValueError(f"{name} or {name}_FILE is required")
        return None
    return value


def _boolean(values: Mapping[str, str], name: str, default: bool) -> bool:
    raw = values.get(name)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _integer(
    values: Mapping[str, str],
    name: str,
    default: int,
    *,
    minimum: int,
) -> int:
    try:
        value = int(values.get(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


def _number(
    values: Mapping[str, str],
    name: str,
    default: float,
    *,
    minimum: float,
) -> float:
    try:
        value = float(values.get(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not math.isfinite(value) or value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


def _registrations(
    values: Mapping[str, str],
    name: str,
    *,
    semantic_validators: bool = False,
) -> dict[str, EndpointRegistration]:
    raw = values.get(name, "{}").strip() or "{}"
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} must be a JSON object") from exc
    if not isinstance(document, dict):
        raise ValueError(f"{name} must be a JSON object")
    registrations: dict[str, EndpointRegistration] = {}
    for registration_id, value in sorted(document.items()):
        if not isinstance(registration_id, str) or not isinstance(value, dict):
            raise ValueError(f"{name} registrations are invalid")
        base_url = str(value.get("base_url") or "").strip()
        token_env = str(value.get("token_env") or "").strip()
        if not base_url:
            raise ValueError(f"{name} registration {registration_id} has no base_url")
        token = _secret(values, token_env, required=True) if token_env else None
        if token_env and not token:
            raise ValueError(f"{name} registration {registration_id} requires secret {token_env}")
        allow = value.get("allow_insecure_http", False)
        if not isinstance(allow, bool):
            raise ValueError(
                f"{name} registration {registration_id} allow_insecure_http must be boolean"
            )
        raw_providers = value.get("semantic_validator_providers", [])
        if not semantic_validators and raw_providers:
            raise ValueError(
                f"{name} registration {registration_id} cannot configure semantic validators"
            )
        if not isinstance(raw_providers, list) or any(
            not isinstance(provider, str) or not provider.strip() for provider in raw_providers
        ):
            raise ValueError(
                f"{name} registration {registration_id} semantic validator providers are invalid"
            )
        providers = tuple(sorted(provider.strip() for provider in raw_providers))
        if len(providers) != len(set(providers)):
            raise ValueError(
                f"{name} registration {registration_id} semantic validator providers repeat"
            )
        registrations[registration_id] = EndpointRegistration(
            base_url=base_url,
            token=token,
            allow_insecure_http=allow,
            semantic_validator_providers=providers,
        )
    return registrations


__all__ = [
    "DEFAULT_OPERATIONAL_STATE_RETENTION_SECONDS",
    "EndpointRegistration",
    "Stove0RuntimeConfig",
]
