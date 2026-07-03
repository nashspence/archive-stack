from __future__ import annotations

from typing import Any

from munchy.config_schema import GROUP_SCHEMA, JOB_SCHEMA, PREDICATE_SCHEMA, STRING_LIST

TARGET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "type": {"const": "munchy"},
        "url": {"type": "string"},
        "base_url": {"type": "string"},
        "url_env": {"type": "string"},
        "upload_chunk_mib": {"type": "integer", "minimum": 1},
        "upload_workers": {"type": "integer", "minimum": 1},
        "wait_for_safe_delete": {"type": "boolean"},
    },
    "required": ["type"],
    "additionalProperties": False,
}

SOURCE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "minLength": 1},
        "enabled": {"type": "boolean"},
        "path": {"type": "string", "minLength": 1},
        "upload_prefix": {"type": "string"},
        "stable_age": {"oneOf": [{"type": "string"}, {"type": "number"}]},
        "include_extensions": STRING_LIST,
        "exclude_globs": STRING_LIST,
        "unmatched_policy": {"enum": ["include", "hold"]},
    },
    "required": ["id", "path"],
    "additionalProperties": False,
}

COLLECTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "minLength": 1},
        "enabled": {"type": "boolean"},
        "collection_slug": {"type": "string", "minLength": 1},
        "target": {"type": "string", "minLength": 1},
        "threshold": {"oneOf": [{"type": "string"}, {"type": "number"}]},
        "cleanup": {"enum": ["never", "after_target_success"]},
        "schedule": {"enum": ["weekly", "daily", "manual"]},
        "weekday": {
            "enum": [
                "monday",
                "tuesday",
                "wednesday",
                "thursday",
                "friday",
                "saturday",
                "sunday",
            ]
        },
        "hour": {"type": "integer", "minimum": 0, "maximum": 23},
        "minute": {"type": "integer", "minimum": 0, "maximum": 59},
        "sources": STRING_LIST,
    },
    "required": ["id", "collection_slug", "target", "sources"],
    "additionalProperties": False,
}

COLLECTOR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "interval": {"oneOf": [{"type": "string"}, {"type": "number"}]},
        "state_db": {"type": "string"},
        "batch_dir": {"type": "string"},
        "preflight_repair": {"enum": ["off", "safe_remux"]},
        "preflight_repair_original": {"enum": ["keep_corrupt", "delete"]},
        "preflight_repair_corrupt_dir": {"type": "string"},
        "preflight_repair_ffmpeg": {"type": "string"},
    },
    "additionalProperties": False,
}

NOTIFY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "enabled": {"type": "boolean"},
        "base_url": {"type": "string"},
        "recipients": STRING_LIST,
        "timeout": {"oneOf": [{"type": "string"}, {"type": "number"}]},
    },
    "additionalProperties": False,
}

JEB_CONFIG_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$defs": {"predicate": PREDICATE_SCHEMA},
    "type": "object",
    "properties": {
        "collector": COLLECTOR_SCHEMA,
        "notify": NOTIFY_SCHEMA,
        "targets": {"type": "object", "additionalProperties": TARGET_SCHEMA},
        "profiles": {"type": "object", "additionalProperties": {"type": "object"}},
        "groups": {"type": "object", "additionalProperties": GROUP_SCHEMA},
        "munchy_job": JOB_SCHEMA,
        "sources": {"type": "array", "items": SOURCE_SCHEMA, "minItems": 1},
        "collections": {"type": "array", "items": COLLECTION_SCHEMA, "minItems": 1},
    },
    "required": ["targets", "groups", "munchy_job", "sources", "collections"],
    "additionalProperties": False,
}
