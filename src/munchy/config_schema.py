from __future__ import annotations

from typing import Any

STRING_LIST: dict[str, Any] = {"type": "array", "items": {"type": "string"}}

PATH_PREDICATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "prefix": {"type": "string"},
        "glob": {"type": "string"},
        "filename_glob": {"type": "string"},
        "suffix": {"type": "string"},
        "suffix_in": STRING_LIST,
        "stem_regex": {"type": "string"},
        "basename_regex": {"type": "string"},
    },
    "additionalProperties": False,
}

PREDICATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "all": {"type": "array", "items": {"$ref": "#/$defs/predicate"}},
        "any": {"type": "array", "items": {"$ref": "#/$defs/predicate"}},
        "not": {"$ref": "#/$defs/predicate"},
        "gate": {"oneOf": [{"type": "string"}, STRING_LIST]},
        "not_gate": {"oneOf": [{"type": "string"}, STRING_LIST]},
        "path": PATH_PREDICATE_SCHEMA,
        "fact": {"type": "string"},
        "exists": {"type": "boolean"},
        "equals": True,
        "in": {"type": "array"},
        "contains": True,
        "regex": {"type": "string"},
        "min": {"type": "number"},
        "max": {"type": "number"},
        "between": {
            "type": "array",
            "prefixItems": [{"type": "number"}, {"type": "number"}],
            "minItems": 2,
            "maxItems": 2,
        },
        "pair": {"type": "string"},
        "not_pair": {"type": "string"},
        "pair_role": {"type": "string"},
    },
    "additionalProperties": False,
}

ROUTE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "minLength": 1},
        "action": {"enum": ["upload", "leave"]},
        "group": {"type": "string", "minLength": 1},
        "into": {"type": "string", "minLength": 1},
        "when": {"$ref": "#/$defs/predicate"},
    },
    "required": ["id", "when"],
    "additionalProperties": False,
}

PAIRING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "minLength": 1},
        "key": {"type": "string"},
        "prefer_same_stem": {"type": "boolean"},
        "still": {"$ref": "#/$defs/predicate"},
        "movie": {"$ref": "#/$defs/predicate"},
    },
    "required": ["id", "still", "movie"],
    "additionalProperties": False,
}

SIDECAR_FACT_EXTRACTOR_REQUIREMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tag": {"type": "string", "minLength": 1},
        "equals": True,
        "contains": True,
    },
    "required": ["tag"],
    "oneOf": [{"required": ["equals"]}, {"required": ["contains"]}],
    "additionalProperties": False,
}

SIDECAR_NAME_VALUE_FACT_EXTRACTOR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "type": {"const": "name_value"},
        "name_tag": {"type": "string", "minLength": 1},
        "value_tag": {"type": "string", "minLength": 1},
        "requires": {
            "type": "array",
            "items": SIDECAR_FACT_EXTRACTOR_REQUIREMENT_SCHEMA,
        },
        "fields": {
            "type": "object",
            "additionalProperties": {"type": "string", "minLength": 1},
            "minProperties": 1,
        },
    },
    "required": ["type", "name_tag", "value_tag", "fields"],
    "additionalProperties": False,
}

SIDECAR_FACT_EXTRACTOR_SCHEMA: dict[str, Any] = {
    "oneOf": [SIDECAR_NAME_VALUE_FACT_EXTRACTOR_SCHEMA],
}

SIDECAR_FACTS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "source": {"const": "exiftool"},
        "tags": {"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1},
        "extractors": {
            "type": "array",
            "items": SIDECAR_FACT_EXTRACTOR_SCHEMA,
        },
    },
    "required": ["tags"],
    "additionalProperties": False,
}

SIDECAR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "format": {"type": "string"},
        "path": {"type": "string", "minLength": 1},
        "paths": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "primary": {"$ref": "#/$defs/predicate"},
        "sidecar": {"$ref": "#/$defs/predicate"},
        "facts": SIDECAR_FACTS_SCHEMA,
    },
    "additionalProperties": False,
    "not": {"required": ["path", "paths"]},
}

ROUTING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "gates": {
            "type": "object",
            "additionalProperties": {"$ref": "#/$defs/predicate"},
        },
        "pairings": {"type": "array", "items": PAIRING_SCHEMA},
        "sidecars": {
            "type": "object",
            "additionalProperties": SIDECAR_SCHEMA,
        },
        "routes": {"type": "array", "items": ROUTE_SCHEMA, "minItems": 1},
        "extra_exiftool_tags": STRING_LIST,
    },
    "required": ["routes"],
    "additionalProperties": False,
}

GROUP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "profile": {"type": "string"},
        "output_mode": {"enum": ["video", "audio", "preserve"]},
        "tasks": STRING_LIST,
        "encode_profile": {"type": "object"},
        "max_parallel_encodes": {"type": "integer", "minimum": 1},
        "eager_pipeline_batches": {"type": "integer", "minimum": 1},
        "metadata_projection": {
            "oneOf": [{"type": "boolean", "const": False}, {"type": "object"}],
        },
    },
    "additionalProperties": False,
}

