from __future__ import annotations

from collections.abc import Mapping, Sequence

from cli_support.output import mapping_items, page_line


def _strings(value: object) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [str(item) for item in value]


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
        lines.append(
            f"- {key.get('id', 'unknown')}  status={key.get('status', 'unknown')}  "
            f"permissions={','.join(_strings(key.get('permissions')))}  "
            f"created={key.get('created_at', 'unknown')}  "
            f"expires={key.get('expires_at') or 'never'}  "
            f"last_used={key.get('last_used_at') or 'never'}"
        )
    return "\n".join(lines)


def format_app_key_created(payload: Mapping[str, object]) -> str:
    return "\n".join(
        [
            "app key created",
            f"app: {payload.get('app', 'unknown')}",
            f"key: {payload.get('id', 'unknown')}",
            "permissions: " + ",".join(_strings(payload.get("permissions"))),
            f"expires: {payload.get('expires_at') or 'never'}",
            f"token: {payload.get('token', '')}",
            "Save this token now; it will not be shown again.",
        ]
    )


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
    "format_app_keys",
    "format_apps",
]
