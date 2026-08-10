from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, cast

from lifecycle_events.repeats import (
    event_repeat_zone,
    normalize_event_repeat_time,
)

from jeb_core.domain.models import (
    JebConfig,
    JebIngressConfig,
    LifecycleEventSettings,
    ServiceSettings,
    TargetConfig,
    parse_duration,
)


def env_bool(env: Mapping[str, str], name: str, default: bool = False) -> bool:
    value = env.get(name)
    if value is None or not value.strip():
        return default
    text = value.strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be boolean")


def env_csv(env: Mapping[str, str], name: str, default: Sequence[str] = ()) -> tuple[str, ...]:
    value = env.get(name)
    if value is None or not value.strip():
        return tuple(default)
    return tuple(item.strip() for item in value.split(",") if item.strip())


def env_value_from(env: Mapping[str, str], name: str, default: str | None = None) -> str | None:
    value = env.get(name)
    if value is not None and value.strip():
        return value.strip()
    return default


def required_env(env: Mapping[str, str], name: str) -> str:
    value = env_value_from(env, name)
    if value is None:
        raise ValueError(f"{name} is required")
    return value


def env_int(env: Mapping[str, str], name: str, default: int) -> int:
    value = env_value_from(env, name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def config_from_env(
    env: Mapping[str, str] | None = None,
    *,
    targets: Mapping[str, TargetConfig] | None = None,
) -> JebConfig:
    values = os.environ if env is None else env
    landing_dir = Path(
        os.path.expandvars(env_value_from(values, "JEB_LANDING_DIR", "/landing") or "/landing")
    )
    state_dir = Path(
        os.path.expandvars(env_value_from(values, "JEB_STATE_DIR", "/state") or "/state")
    )
    tus_incomplete_max_age_seconds = parse_duration(
        env_value_from(values, "JEB_TUS_INCOMPLETE_MAX_AGE", "14d")
    )
    if tus_incomplete_max_age_seconds < 1:
        raise ValueError("JEB_TUS_INCOMPLETE_MAX_AGE must be positive")
    ingress = JebIngressConfig(
        landing_dir=landing_dir,
        tus_staging_dir=Path(
            os.path.expandvars(
                env_value_from(
                    values,
                    "JEB_TUS_STAGING_DIR",
                    str(landing_dir / ".ingress" / "tus"),
                )
                or str(landing_dir / ".ingress" / "tus")
            )
        ),
        provenance_installation_id_path=state_dir / "provenance-installation-id",
        tusd_base_url=(
            env_value_from(
                values,
                "JEB_TUSD_BASE_URL",
                "http://jeb-tusd:1080/files/",
            )
            or "http://jeb-tusd:1080/files/"
        ).rstrip("/")
        + "/",
        tus_incomplete_max_age_seconds=tus_incomplete_max_age_seconds,
        ftp_projection=Path(
            os.path.expandvars(
                env_value_from(
                    values,
                    "JEB_FTP_PROJECTION",
                    str(state_dir / "ingress" / "ftp" / "passwd"),
                )
                or str(state_dir / "ingress" / "ftp" / "passwd")
            )
        ),
        ftp_uid=env_int(values, "JEB_FTP_UID", 1000),
        ftp_gid=env_int(values, "JEB_FTP_GID", 1000),
    )

    preflight_repair = env_value_from(values, "JEB_PREFLIGHT_REPAIR", "safe_remux") or "safe_remux"
    if preflight_repair not in {"off", "safe_remux"}:
        raise ValueError("JEB_PREFLIGHT_REPAIR must be off or safe_remux")
    preflight_repair_original = (
        env_value_from(values, "JEB_PREFLIGHT_REPAIR_ORIGINAL", "keep_corrupt") or "keep_corrupt"
    )
    if preflight_repair_original not in {"keep_corrupt", "delete"}:
        raise ValueError("JEB_PREFLIGHT_REPAIR_ORIGINAL must be keep_corrupt or delete")
    service = ServiceSettings(
        interval_seconds=parse_duration(env_value_from(values, "JEB_INTERVAL"), 300),
        state_db=Path(
            os.path.expandvars(
                env_value_from(values, "JEB_STATE_DB", str(state_dir / "jeb.sqlite3"))
                or str(state_dir / "jeb.sqlite3")
            )
        ),
        batch_dir=Path(
            os.path.expandvars(
                env_value_from(
                    values,
                    "JEB_BATCH_DIR",
                    str(landing_dir / ".jeb-batches"),
                )
                or str(landing_dir / ".jeb-batches")
            )
        ),
        preflight_repair=cast(Literal["off", "safe_remux"], preflight_repair),
        preflight_repair_original=cast(
            Literal["keep_corrupt", "delete"], preflight_repair_original
        ),
        preflight_repair_corrupt_dir=Path(
            os.path.expandvars(
                env_value_from(
                    values,
                    "JEB_PREFLIGHT_REPAIR_CORRUPT_DIR",
                    str(landing_dir / "_corrupt"),
                )
                or str(landing_dir / "_corrupt")
            )
        ),
        preflight_repair_ffmpeg=env_value_from(values, "JEB_PREFLIGHT_REPAIR_FFMPEG", "ffmpeg")
        or "ffmpeg",
    )

    repeat_time = normalize_event_repeat_time(env_value_from(values, "JEB_EVENT_REPEAT_TIME"))
    repeat_timezone = env_value_from(values, "JEB_EVENT_REPEAT_TIMEZONE", "UTC") or "UTC"
    event_repeat_zone(repeat_timezone)
    events = LifecycleEventSettings(
        source=env_value_from(values, "JEB_EVENT_SOURCE", "urn:jeb") or "urn:jeb",
        upstream_poll_seconds=max(
            1.0,
            float(env_value_from(values, "JEB_UPSTREAM_EVENT_POLL_SECONDS", "5") or "5"),
        ),
        context_retention_seconds=parse_duration(
            env_value_from(values, "JEB_EVENT_CONTEXT_RETENTION", "30d"),
            30 * 86_400,
        ),
        repeat_interval_seconds=parse_duration(
            env_value_from(values, "JEB_EVENT_REPEAT_INTERVAL", "24h"),
            86_400,
        ),
        repeat_time=repeat_time,
        repeat_timezone=repeat_timezone,
    )

    return JebConfig(
        service=service,
        ingress=ingress,
        events=events,
        targets=dict(targets or {}),
    )


def mapping(value: Any) -> Mapping[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return value
    raise ValueError(f"expected table/object, got {type(value).__name__}")


def sequence(value: Any) -> Sequence[Any]:
    if value is None:
        return ()
    if isinstance(value, list | tuple):
        return value
    raise ValueError(f"expected list, got {type(value).__name__}")


def optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