DEVICE_PROFILE_SECTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "collection_slug": {"type": "string"},
        "destination_prefix": {"type": "string"},
        "file_extensions": STRING_LIST,
        "file_include_globs": STRING_LIST,
        "video_extensions": STRING_LIST,
        "output_mode": {"enum": ["video", "audio", "preserve"]},
        "tasks": STRING_LIST,
        "media_preflight": {"type": "boolean"},
        "group": {"type": "string"},
        "encode_profile": {"oneOf": [{"type": "boolean"}, {"type": "object"}]},
        "metadata_projection": {
            "oneOf": [{"type": "boolean", "const": False}, {"type": "object"}],
        },
        "notify": {"type": "object"},
        "profiles": {"type": "object", "additionalProperties": {"type": "object"}},
        "groups": {"type": "object", "additionalProperties": GROUP_SCHEMA},
        "routing": ROUTING_SCHEMA,
    },
    "additionalProperties": False,
}

DEVICE_PROFILE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "required": {"type": "boolean"},
        "default": {"oneOf": [{"type": "string"}, {"type": "number"}, {"type": "boolean"}]},
    },
    "additionalProperties": False,
}

DEVICE_PROFILE_VALUE_SCHEMA: dict[str, Any] = {
    "oneOf": [{"type": "string"}, {"type": "number"}, {"type": "boolean"}],
}

DEVICE_PROFILE_REF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "minLength": 1},
        "parameters": {
            "type": "object",
            "additionalProperties": DEVICE_PROFILE_VALUE_SCHEMA,
        },
        "overrides": DEVICE_PROFILE_SECTION_SCHEMA,
    },
    "required": ["path"],
    "additionalProperties": False,
}

MUNCHY_DEVICE_PROFILE_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$defs": {"predicate": PREDICATE_SCHEMA},
    "type": "object",
    "properties": {
        "schema_version": {"type": "integer", "const": 1},
        "kind": {"type": "string", "const": "munchy.device_profile"},
        "id": {"type": "string", "minLength": 1},
        "parameters": {
            "type": "object",
            "additionalProperties": DEVICE_PROFILE_PARAMETER_SCHEMA,
        },
        "section": DEVICE_PROFILE_SECTION_SCHEMA,
    },
    "required": ["schema_version", "kind", "id", "section"],
    "additionalProperties": False,
}

TARGET_UPLOAD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "enabled": {"type": "boolean"},
        "method": {"enum": ["command", "rclone"]},
        "destination": {"type": "string", "minLength": 1},
        "mode": {"enum": ["copy", "sync"]},
        "exclude": STRING_LIST,
    },
    "additionalProperties": False,
}

COLLECTION_ARCHIVE_RIVERHOG_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "wait": {"enum": ["staged", "finalized"]},
        "archive_store": {"type": "string", "minLength": 1},
        "retain_hot": {"type": "boolean", "default": True},
    },
    "additionalProperties": False,
}

COLLECTION_ARCHIVE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "destination": {"enum": ["riverhog", "target"]},
        "target": TARGET_UPLOAD_SCHEMA,
        "riverhog": COLLECTION_ARCHIVE_RIVERHOG_SCHEMA,
    },
    "additionalProperties": False,
}

JOB_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "job_id": {"type": "string"},
        "input_upload_id": {"type": "string"},
        "collection_slug": {"type": "string"},
        "collection_timestamp": {"type": "string"},
        "destination_prefix": {"type": "string"},
        "workflow_mode": {"enum": ["collection_archive", "review"]},
        "output_mode": {"enum": ["video", "audio", "preserve"]},
        "tasks": STRING_LIST,
        "cleanup_local_on_success": {"type": "boolean"},
        "collection_archive": COLLECTION_ARCHIVE_SCHEMA,
        "review": {"type": "object"},
        "notify": {"type": "object"},
        "routing": ROUTING_SCHEMA,
    },
    "additionalProperties": False,
}

MUNCHY_CONFIG_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$defs": {"predicate": PREDICATE_SCHEMA},
    "type": "object",
    "properties": {
        "schema_version": {"type": "integer", "const": 1},
        "kind": {"type": "string", "const": "munchy.job"},
        "inputs": {
            "type": "object",
            "propertyNames": {"pattern": "^[A-Za-z][A-Za-z0-9_.-]*$"},
            "additionalProperties": {
                "type": "object",
                "properties": {
                    "required": {"type": "boolean"},
                    "enum": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "minItems": 1,
                        "uniqueItems": True,
                    },
                },
                "additionalProperties": False,
            },
        },
        "device_profile": DEVICE_PROFILE_REF_SCHEMA,
        "job": JOB_SCHEMA,
        "profiles": {"type": "object", "additionalProperties": {"type": "object"}},
        "groups": {"type": "object", "additionalProperties": GROUP_SCHEMA},
    },
    "additionalProperties": False,
}
