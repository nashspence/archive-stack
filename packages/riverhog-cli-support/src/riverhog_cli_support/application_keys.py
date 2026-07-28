from __future__ import annotations

from collections.abc import Mapping, Sequence

from riverhog_cli_support.output import mapping_items, page_line


def _strings(value: object) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [str(item) for item in value]


def _access(value: object) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    values: list[str] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        permission = str(item.get("permission", "unknown"))
        resource = str(item.get("resource", "*"))
        values.append(permission if resource == "*" else f"{permission}={resource}")
    return values


def format_apps(payload: Mapping[str, object]) -> str:
    lines = [page_line(payload, "apps")]
    for application in mapping_items(payload, "apps"):
        lines.append(
            f"- {application.get('name', 'unknown')}  "
            f"keys={application.get('active_keys', 0)}/{application.get('keys', 0)}  "
            f"last_used={application.get('last_used_at') or 'never'}"
        )
    return "\n".join(lines)


def format_app_keys(payload: Mapping[str, object]) -> str:
    lines = [
        f"app: {payload.get('app', 'unknown')}",
        page_line(payload, "keys"),
    ]
    for key in mapping_items(payload, "keys"):
        details = ""
        if "monthly_download_quota_bytes" in key:
            quota = key.get("monthly_download_quota_bytes")
            details += (
                "  quota=unlimited"
                if quota is None
                else f"  quota={quota}B"
                if quota
                else "  quota=blocked"
            )
        authority = (
            f"access={','.join(_access(key.get('access')))}"
            if "access" in key
            else f"permissions={','.join(_strings(key.get('permissions')))}"
        )
        lines.append(
            f"- {key.get('id', 'unknown')}  status={key.get('status', 'unknown')}  "
            f"{authority}  "
            f"{details}  "
            f"created={key.get('created_at', 'unknown')}  "
            f"expires={key.get('expires_at') or 'never'}  "
            f"last_used={key.get('last_used_at') or 'never'}"
        )
    return "\n".join(lines)


def format_app_key_created(payload: Mapping[str, object]) -> str:
    lines = [
        "app key created",
        f"app: {payload.get('app', 'unknown')}",
        f"key: {payload.get('id', 'unknown')}",
    ]
    if "access" in payload:
        lines.append("access: " + ",".join(_access(payload.get("access"))))
    else:
        lines.append("permissions: " + ",".join(_strings(payload.get("permissions"))))
    if "monthly_download_quota_bytes" in payload:
        quota = payload.get("monthly_download_quota_bytes")
        lines.append(
            "monthly remote-download quota: "
            + ("unlimited" if quota is None else f"{quota}B" if quota else "blocked")
        )
    lines.extend(
        [
            f"expires: {payload.get('expires_at') or 'never'}",
            f"token: {payload.get('token', '')}",
            "Save this token now; it will not be shown again.",
        ]
    )
    return "\n".join(lines)


def format_app_key_rotated(payload: Mapping[str, object]) -> str:
    return format_app_key_created(payload).replace("app key created", "app key rotated", 1)


def format_app_key_revoked(payload: Mapping[str, object]) -> str:
    return "\n".join(
        [
            "app key revoked",
            f"app: {payload.get('app', 'unknown')}",
            f"key: {payload.get('id', 'unknown')}",
            f"revoked: {payload.get('revoked_at', 'unknown')}",
        ]
    )


__all__ = [
    "format_app_key_created",
    "format_app_key_revoked",
    "format_app_key_rotated",
    "format_app_keys",
    "format_apps",
]
