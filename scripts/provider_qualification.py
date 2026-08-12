#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, cast

CONFIG_SCHEMA = "riverhog-provider-qualification-config/v1"
PLAN_SCHEMA = "riverhog-provider-qualification-infrastructure-plan/v1"
CORPUS_SCHEMA = "riverhog-provider-qualification-corpus/v1"
CHECKPOINT_SCHEMA = "riverhog-provider-qualification-checkpoint/v1"
EVIDENCE_SCHEMA = "riverhog-provider-qualification-evidence/v1"
QUALIFICATION_MARKER = "riverhog-provider-qualification"
AWS_DEEP_ARCHIVE_MINIMUM_DAYS = 180
DEFAULT_AWS_EXPIRATION_DAYS = 185
DEFAULT_B2_HIDE_DAYS = 7
DEFAULT_B2_DELETE_DAYS = 1
DEFAULT_MULTIPART_ABORT_DAYS = 3
DEFAULT_RESTORE_COPY_DAYS = 3
DEFAULT_RESTORE_DEADLINE_HOURS = 96
MIB = 1024 * 1024
QUALIFICATION_MONTHLY_DOWNLOAD_QUOTA_BYTES = 2 * 1024 * MIB
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_SOURCE_SHA_RE = re.compile(r"[0-9a-f]{40}")
_ENV_NAME_RE = re.compile(r"[A-Z][A-Z0-9_]*")
_PREFIX_RE = re.compile(r"[a-z0-9](?:[a-z0-9._/-]*[a-z0-9])?")
_EXPECTED_ROLES = {
    "b2-archive": ("b2", "archive"),
    "b2-retrieval-cache": ("b2", "retrieval-cache"),
    "aws-deep-archive": ("aws", "deep-archive"),
}
_PHASES = (
    "created",
    "immediate-qualified",
    "deep-archive-uploaded",
    "restore-requested",
    "restore-pending",
    "restored",
    "verified",
    "cleaned",
    "failed",
)
_TRANSITIONS = {
    "created": {"immediate-qualified", "failed"},
    "immediate-qualified": {"deep-archive-uploaded", "failed"},
    "deep-archive-uploaded": {"restore-requested", "failed"},
    "restore-requested": {"restore-pending", "restored", "failed"},
    "restore-pending": {"restore-pending", "restored", "failed"},
    "restored": {"verified", "failed"},
    "verified": {"cleaned", "failed"},
    "cleaned": set(),
    "failed": set(),
}


class QualificationError(RuntimeError):
    """A safe, operator-actionable qualification failure."""


@dataclass(frozen=True, slots=True)
class BucketDefinition:
    logical_name: str
    provider: str
    role: str
    name_env: str
    region_env: str


@dataclass(frozen=True, slots=True)
class B2ProvisioningDefinition:
    key_id_env: str
    application_key_env: str


@dataclass(frozen=True, slots=True)
class CloudFrontDefinition:
    enabled: bool
    public_key_path_env: str
    private_key_path_env: str


@dataclass(frozen=True, slots=True)
class QualificationConfig:
    namespace_prefix: str
    aws_expiration_days: int
    b2_hide_days: int
    b2_delete_days: int
    multipart_abort_days: int
    restore_copy_days: int
    restore_deadline_hours: int
    restore_tier: str
    b2_provisioning: B2ProvisioningDefinition
    cloudfront: CloudFrontDefinition
    buckets: tuple[BucketDefinition, ...]
    config_sha256: str

    def bucket(self, logical_name: str) -> BucketDefinition:
        for bucket in self.buckets:
            if bucket.logical_name == logical_name:
                return bucket
        raise QualificationError(f"qualification bucket is not configured: {logical_name}")


@dataclass(frozen=True, slots=True)
class ResolvedBucket:
    logical_name: str
    provider: str
    role: str
    bucket_name: str
    region: str


@dataclass(frozen=True, slots=True)
class ProviderContract:
    logical_name: str
    provider: str
    role: str
    region: str
    storage_class: str
    read_mode: str


@dataclass(frozen=True, slots=True)
class ArtifactIdentity:
    surface: str
    sha256: str
    objects: int
    bytes: int


@dataclass(frozen=True, slots=True)
class InfrastructureAction:
    logical_name: str
    provider: str
    action: str
    changes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class InfrastructurePlan:
    config_sha256: str
    actions: tuple[InfrastructureAction, ...]

    @property
    def ready(self) -> bool:
        return all(action.action == "ready" for action in self.actions)

    @property
    def blocked(self) -> bool:
        return any(action.action == "blocked" for action in self.actions)

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": PLAN_SCHEMA,
            "config_sha256": self.config_sha256,
            "ready": self.ready,
            "blocked": self.blocked,
            "actions": [asdict(action) for action in self.actions],
        }


@dataclass(frozen=True, slots=True)
class CorpusFile:
    path: str
    bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class CorpusManifest:
    profile: str
    files: tuple[CorpusFile, ...]
    bytes: int
    sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": CORPUS_SCHEMA,
            "profile": self.profile,
            "files": [asdict(item) for item in self.files],
            "bytes": self.bytes,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class PhaseRecord:
    phase: str
    at: str
    assertions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class QualificationCheckpoint:
    run_id: str
    source_sha: str
    source_ref: str
    config_sha256: str
    corpus_sha256: str
    corpus_profile: str
    corpus_files: int
    corpus_bytes: int
    restore_tier: str
    restore_copy_days: int
    provider_binding_sha256: str
    providers: tuple[ProviderContract, ...]
    artifacts: tuple[ArtifactIdentity, ...]
    namespace: str
    phase: str
    generation: int
    previous_checkpoint_sha256: str | None
    started_at: str
    updated_at: str
    restore_deadline_at: str
    collection_id: int | None
    retrieval_job_id: str | None
    history: tuple[PhaseRecord, ...]
    checkpoint_sha256: str

    def as_dict(self, *, include_digest: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema": CHECKPOINT_SCHEMA,
            "run_id": self.run_id,
            "source_sha": self.source_sha,
            "source_ref": self.source_ref,
            "config_sha256": self.config_sha256,
            "corpus_sha256": self.corpus_sha256,
            "corpus_profile": self.corpus_profile,
            "corpus_files": self.corpus_files,
            "corpus_bytes": self.corpus_bytes,
            "restore_tier": self.restore_tier,
            "restore_copy_days": self.restore_copy_days,
            "provider_binding_sha256": self.provider_binding_sha256,
            "providers": [asdict(item) for item in self.providers],
            "artifacts": [asdict(item) for item in self.artifacts],
            "namespace": self.namespace,
            "phase": self.phase,
            "generation": self.generation,
            "previous_checkpoint_sha256": self.previous_checkpoint_sha256,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "restore_deadline_at": self.restore_deadline_at,
            "collection_id": self.collection_id,
            "retrieval_job_id": self.retrieval_job_id,
            "history": [asdict(record) for record in self.history],
        }
        if include_digest:
            payload["checkpoint_sha256"] = self.checkpoint_sha256
        return payload


class BucketManager(Protocol):
    def plan(self, bucket: ResolvedBucket, config: QualificationConfig) -> InfrastructureAction: ...

    def apply(self, bucket: ResolvedBucket, config: QualificationConfig) -> None: ...


class AdditionalInfrastructureManager(Protocol):
    def plan(self) -> InfrastructureAction: ...

    def apply(self) -> None: ...


def _canonical_json(payload: object) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise QualificationError("qualification timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise QualificationError("qualification timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _expect_mapping(value: object, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise QualificationError(f"{label} must be a TOML table")
    return cast(dict[str, Any], value)


def _expect_int(table: Mapping[str, object], name: str, default: int) -> int:
    value = table.get(name, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise QualificationError(f"{name} must be a positive integer")
    return value


def _expect_string(table: Mapping[str, object], name: str, *, label: str) -> str:
    value = table.get(name)
    if not isinstance(value, str) or not value.strip():
        raise QualificationError(f"{label}.{name} must be a non-empty string")
    return value.strip()


def _expect_env_name(table: Mapping[str, object], name: str, *, label: str) -> str:
    value = _expect_string(table, name, label=label)
    if _ENV_NAME_RE.fullmatch(value) is None:
        raise QualificationError(f"{label}.{name} must name an uppercase environment variable")
    return value


def _expect_bool(table: Mapping[str, object], name: str, *, label: str) -> bool:
    value = table.get(name)
    if not isinstance(value, bool):
        raise QualificationError(f"{label}.{name} must be true or false")
    return value


def load_config(path: Path) -> QualificationConfig:
    try:
        content = path.read_bytes()
        raw = tomllib.loads(content.decode("utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise QualificationError(f"cannot load qualification config: {exc}") from exc
    if raw.get("schema") != CONFIG_SCHEMA:
        raise QualificationError(f"qualification config schema must be {CONFIG_SCHEMA}")
    namespace_prefix = _expect_string(raw, "namespace_prefix", label="config").strip("/")
    if _PREFIX_RE.fullmatch(namespace_prefix) is None or "//" in namespace_prefix:
        raise QualificationError("namespace_prefix must be a canonical relative object prefix")
    retention = _expect_mapping(raw.get("retention"), label="retention")
    aws_expiration_days = _expect_int(
        retention, "aws_deep_archive_expiration_days", DEFAULT_AWS_EXPIRATION_DAYS
    )
    if aws_expiration_days < AWS_DEEP_ARCHIVE_MINIMUM_DAYS:
        raise QualificationError(
            "aws_deep_archive_expiration_days must honor the 180-day provider minimum"
        )
    b2_hide_days = _expect_int(retention, "b2_hide_days", DEFAULT_B2_HIDE_DAYS)
    b2_delete_days = _expect_int(retention, "b2_delete_days", DEFAULT_B2_DELETE_DAYS)
    multipart_abort_days = _expect_int(
        retention, "multipart_abort_days", DEFAULT_MULTIPART_ABORT_DAYS
    )
    restore = _expect_mapping(raw.get("restore"), label="restore")
    restore_copy_days = _expect_int(restore, "copy_days", DEFAULT_RESTORE_COPY_DAYS)
    restore_deadline_hours = _expect_int(restore, "deadline_hours", DEFAULT_RESTORE_DEADLINE_HOURS)
    restore_tier = _expect_string(restore, "tier", label="restore").casefold()
    if restore_tier != "bulk":
        raise QualificationError("restore.tier must be bulk for the bounded qualification")
    b2 = _expect_mapping(raw.get("b2_provisioning"), label="b2_provisioning")
    b2_provisioning = B2ProvisioningDefinition(
        key_id_env=_expect_env_name(b2, "key_id_env", label="b2_provisioning"),
        application_key_env=_expect_env_name(b2, "application_key_env", label="b2_provisioning"),
    )
    cloudfront_table = _expect_mapping(raw.get("cloudfront"), label="cloudfront")
    cloudfront = CloudFrontDefinition(
        enabled=_expect_bool(cloudfront_table, "enabled", label="cloudfront"),
        public_key_path_env=_expect_env_name(
            cloudfront_table,
            "public_key_path_env",
            label="cloudfront",
        ),
        private_key_path_env=_expect_env_name(
            cloudfront_table,
            "private_key_path_env",
            label="cloudfront",
        ),
    )
    raw_buckets = raw.get("buckets")
    if not isinstance(raw_buckets, list):
        raise QualificationError("buckets must be an array of tables")
    buckets: list[BucketDefinition] = []
    for index, item in enumerate(raw_buckets):
        table = _expect_mapping(item, label=f"buckets[{index}]")
        label = f"buckets[{index}]"
        buckets.append(
            BucketDefinition(
                logical_name=_expect_string(table, "logical_name", label=label),
                provider=_expect_string(table, "provider", label=label).casefold(),
                role=_expect_string(table, "role", label=label).casefold(),
                name_env=_expect_env_name(table, "name_env", label=label),
                region_env=_expect_env_name(table, "region_env", label=label),
            )
        )
    actual = {bucket.logical_name: (bucket.provider, bucket.role) for bucket in buckets}
    if actual != _EXPECTED_ROLES or len(actual) != len(buckets):
        raise QualificationError(
            "buckets must define exactly b2-archive, b2-retrieval-cache, and "
            "aws-deep-archive with their canonical providers and roles"
        )
    name_envs = [bucket.name_env for bucket in buckets]
    if len(set(name_envs)) != len(name_envs):
        raise QualificationError("each qualification bucket must use a distinct name_env")
    structural = {
        "schema": CONFIG_SCHEMA,
        "namespace_prefix": namespace_prefix,
        "retention": {
            "aws_deep_archive_expiration_days": aws_expiration_days,
            "b2_hide_days": b2_hide_days,
            "b2_delete_days": b2_delete_days,
            "multipart_abort_days": multipart_abort_days,
        },
        "restore": {
            "copy_days": restore_copy_days,
            "deadline_hours": restore_deadline_hours,
            "tier": restore_tier,
        },
        "b2_provisioning": asdict(b2_provisioning),
        "cloudfront": asdict(cloudfront),
        "buckets": [
            asdict(bucket) for bucket in sorted(buckets, key=lambda item: item.logical_name)
        ],
    }
    return QualificationConfig(
        namespace_prefix=namespace_prefix,
        aws_expiration_days=aws_expiration_days,
        b2_hide_days=b2_hide_days,
        b2_delete_days=b2_delete_days,
        multipart_abort_days=multipart_abort_days,
        restore_copy_days=restore_copy_days,
        restore_deadline_hours=restore_deadline_hours,
        restore_tier=restore_tier,
        b2_provisioning=b2_provisioning,
        cloudfront=cloudfront,
        buckets=tuple(sorted(buckets, key=lambda item: item.logical_name)),
        config_sha256=hashlib.sha256(_canonical_json(structural)).hexdigest(),
    )


def _required_env(values: Mapping[str, str], name: str) -> str:
    value = values.get(name, "").strip()
    if not value:
        raise QualificationError(f"required environment variable is unset: {name}")
    return value


def resolve_buckets(
    config: QualificationConfig,
    values: Mapping[str, str],
) -> tuple[ResolvedBucket, ...]:
    resolved = tuple(
        ResolvedBucket(
            logical_name=bucket.logical_name,
            provider=bucket.provider,
            role=bucket.role,
            bucket_name=_required_env(values, bucket.name_env),
            region=_required_env(values, bucket.region_env),
        )
        for bucket in config.buckets
    )
    names = [bucket.bucket_name for bucket in resolved]
    if len(set(names)) != len(names):
        raise QualificationError("qualification roles must resolve to distinct buckets")
    return resolved


def _provider_binding_sha256(buckets: Sequence[ResolvedBucket]) -> str:
    return hashlib.sha256(
        _canonical_json(
            [asdict(bucket) for bucket in sorted(buckets, key=lambda item: item.logical_name)]
        )
    ).hexdigest()


def _provider_contracts(buckets: Sequence[ResolvedBucket]) -> tuple[ProviderContract, ...]:
    return tuple(
        ProviderContract(
            logical_name=bucket.logical_name,
            provider=bucket.provider,
            role=bucket.role,
            region=bucket.region,
            storage_class="DEEP_ARCHIVE" if bucket.provider == "aws" else "STANDARD",
            read_mode="restore_required" if bucket.provider == "aws" else "immediate",
        )
        for bucket in sorted(buckets, key=lambda item: item.logical_name)
    )


def _verify_checkpoint_providers(
    checkpoint: QualificationCheckpoint,
    buckets: Sequence[ResolvedBucket],
    config: QualificationConfig,
) -> None:
    if checkpoint.namespace != f"{config.namespace_prefix}/{checkpoint.run_id}":
        raise QualificationError("checkpoint namespace is not qualification-owned")
    if (
        checkpoint.restore_tier != config.restore_tier
        or checkpoint.restore_copy_days != config.restore_copy_days
    ):
        raise QualificationError("checkpoint and restore policy do not match")
    if checkpoint.provider_binding_sha256 != _provider_binding_sha256(buckets):
        raise QualificationError("checkpoint and resolved provider resources do not match")
    if checkpoint.providers != _provider_contracts(buckets):
        raise QualificationError("checkpoint and provider contracts do not match")


def _aws_lifecycle(config: QualificationConfig) -> dict[str, object]:
    return {
        "Rules": [
            {
                "ID": "riverhog-qualification-retention-v1",
                "Status": "Enabled",
                "Filter": {"Prefix": f"{config.namespace_prefix}/"},
                "Expiration": {"Days": config.aws_expiration_days},
                "AbortIncompleteMultipartUpload": {
                    "DaysAfterInitiation": config.multipart_abort_days
                },
            }
        ]
    }


def _normalize_aws_lifecycle(payload: Mapping[str, object]) -> dict[str, object]:
    rules = payload.get("Rules")
    if not isinstance(rules, list):
        return {"Rules": []}
    normalized: list[dict[str, object]] = []
    for item in rules:
        if not isinstance(item, dict):
            continue
        expiration = item.get("Expiration")
        abort = item.get("AbortIncompleteMultipartUpload")
        normalized.append(
            {
                "ID": item.get("ID"),
                "Status": item.get("Status"),
                "Filter": item.get("Filter", {}),
                "Expiration": {
                    "Days": expiration.get("Days") if isinstance(expiration, dict) else None
                },
                "AbortIncompleteMultipartUpload": {
                    "DaysAfterInitiation": (
                        abort.get("DaysAfterInitiation") if isinstance(abort, dict) else None
                    )
                },
            }
        )
    return {"Rules": sorted(normalized, key=lambda item: str(item.get("ID")))}


def _client_error_code(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return ""
    error = response.get("Error")
    return str(error.get("Code", "")) if isinstance(error, dict) else ""


class AwsBucketManager:
    def __init__(self, client: object) -> None:
        self.client = client

    def _call(self, name: str, **kwargs: object) -> dict[str, Any]:
        method = getattr(self.client, name)
        return cast(dict[str, Any], method(**kwargs))

    def _exists(self, bucket: ResolvedBucket) -> bool:
        try:
            self._call("head_bucket", Bucket=bucket.bucket_name)
        except Exception as exc:
            if _client_error_code(exc) in {"404", "NoSuchBucket", "NotFound"}:
                return False
            raise QualificationError(
                f"cannot inspect {bucket.logical_name} bucket availability"
            ) from exc
        return True

    def _optional(
        self, operation: str, bucket: ResolvedBucket, missing: set[str]
    ) -> dict[str, Any]:
        try:
            return self._call(operation, Bucket=bucket.bucket_name)
        except Exception as exc:
            if _client_error_code(exc) in missing:
                return {}
            raise QualificationError(
                f"cannot inspect {bucket.logical_name} {operation.removeprefix('get_bucket_')}"
            ) from exc

    def _changes(self, bucket: ResolvedBucket, config: QualificationConfig) -> tuple[str, ...]:
        changes: list[str] = []
        tags_payload = self._optional("get_bucket_tagging", bucket, {"NoSuchTagSet"})
        tags = {
            str(item.get("Key")): str(item.get("Value"))
            for item in tags_payload.get("TagSet", [])
            if isinstance(item, dict)
        }
        marker = tags.get("riverhog-purpose")
        logical = tags.get("riverhog-logical-name")
        if marker != QUALIFICATION_MARKER or logical != bucket.logical_name:
            return ("ownership-marker",)
        if tags != {
            "riverhog-purpose": QUALIFICATION_MARKER,
            "riverhog-logical-name": bucket.logical_name,
        }:
            return ("unmanaged-tags",)
        versioning = self._call("get_bucket_versioning", Bucket=bucket.bucket_name).get("Status")
        if versioning is not None:
            changes.append("versioning")
        encryption = self._optional(
            "get_bucket_encryption",
            bucket,
            {"ServerSideEncryptionConfigurationNotFoundError"},
        )
        algorithms = [
            rule.get("ApplyServerSideEncryptionByDefault", {}).get("SSEAlgorithm")
            for rule in encryption.get("ServerSideEncryptionConfiguration", {}).get("Rules", [])
            if isinstance(rule, dict)
        ]
        if algorithms != ["AES256"]:
            changes.append("default-encryption")
        public = self._optional(
            "get_public_access_block",
            bucket,
            {"NoSuchPublicAccessBlockConfiguration"},
        ).get("PublicAccessBlockConfiguration", {})
        if public != {
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        }:
            changes.append("public-access-block")
        ownership = self._optional(
            "get_bucket_ownership_controls",
            bucket,
            {"OwnershipControlsNotFoundError", "NoSuchOwnershipControls"},
        ).get("OwnershipControls", {})
        rules = ownership.get("Rules", []) if isinstance(ownership, dict) else []
        if rules != [{"ObjectOwnership": "BucketOwnerEnforced"}]:
            changes.append("object-ownership")
        lifecycle = self._optional(
            "get_bucket_lifecycle_configuration",
            bucket,
            {"NoSuchLifecycleConfiguration"},
        )
        if _normalize_aws_lifecycle(lifecycle) != _normalize_aws_lifecycle(_aws_lifecycle(config)):
            changes.append("lifecycle")
        return tuple(changes)

    def plan(self, bucket: ResolvedBucket, config: QualificationConfig) -> InfrastructureAction:
        if not self._exists(bucket):
            return InfrastructureAction(bucket.logical_name, "aws", "create", ("bucket",))
        changes = self._changes(bucket, config)
        if "ownership-marker" in changes or "unmanaged-tags" in changes or "versioning" in changes:
            return InfrastructureAction(bucket.logical_name, "aws", "blocked", changes)
        return InfrastructureAction(
            bucket.logical_name,
            "aws",
            "update" if changes else "ready",
            changes,
        )

    def apply(self, bucket: ResolvedBucket, config: QualificationConfig) -> None:
        action = self.plan(bucket, config)
        if action.action == "blocked":
            raise QualificationError(
                f"{bucket.logical_name} has state outside its exact qualification contract"
            )
        if action.action == "create":
            kwargs: dict[str, object] = {"Bucket": bucket.bucket_name}
            if bucket.region != "us-east-1":
                kwargs["CreateBucketConfiguration"] = {"LocationConstraint": bucket.region}
            self._call("create_bucket", **kwargs)
        self._call(
            "put_bucket_tagging",
            Bucket=bucket.bucket_name,
            Tagging={
                "TagSet": [
                    {"Key": "riverhog-purpose", "Value": QUALIFICATION_MARKER},
                    {"Key": "riverhog-logical-name", "Value": bucket.logical_name},
                ]
            },
        )
        self._call(
            "put_public_access_block",
            Bucket=bucket.bucket_name,
            PublicAccessBlockConfiguration={
                "BlockPublicAcls": True,
                "IgnorePublicAcls": True,
                "BlockPublicPolicy": True,
                "RestrictPublicBuckets": True,
            },
        )
        self._call(
            "put_bucket_ownership_controls",
            Bucket=bucket.bucket_name,
            OwnershipControls={"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]},
        )
        self._call(
            "put_bucket_encryption",
            Bucket=bucket.bucket_name,
            ServerSideEncryptionConfiguration={
                "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
            },
        )
        self._call(
            "put_bucket_lifecycle_configuration",
            Bucket=bucket.bucket_name,
            LifecycleConfiguration=_aws_lifecycle(config),
        )
        final = self.plan(bucket, config)
        if final.action != "ready":
            raise QualificationError(f"{bucket.logical_name} did not converge after apply")


def _b2_lifecycle(config: QualificationConfig) -> list[dict[str, object]]:
    return [
        {
            "fileNamePrefix": f"{config.namespace_prefix}/",
            "daysFromUploadingToHiding": config.b2_hide_days,
            "daysFromHidingToDeleting": config.b2_delete_days,
            "daysFromStartingToCancelingUnfinishedLargeFiles": config.multipart_abort_days,
        }
    ]


class B2NativeClient:
    def __init__(self, *, key_id: str, application_key: str) -> None:
        self.key_id = key_id
        self.application_key = application_key
        self.account_id = ""
        self.api_url = ""
        self.authorization_token = ""

    def _request(
        self,
        url: str,
        *,
        authorization: str,
        payload: Mapping[str, object] | None = None,
    ) -> dict[str, Any]:
        data = _canonical_json(payload) if payload is not None else None
        request = urllib.request.Request(
            url,
            method="POST" if payload is not None else "GET",
            data=data,
            headers={
                "Authorization": authorization,
                "Accept": "application/json",
                **({"Content-Type": "application/json"} if data is not None else {}),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                error = json.loads(exc.read())
                code = error.get("code", "request_failed")
            except (OSError, ValueError, AttributeError):
                code = "request_failed"
            raise QualificationError(f"Backblaze API request failed: {code}") from exc
        except (OSError, ValueError) as exc:
            raise QualificationError("Backblaze API request failed") from exc
        if not isinstance(result, dict):
            raise QualificationError("Backblaze API returned a non-object response")
        return cast(dict[str, Any], result)

    def authorize(self) -> None:
        basic = base64.b64encode(f"{self.key_id}:{self.application_key}".encode()).decode("ascii")
        payload = self._request(
            "https://api.backblazeb2.com/b2api/v4/b2_authorize_account",
            authorization=f"Basic {basic}",
        )
        storage = payload.get("apiInfo", {}).get("storageApi", {})
        allowed = storage.get("allowed", {}) if isinstance(storage, dict) else {}
        capabilities = set(allowed.get("capabilities", [])) if isinstance(allowed, dict) else set()
        required = {
            "listBuckets",
            "writeBuckets",
            "readBucketEncryption",
            "writeBucketEncryption",
            "readBucketRetentions",
        }
        missing = sorted(required - capabilities)
        if missing:
            raise QualificationError(
                "Backblaze provisioning key lacks capabilities: " + ", ".join(missing)
            )
        self.account_id = str(payload.get("accountId", ""))
        self.api_url = str(storage.get("apiUrl", "")) if isinstance(storage, dict) else ""
        self.authorization_token = str(payload.get("authorizationToken", ""))
        if not self.account_id or not self.api_url or not self.authorization_token:
            raise QualificationError("Backblaze authorization response is incomplete")

    def call(self, operation: str, payload: Mapping[str, object]) -> dict[str, Any]:
        if not self.authorization_token:
            self.authorize()
        return self._request(
            f"{self.api_url}/b2api/v4/{operation}",
            authorization=self.authorization_token,
            payload=payload,
        )


class B2BucketManager:
    def __init__(self, client: B2NativeClient) -> None:
        self.client = client

    def _bucket(self, bucket: ResolvedBucket) -> dict[str, Any] | None:
        result = self.client.call(
            "b2_list_buckets",
            {"accountId": self.client.account_id, "bucketName": bucket.bucket_name},
        )
        buckets = result.get("buckets", [])
        if not buckets:
            return None
        if len(buckets) != 1 or not isinstance(buckets[0], dict):
            raise QualificationError(
                f"Backblaze returned ambiguous state for {bucket.logical_name}"
            )
        return cast(dict[str, Any], buckets[0])

    @staticmethod
    def _owned(current: Mapping[str, object], bucket: ResolvedBucket) -> bool:
        info = current.get("bucketInfo")
        return info == {
            "riverhog-purpose": QUALIFICATION_MARKER,
            "riverhog-logical-name": bucket.logical_name,
        }

    def _changes(
        self,
        current: Mapping[str, object],
        bucket: ResolvedBucket,
        config: QualificationConfig,
    ) -> tuple[str, ...]:
        if not self._owned(current, bucket):
            return ("ownership-marker",)
        changes: list[str] = []
        if current.get("bucketType") != "allPrivate":
            changes.append("private-access")
        if current.get("corsRules") != []:
            changes.append("cors")
        if current.get("lifecycleRules") != _b2_lifecycle(config):
            changes.append("lifecycle")
        encryption = current.get("defaultServerSideEncryption")
        value = encryption.get("value") if isinstance(encryption, dict) else None
        if value != {"algorithm": "AES256", "mode": "SSE-B2"}:
            changes.append("default-encryption")
        lock = current.get("fileLockConfiguration")
        if not isinstance(lock, dict) or lock.get("isClientAuthorizedToRead") is not True:
            changes.append("object-lock-visibility")
        elif not isinstance(lock.get("value"), dict):
            changes.append("object-lock-visibility")
        elif cast(dict[str, object], lock["value"]).get("isFileLockEnabled") is True:
            changes.append("object-lock")
        return tuple(changes)

    def plan(self, bucket: ResolvedBucket, config: QualificationConfig) -> InfrastructureAction:
        current = self._bucket(bucket)
        if current is None:
            return InfrastructureAction(bucket.logical_name, "b2", "create", ("bucket",))
        changes = self._changes(current, bucket, config)
        if any(
            change in changes
            for change in ("ownership-marker", "object-lock", "object-lock-visibility")
        ):
            return InfrastructureAction(bucket.logical_name, "b2", "blocked", changes)
        return InfrastructureAction(
            bucket.logical_name,
            "b2",
            "update" if changes else "ready",
            changes,
        )

    def apply(self, bucket: ResolvedBucket, config: QualificationConfig) -> None:
        current = self._bucket(bucket)
        if current is not None and not self._owned(current, bucket):
            raise QualificationError(
                f"{bucket.logical_name} exists without its exact qualification ownership marker"
            )
        common: dict[str, object] = {
            "bucketType": "allPrivate",
            "bucketInfo": {
                "riverhog-purpose": QUALIFICATION_MARKER,
                "riverhog-logical-name": bucket.logical_name,
            },
            "corsRules": [],
            "lifecycleRules": _b2_lifecycle(config),
            "defaultServerSideEncryption": {"algorithm": "AES256", "mode": "SSE-B2"},
        }
        if current is None:
            self.client.call(
                "b2_create_bucket",
                {
                    "accountId": self.client.account_id,
                    "bucketName": bucket.bucket_name,
                    "fileLockEnabled": False,
                    **common,
                },
            )
        else:
            lock = current.get("fileLockConfiguration")
            lock_value = lock.get("value") if isinstance(lock, dict) else None
            if isinstance(lock_value, dict) and lock_value.get("isFileLockEnabled") is True:
                raise QualificationError(
                    f"{bucket.logical_name} has irreversible Object Lock enabled"
                )
            self.client.call(
                "b2_update_bucket",
                {
                    "accountId": self.client.account_id,
                    "bucketId": current["bucketId"],
                    "ifRevisionIs": current["revision"],
                    **common,
                },
            )
        final = self.plan(bucket, config)
        if final.action != "ready":
            raise QualificationError(f"{bucket.logical_name} did not converge after apply")


class CloudFrontManager:
    """Reconcile one dedicated private CloudFront egress path without exposing its identity."""

    _LOGICAL_NAME = "aws-cloudfront-egress"
    _ORIGIN_ID = "aws-deep-archive"
    _POLICY_SID = "RiverhogQualificationCloudFrontRead"

    def __init__(
        self,
        *,
        cloudfront_client: object,
        s3_client: object,
        bucket: ResolvedBucket,
        config: QualificationConfig,
        public_key_pem: str,
    ) -> None:
        self.cloudfront = cloudfront_client
        self.s3 = s3_client
        self.bucket = bucket
        self.config = config
        self.public_key_pem = public_key_pem.strip() + "\n"
        suffix = hashlib.sha256(bucket.bucket_name.encode()).hexdigest()[:16]
        self.marker = f"{QUALIFICATION_MARKER}:{suffix}"
        self.resource_name = f"riverhog-qualification-{suffix}"

    @staticmethod
    def _call(client: object, name: str, **kwargs: object) -> dict[str, Any]:
        return cast(dict[str, Any], getattr(client, name)(**kwargs))

    def _cloudfront_call(self, name: str, **kwargs: object) -> dict[str, Any]:
        try:
            return self._call(self.cloudfront, name, **kwargs)
        except Exception as exc:
            raise QualificationError(f"cannot inspect CloudFront {name.replace('_', ' ')}") from exc

    def _list(self, operation: str, container_name: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        marker: str | None = None
        while True:
            kwargs: dict[str, object] = {}
            if marker is not None:
                kwargs["Marker"] = marker
            response = self._cloudfront_call(operation, **kwargs)
            container = response.get(container_name, {})
            if not isinstance(container, dict):
                raise QualificationError(f"CloudFront {operation} returned invalid pagination")
            items.extend(
                cast(dict[str, Any], item)
                for item in container.get("Items", [])
                if isinstance(item, dict)
            )
            if not container.get("IsTruncated"):
                return items
            marker = str(container.get("NextMarker", ""))
            if not marker:
                raise QualificationError(f"CloudFront {operation} omitted its next marker")

    def _public_key(self) -> dict[str, Any] | None:
        for item in self._list("list_public_keys", "PublicKeyList"):
            current = item.get("PublicKeyConfig", item)
            if isinstance(current, dict) and current.get("Comment") == self.marker:
                return item
        return None

    def _key_group(self) -> dict[str, Any] | None:
        for item in self._list("list_key_groups", "KeyGroupList"):
            current = item.get("KeyGroupConfig", item)
            if isinstance(current, dict) and current.get("Comment") == self.marker:
                return item
        return None

    def _origin_access_control(self) -> dict[str, Any] | None:
        for item in self._list("list_origin_access_controls", "OriginAccessControlList"):
            current = item.get("OriginAccessControlConfig", item)
            if isinstance(current, dict) and current.get("Description") == self.marker:
                return item
        return None

    def _distribution(self) -> dict[str, Any] | None:
        for item in self._list("list_distributions", "DistributionList"):
            if item.get("Comment") == self.marker:
                return item
        return None

    @staticmethod
    def _item_id(item: Mapping[str, object] | None) -> str | None:
        value = item.get("Id") if item is not None else None
        return str(value) if value else None

    def _public_key_config(self, public_key_id: str) -> dict[str, Any]:
        response = self._cloudfront_call("get_public_key_config", Id=public_key_id)
        config = response.get("PublicKeyConfig")
        if not isinstance(config, dict):
            raise QualificationError("CloudFront public key configuration is invalid")
        return cast(dict[str, Any], config)

    def _key_group_config(self, key_group_id: str) -> dict[str, Any]:
        response = self._cloudfront_call("get_key_group_config", Id=key_group_id)
        config = response.get("KeyGroupConfig")
        if not isinstance(config, dict):
            raise QualificationError("CloudFront key group configuration is invalid")
        return cast(dict[str, Any], config)

    def _oac_config(self, oac_id: str) -> dict[str, Any]:
        response = self._cloudfront_call("get_origin_access_control_config", Id=oac_id)
        config = response.get("OriginAccessControlConfig")
        if not isinstance(config, dict):
            raise QualificationError("CloudFront origin access control configuration is invalid")
        return cast(dict[str, Any], config)

    def _distribution_config(self, distribution_id: str) -> dict[str, Any]:
        response = self._cloudfront_call("get_distribution_config", Id=distribution_id)
        config = response.get("DistributionConfig")
        if not isinstance(config, dict):
            raise QualificationError("CloudFront distribution configuration is invalid")
        return cast(dict[str, Any], config)

    def _desired_oac(self) -> dict[str, object]:
        return {
            "Name": self.resource_name,
            "Description": self.marker,
            "SigningProtocol": "sigv4",
            "SigningBehavior": "always",
            "OriginAccessControlOriginType": "s3",
        }

    def _desired_key_group(self, public_key_id: str) -> dict[str, object]:
        return {
            "Name": self.resource_name,
            "Items": [public_key_id],
            "Comment": self.marker,
        }

    def _desired_distribution(
        self,
        *,
        caller_reference: str,
        oac_id: str,
        key_group_id: str,
    ) -> dict[str, object]:
        origin_domain = (
            f"{self.bucket.bucket_name}.s3.amazonaws.com"
            if self.bucket.region == "us-east-1"
            else f"{self.bucket.bucket_name}.s3.{self.bucket.region}.amazonaws.com"
        )
        return {
            "CallerReference": caller_reference,
            "Comment": self.marker,
            "Enabled": True,
            "Origins": {
                "Quantity": 1,
                "Items": [
                    {
                        "Id": self._ORIGIN_ID,
                        "DomainName": origin_domain,
                        "OriginAccessControlId": oac_id,
                        "S3OriginConfig": {"OriginAccessIdentity": ""},
                        "ConnectionAttempts": 3,
                        "ConnectionTimeout": 10,
                        "OriginShield": {"Enabled": False},
                    }
                ],
            },
            "DefaultCacheBehavior": {
                "TargetOriginId": self._ORIGIN_ID,
                "ViewerProtocolPolicy": "https-only",
                "TrustedSigners": {"Enabled": False, "Quantity": 0},
                "TrustedKeyGroups": {
                    "Enabled": True,
                    "Quantity": 1,
                    "Items": [key_group_id],
                },
                "AllowedMethods": {
                    "Quantity": 2,
                    "Items": ["HEAD", "GET"],
                    "CachedMethods": {"Quantity": 2, "Items": ["HEAD", "GET"]},
                },
                "Compress": False,
                "ForwardedValues": {
                    "QueryString": True,
                    "Cookies": {"Forward": "none"},
                    "Headers": {"Quantity": 0},
                    "QueryStringCacheKeys": {"Quantity": 1, "Items": ["versionId"]},
                },
                "MinTTL": 0,
                "DefaultTTL": 86400,
                "MaxTTL": 604800,
            },
            "PriceClass": "PriceClass_100",
            "ViewerCertificate": {"CloudFrontDefaultCertificate": True},
            "Restrictions": {"GeoRestriction": {"RestrictionType": "none", "Quantity": 0}},
            "HttpVersion": "http2and3",
            "IsIPV6Enabled": True,
        }

    @staticmethod
    def _normalize_distribution(config: Mapping[str, object]) -> dict[str, object]:
        origins = config.get("Origins")
        origin_items = origins.get("Items", []) if isinstance(origins, dict) else []
        origin = (
            origin_items[0] if len(origin_items) == 1 and isinstance(origin_items[0], dict) else {}
        )
        behavior = config.get("DefaultCacheBehavior")
        behavior = behavior if isinstance(behavior, dict) else {}
        trusted = behavior.get("TrustedKeyGroups")
        trusted = trusted if isinstance(trusted, dict) else {}
        trusted_signers = behavior.get("TrustedSigners")
        trusted_signers = trusted_signers if isinstance(trusted_signers, dict) else {}
        allowed = behavior.get("AllowedMethods")
        allowed = allowed if isinstance(allowed, dict) else {}
        cached = allowed.get("CachedMethods")
        cached = cached if isinstance(cached, dict) else {}
        forwarded = behavior.get("ForwardedValues")
        forwarded = forwarded if isinstance(forwarded, dict) else {}
        cookies = forwarded.get("Cookies")
        cookies = cookies if isinstance(cookies, dict) else {}
        headers = forwarded.get("Headers")
        headers = headers if isinstance(headers, dict) else {}
        query_keys = forwarded.get("QueryStringCacheKeys")
        query_keys = query_keys if isinstance(query_keys, dict) else {}
        origin_shield = origin.get("OriginShield")
        origin_shield = origin_shield if isinstance(origin_shield, dict) else {}
        viewer = config.get("ViewerCertificate")
        viewer = viewer if isinstance(viewer, dict) else {}
        restrictions = config.get("Restrictions")
        restrictions = restrictions if isinstance(restrictions, dict) else {}
        geo = restrictions.get("GeoRestriction")
        geo = geo if isinstance(geo, dict) else {}
        return {
            "Comment": config.get("Comment"),
            "Enabled": config.get("Enabled"),
            "PriceClass": config.get("PriceClass"),
            "HttpVersion": config.get("HttpVersion"),
            "IsIPV6Enabled": config.get("IsIPV6Enabled"),
            "Origin": {
                "Id": origin.get("Id"),
                "DomainName": origin.get("DomainName"),
                "OriginAccessControlId": origin.get("OriginAccessControlId"),
                "S3OriginConfig": origin.get("S3OriginConfig"),
                "ConnectionAttempts": origin.get("ConnectionAttempts"),
                "ConnectionTimeout": origin.get("ConnectionTimeout"),
                "OriginShield": {"Enabled": origin_shield.get("Enabled")},
            },
            "Behavior": {
                "TargetOriginId": behavior.get("TargetOriginId"),
                "ViewerProtocolPolicy": behavior.get("ViewerProtocolPolicy"),
                "TrustedSigners": {
                    "Enabled": trusted_signers.get("Enabled"),
                    "Items": trusted_signers.get("Items", []),
                },
                "TrustedKeyGroups": trusted.get("Items", []),
                "AllowedMethods": allowed.get("Items", []),
                "CachedMethods": cached.get("Items", []),
                "Compress": behavior.get("Compress"),
                "QueryString": forwarded.get("QueryString"),
                "Cookies": cookies.get("Forward"),
                "Headers": headers.get("Items", []),
                "QueryStringCacheKeys": query_keys.get("Items", []),
                "MinTTL": behavior.get("MinTTL"),
                "DefaultTTL": behavior.get("DefaultTTL"),
                "MaxTTL": behavior.get("MaxTTL"),
            },
            "ViewerCertificate": {
                "CloudFrontDefaultCertificate": viewer.get("CloudFrontDefaultCertificate")
            },
            "Restrictions": {
                "GeoRestriction": {
                    "RestrictionType": geo.get("RestrictionType"),
                    "Quantity": geo.get("Quantity"),
                    "Items": geo.get("Items", []),
                }
            },
        }

    def _distribution_arn(self, distribution_id: str) -> str:
        response = self._cloudfront_call("get_distribution", Id=distribution_id)
        distribution = response.get("Distribution")
        arn = distribution.get("ARN") if isinstance(distribution, dict) else None
        if not arn:
            raise QualificationError("CloudFront distribution ARN is missing")
        return str(arn)

    def _bucket_policy_statement(self, distribution_arn: str) -> dict[str, object]:
        return {
            "Sid": self._POLICY_SID,
            "Effect": "Allow",
            "Principal": {"Service": "cloudfront.amazonaws.com"},
            "Action": "s3:GetObject",
            "Resource": (
                f"arn:aws:s3:::{self.bucket.bucket_name}/{self.config.namespace_prefix}/*"
            ),
            "Condition": {"StringEquals": {"AWS:SourceArn": distribution_arn}},
        }

    def _bucket_policy(self) -> dict[str, object]:
        try:
            response = self._call(self.s3, "get_bucket_policy", Bucket=self.bucket.bucket_name)
        except Exception as exc:
            if _client_error_code(exc) in {"NoSuchBucketPolicy", "NoSuchPolicy", "404"}:
                return {"Version": "2012-10-17", "Statement": []}
            raise QualificationError("cannot inspect CloudFront origin bucket policy") from exc
        try:
            payload = json.loads(response["Policy"])
        except (KeyError, TypeError, ValueError) as exc:
            raise QualificationError("CloudFront origin bucket policy is invalid") from exc
        if not isinstance(payload, dict):
            raise QualificationError("CloudFront origin bucket policy is invalid")
        return cast(dict[str, object], payload)

    def _bucket_policy_ready(self, distribution_arn: str) -> bool:
        policy = self._bucket_policy()
        statements = policy.get("Statement", [])
        return (
            isinstance(statements, list)
            and self._bucket_policy_statement(distribution_arn) in statements
        )

    def _bucket_policy_has_unmanaged_statements(self) -> bool:
        statements = self._bucket_policy().get("Statement", [])
        return not isinstance(statements, list) or any(
            not isinstance(item, dict) or item.get("Sid") != self._POLICY_SID for item in statements
        )

    def plan(self) -> InfrastructureAction:
        if self._bucket_policy_has_unmanaged_statements():
            return InfrastructureAction(
                self._LOGICAL_NAME,
                "aws",
                "blocked",
                ("unmanaged-origin-policy",),
            )
        public_key = self._public_key()
        key_group = self._key_group()
        oac = self._origin_access_control()
        distribution = self._distribution()
        missing = tuple(
            name
            for name, value in (
                ("public-key", public_key),
                ("key-group", key_group),
                ("origin-access-control", oac),
                ("distribution", distribution),
            )
            if value is None
        )
        if missing:
            return InfrastructureAction(self._LOGICAL_NAME, "aws", "create", missing)
        assert distribution is not None
        public_key_id = cast(str, self._item_id(public_key))
        key_group_id = cast(str, self._item_id(key_group))
        oac_id = cast(str, self._item_id(oac))
        distribution_id = cast(str, self._item_id(distribution))
        if not all((public_key_id, key_group_id, oac_id, distribution_id)):
            return InfrastructureAction(
                self._LOGICAL_NAME, "aws", "blocked", ("resource-identity",)
            )
        changes: list[str] = []
        public_config = self._public_key_config(public_key_id)
        if public_config.get("EncodedKey", "").strip() != self.public_key_pem.strip():
            changes.append("public-key")
        if self._key_group_config(key_group_id).get("Items") != [public_key_id]:
            changes.append("key-group")
        if self._oac_config(oac_id) != self._desired_oac():
            changes.append("origin-access-control")
        actual_distribution = self._distribution_config(distribution_id)
        desired_distribution = self._desired_distribution(
            caller_reference=str(actual_distribution.get("CallerReference", "")),
            oac_id=oac_id,
            key_group_id=key_group_id,
        )
        if self._normalize_distribution(actual_distribution) != self._normalize_distribution(
            desired_distribution
        ):
            changes.append("distribution")
        distribution_arn = self._distribution_arn(distribution_id)
        if not self._bucket_policy_ready(distribution_arn):
            changes.append("origin-policy")
        status = distribution.get("Status")
        if status != "Deployed":
            changes.append("deployment")
        return InfrastructureAction(
            self._LOGICAL_NAME,
            "aws",
            "update" if changes else "ready",
            tuple(changes),
        )

    def _ensure_public_key(self) -> str:
        current = self._public_key()
        desired = {
            "CallerReference": self.marker,
            "Name": self.resource_name,
            "EncodedKey": self.public_key_pem,
            "Comment": self.marker,
        }
        if current is None:
            response = self._cloudfront_call("create_public_key", PublicKeyConfig=desired)
            created = response.get("PublicKey")
            if not isinstance(created, dict) or not created.get("Id"):
                raise QualificationError("CloudFront did not return the created public key")
            return str(created["Id"])
        public_key_id = self._item_id(current)
        if public_key_id is None:
            raise QualificationError("CloudFront public key identity is missing")
        response = self._cloudfront_call("get_public_key_config", Id=public_key_id)
        config = response.get("PublicKeyConfig")
        if not isinstance(config, dict):
            raise QualificationError("CloudFront public key configuration is invalid")
        desired["CallerReference"] = str(config.get("CallerReference", self.marker))
        if config != desired:
            self._cloudfront_call(
                "update_public_key",
                Id=public_key_id,
                IfMatch=str(response.get("ETag", "")),
                PublicKeyConfig=desired,
            )
        return public_key_id

    def _ensure_key_group(self, public_key_id: str) -> str:
        current = self._key_group()
        desired = self._desired_key_group(public_key_id)
        if current is None:
            response = self._cloudfront_call("create_key_group", KeyGroupConfig=desired)
            created = response.get("KeyGroup")
            if not isinstance(created, dict) or not created.get("Id"):
                raise QualificationError("CloudFront did not return the created key group")
            return str(created["Id"])
        key_group_id = self._item_id(current)
        if key_group_id is None:
            raise QualificationError("CloudFront key group identity is missing")
        response = self._cloudfront_call("get_key_group_config", Id=key_group_id)
        if response.get("KeyGroupConfig") != desired:
            self._cloudfront_call(
                "update_key_group",
                Id=key_group_id,
                IfMatch=str(response.get("ETag", "")),
                KeyGroupConfig=desired,
            )
        return key_group_id

    def _ensure_oac(self) -> str:
        current = self._origin_access_control()
        desired = self._desired_oac()
        if current is None:
            response = self._cloudfront_call(
                "create_origin_access_control",
                OriginAccessControlConfig=desired,
            )
            created = response.get("OriginAccessControl")
            if not isinstance(created, dict) or not created.get("Id"):
                raise QualificationError("CloudFront did not return the created OAC")
            return str(created["Id"])
        oac_id = self._item_id(current)
        if oac_id is None:
            raise QualificationError("CloudFront OAC identity is missing")
        response = self._cloudfront_call("get_origin_access_control_config", Id=oac_id)
        if response.get("OriginAccessControlConfig") != desired:
            self._cloudfront_call(
                "update_origin_access_control",
                Id=oac_id,
                IfMatch=str(response.get("ETag", "")),
                OriginAccessControlConfig=desired,
            )
        return oac_id

    def _ensure_distribution(self, *, oac_id: str, key_group_id: str) -> str:
        current = self._distribution()
        if current is None:
            desired = self._desired_distribution(
                caller_reference=self.marker,
                oac_id=oac_id,
                key_group_id=key_group_id,
            )
            response = self._cloudfront_call("create_distribution", DistributionConfig=desired)
            created = response.get("Distribution")
            if not isinstance(created, dict) or not created.get("Id"):
                raise QualificationError("CloudFront did not return the created distribution")
            return str(created["Id"])
        distribution_id = self._item_id(current)
        if distribution_id is None:
            raise QualificationError("CloudFront distribution identity is missing")
        response = self._cloudfront_call("get_distribution_config", Id=distribution_id)
        actual = response.get("DistributionConfig")
        if not isinstance(actual, dict):
            raise QualificationError("CloudFront distribution configuration is invalid")
        desired = self._desired_distribution(
            caller_reference=str(actual.get("CallerReference", self.marker)),
            oac_id=oac_id,
            key_group_id=key_group_id,
        )
        if self._normalize_distribution(actual) != self._normalize_distribution(desired):
            self._cloudfront_call(
                "update_distribution",
                Id=distribution_id,
                IfMatch=str(response.get("ETag", "")),
                DistributionConfig=desired,
            )
        return distribution_id

    def _ensure_bucket_policy(self, distribution_arn: str) -> None:
        policy = self._bucket_policy()
        raw_statements = policy.get("Statement", [])
        statements = (
            [item for item in raw_statements if isinstance(item, dict)]
            if isinstance(raw_statements, list)
            else []
        )
        statements = [item for item in statements if item.get("Sid") != self._POLICY_SID]
        statements.append(self._bucket_policy_statement(distribution_arn))
        self._call(
            self.s3,
            "put_bucket_policy",
            Bucket=self.bucket.bucket_name,
            Policy=json.dumps(
                {"Version": "2012-10-17", "Statement": statements},
                sort_keys=True,
                separators=(",", ":"),
            ),
        )

    def _wait_for_distribution(self, distribution_id: str) -> None:
        get_waiter = getattr(self.cloudfront, "get_waiter", None)
        if not callable(get_waiter):
            return
        waiter = get_waiter("distribution_deployed")
        waiter.wait(
            Id=distribution_id,
            WaiterConfig={"Delay": 15, "MaxAttempts": 160},
        )

    def apply(self) -> None:
        public_key_id = self._ensure_public_key()
        key_group_id = self._ensure_key_group(public_key_id)
        oac_id = self._ensure_oac()
        distribution_id = self._ensure_distribution(
            oac_id=oac_id,
            key_group_id=key_group_id,
        )
        self._ensure_bucket_policy(self._distribution_arn(distribution_id))
        self._wait_for_distribution(distribution_id)
        if self.plan().action != "ready":
            raise QualificationError("CloudFront egress did not converge after apply")

    def runtime_configuration(self) -> tuple[str, str]:
        if self.plan().action != "ready":
            raise QualificationError("CloudFront egress must be ready before deployment")
        public_key_id = self._item_id(self._public_key())
        distribution_id = self._item_id(self._distribution())
        if public_key_id is None or distribution_id is None:
            raise QualificationError("CloudFront runtime identity is incomplete")
        response = self._cloudfront_call("get_distribution", Id=distribution_id)
        distribution = response.get("Distribution")
        domain = distribution.get("DomainName") if isinstance(distribution, dict) else None
        if not isinstance(domain, str) or not domain:
            raise QualificationError("CloudFront distribution domain is missing")
        return f"https://{domain}", public_key_id


def infrastructure_plan(
    config: QualificationConfig,
    buckets: Sequence[ResolvedBucket],
    managers: Mapping[str, BucketManager],
    additional: Sequence[AdditionalInfrastructureManager] = (),
) -> InfrastructurePlan:
    actions = (
        *(
            managers[bucket.provider].plan(bucket, config)
            for bucket in sorted(buckets, key=lambda item: item.logical_name)
        ),
        *(manager.plan() for manager in additional),
    )
    return InfrastructurePlan(config_sha256=config.config_sha256, actions=actions)


def apply_infrastructure(
    config: QualificationConfig,
    buckets: Sequence[ResolvedBucket],
    managers: Mapping[str, BucketManager],
    additional: Sequence[AdditionalInfrastructureManager] = (),
) -> InfrastructurePlan:
    initial = infrastructure_plan(config, buckets, managers, additional)
    if initial.blocked:
        blocked = ", ".join(
            action.logical_name for action in initial.actions if action.action == "blocked"
        )
        raise QualificationError(f"qualification infrastructure is blocked: {blocked}")
    for bucket in sorted(buckets, key=lambda item: item.logical_name):
        action = managers[bucket.provider].plan(bucket, config)
        if action.action != "ready":
            managers[bucket.provider].apply(bucket, config)
    for manager in additional:
        if manager.plan().action != "ready":
            manager.apply()
    final = infrastructure_plan(config, buckets, managers, additional)
    if not final.ready:
        raise QualificationError("qualification infrastructure did not converge")
    return final


def _stream_pattern(path: Path, byte_count: int, *, seed: str) -> str:
    digest = hashlib.sha256()
    block = hashlib.sha256(seed.encode("utf-8")).digest() * 4096
    remaining = byte_count
    with path.open("xb") as handle:
        while remaining:
            chunk = block[: min(len(block), remaining)]
            handle.write(chunk)
            digest.update(chunk)
            remaining -= len(chunk)
    return digest.hexdigest()


def _corpus_layout(profile: str) -> tuple[tuple[str, int], ...]:
    common = (
        ("empty.txt", 0),
        ("packed/readme.txt", len(b"Riverhog provider qualification\n")),
        (f"packed/{'long-path-' * 12}member.txt", 32 * 1024),
        ("direct/deterministic.bin", 16 * MIB + 1),
    )
    if profile == "regular":
        return common
    if profile == "multipart":
        return (*common, ("direct/multipart-boundary.bin", 128 * MIB + 64 * 1024))
    raise QualificationError("corpus profile must be regular or multipart")


def create_corpus(output: Path, *, profile: str) -> CorpusManifest:
    output = output.expanduser().resolve()
    manifest_path = corpus_manifest_path(output)
    if output.exists():
        raise QualificationError("corpus output must not already exist")
    if manifest_path.exists():
        raise QualificationError("corpus manifest output must not already exist")
    output.mkdir(parents=True)
    files: list[CorpusFile] = []
    try:
        for relative, byte_count in _corpus_layout(profile):
            path = output / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if relative == "packed/readme.txt":
                content = b"Riverhog provider qualification\n"
                path.write_bytes(content)
                sha256 = hashlib.sha256(content).hexdigest()
            else:
                sha256 = _stream_pattern(path, byte_count, seed=f"{profile}:{relative}")
            files.append(CorpusFile(path=relative, bytes=byte_count, sha256=sha256))
        identity_payload = [asdict(item) for item in sorted(files, key=lambda item: item.path)]
        identity = hashlib.sha256(_canonical_json(identity_payload)).hexdigest()
        manifest = CorpusManifest(
            profile=profile,
            files=tuple(sorted(files, key=lambda item: item.path)),
            bytes=sum(item.bytes for item in files),
            sha256=identity,
        )
        manifest_path.write_bytes(_canonical_json(manifest.as_dict()) + b"\n")
        return manifest
    except BaseException:
        import shutil

        shutil.rmtree(output, ignore_errors=True)
        manifest_path.unlink(missing_ok=True)
        raise


def corpus_manifest_path(output: Path) -> Path:
    output = output.expanduser().resolve()
    return output.with_name(f"{output.name}.qualification.json")


def load_corpus_manifest(path: Path) -> CorpusManifest:
    try:
        payload = json.loads(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise QualificationError(f"cannot load corpus manifest: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema") != CORPUS_SCHEMA:
        raise QualificationError(f"corpus manifest schema must be {CORPUS_SCHEMA}")
    raw_files = payload.get("files")
    if not isinstance(raw_files, list):
        raise QualificationError("corpus manifest files must be a list")
    files = tuple(
        CorpusFile(path=str(item["path"]), bytes=int(item["bytes"]), sha256=str(item["sha256"]))
        for item in raw_files
        if isinstance(item, dict)
    )
    identity = hashlib.sha256(_canonical_json([asdict(item) for item in files])).hexdigest()
    if identity != payload.get("sha256") or any(
        item.bytes < 0 or _SHA256_RE.fullmatch(item.sha256) is None for item in files
    ):
        raise QualificationError("corpus manifest identity is invalid")
    return CorpusManifest(
        profile=str(payload.get("profile")),
        files=files,
        bytes=int(payload.get("bytes", -1)),
        sha256=identity,
    )


def _checkpoint_digest(checkpoint: QualificationCheckpoint) -> str:
    return hashlib.sha256(_canonical_json(checkpoint.as_dict(include_digest=False))).hexdigest()


def new_checkpoint(
    *,
    source_sha: str,
    source_ref: str,
    config: QualificationConfig,
    corpus: CorpusManifest,
    buckets: Sequence[ResolvedBucket],
    run_id: str | None = None,
    now: datetime | None = None,
) -> QualificationCheckpoint:
    if _SOURCE_SHA_RE.fullmatch(source_sha) is None:
        raise QualificationError("source_sha must be an exact lowercase 40-character commit")
    resolved_run_id = run_id or uuid.uuid4().hex
    try:
        uuid.UUID(hex=resolved_run_id)
    except ValueError as exc:
        raise QualificationError("run_id must be a UUID hex value") from exc
    current = now or _utc_now()
    timestamp = _timestamp(current)
    providers = _provider_contracts(buckets)
    if {item.logical_name for item in providers} != set(_EXPECTED_ROLES):
        raise QualificationError("checkpoint requires all qualification provider roles")
    checkpoint = QualificationCheckpoint(
        run_id=resolved_run_id,
        source_sha=source_sha,
        source_ref=source_ref,
        config_sha256=config.config_sha256,
        corpus_sha256=corpus.sha256,
        corpus_profile=corpus.profile,
        corpus_files=len(corpus.files),
        corpus_bytes=corpus.bytes,
        restore_tier=config.restore_tier,
        restore_copy_days=config.restore_copy_days,
        provider_binding_sha256=_provider_binding_sha256(buckets),
        providers=providers,
        artifacts=(),
        namespace=f"{config.namespace_prefix}/{resolved_run_id}",
        phase="created",
        generation=0,
        previous_checkpoint_sha256=None,
        started_at=timestamp,
        updated_at=timestamp,
        restore_deadline_at=_timestamp(current + timedelta(hours=config.restore_deadline_hours)),
        collection_id=None,
        retrieval_job_id=None,
        history=(PhaseRecord(phase="created", at=timestamp, assertions=()),),
        checkpoint_sha256="",
    )
    return replace(checkpoint, checkpoint_sha256=_checkpoint_digest(checkpoint))


def advance_checkpoint(
    checkpoint: QualificationCheckpoint,
    *,
    phase: str,
    assertions: Sequence[str] = (),
    collection_id: int | None = None,
    retrieval_job_id: str | None = None,
    artifacts: Sequence[ArtifactIdentity] = (),
    now: datetime | None = None,
) -> QualificationCheckpoint:
    if _checkpoint_digest(checkpoint) != checkpoint.checkpoint_sha256:
        raise QualificationError("checkpoint digest does not match its contents")
    if phase not in _TRANSITIONS.get(checkpoint.phase, set()):
        raise QualificationError(f"checkpoint cannot advance from {checkpoint.phase} to {phase}")
    if any(not value or len(value) > 120 for value in assertions):
        raise QualificationError("checkpoint assertions must be concise non-empty identifiers")
    timestamp = _timestamp(now or _utc_now())
    merged_artifacts = {item.surface: item for item in checkpoint.artifacts}
    for artifact in artifacts:
        if _SHA256_RE.fullmatch(artifact.sha256) is None:
            raise QualificationError("artifact identity must be an exact SHA-256")
        if artifact.objects <= 0 or artifact.bytes < 0:
            raise QualificationError("artifact identity counts are invalid")
        existing = merged_artifacts.get(artifact.surface)
        if existing is not None and existing != artifact:
            raise QualificationError("artifact identity cannot change")
        merged_artifacts[artifact.surface] = artifact
    updated = replace(
        checkpoint,
        phase=phase,
        generation=checkpoint.generation + 1,
        previous_checkpoint_sha256=checkpoint.checkpoint_sha256,
        updated_at=timestamp,
        collection_id=collection_id if collection_id is not None else checkpoint.collection_id,
        retrieval_job_id=(
            retrieval_job_id if retrieval_job_id is not None else checkpoint.retrieval_job_id
        ),
        artifacts=tuple(sorted(merged_artifacts.values(), key=lambda item: item.surface)),
        history=(
            *checkpoint.history,
            PhaseRecord(phase=phase, at=timestamp, assertions=tuple(sorted(set(assertions)))),
        ),
        checkpoint_sha256="",
    )
    return replace(updated, checkpoint_sha256=_checkpoint_digest(updated))


def write_checkpoint(path: Path, checkpoint: QualificationCheckpoint) -> None:
    if _checkpoint_digest(checkpoint) != checkpoint.checkpoint_sha256:
        raise QualificationError("refusing to write a checkpoint with an invalid digest")
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_canonical_json(checkpoint.as_dict()) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def load_checkpoint(path: Path) -> QualificationCheckpoint:
    try:
        payload = json.loads(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise QualificationError(f"cannot load qualification checkpoint: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema") != CHECKPOINT_SCHEMA:
        raise QualificationError(f"checkpoint schema must be {CHECKPOINT_SCHEMA}")
    try:
        history = tuple(
            PhaseRecord(
                phase=str(item["phase"]),
                at=str(item["at"]),
                assertions=tuple(str(value) for value in item.get("assertions", [])),
            )
            for item in payload["history"]
        )
        checkpoint = QualificationCheckpoint(
            run_id=str(payload["run_id"]),
            source_sha=str(payload["source_sha"]),
            source_ref=str(payload["source_ref"]),
            config_sha256=str(payload["config_sha256"]),
            corpus_sha256=str(payload["corpus_sha256"]),
            corpus_profile=str(payload["corpus_profile"]),
            corpus_files=int(payload["corpus_files"]),
            corpus_bytes=int(payload["corpus_bytes"]),
            restore_tier=str(payload["restore_tier"]),
            restore_copy_days=int(payload["restore_copy_days"]),
            provider_binding_sha256=str(payload["provider_binding_sha256"]),
            providers=tuple(
                ProviderContract(
                    logical_name=str(item["logical_name"]),
                    provider=str(item["provider"]),
                    role=str(item["role"]),
                    region=str(item["region"]),
                    storage_class=str(item["storage_class"]),
                    read_mode=str(item["read_mode"]),
                )
                for item in payload["providers"]
            ),
            artifacts=tuple(
                ArtifactIdentity(
                    surface=str(item["surface"]),
                    sha256=str(item["sha256"]),
                    objects=int(item["objects"]),
                    bytes=int(item["bytes"]),
                )
                for item in payload["artifacts"]
            ),
            namespace=str(payload["namespace"]),
            phase=str(payload["phase"]),
            generation=int(payload["generation"]),
            previous_checkpoint_sha256=(
                str(payload["previous_checkpoint_sha256"])
                if payload["previous_checkpoint_sha256"] is not None
                else None
            ),
            started_at=str(payload["started_at"]),
            updated_at=str(payload["updated_at"]),
            restore_deadline_at=str(payload["restore_deadline_at"]),
            collection_id=(
                int(payload["collection_id"]) if payload["collection_id"] is not None else None
            ),
            retrieval_job_id=(
                str(payload["retrieval_job_id"])
                if payload["retrieval_job_id"] is not None
                else None
            ),
            history=history,
            checkpoint_sha256=str(payload["checkpoint_sha256"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise QualificationError("checkpoint shape is invalid") from exc
    if checkpoint.phase not in _PHASES or not history or history[-1].phase != checkpoint.phase:
        raise QualificationError("checkpoint phase history is invalid")
    if checkpoint.restore_tier != "bulk" or checkpoint.restore_copy_days < 1:
        raise QualificationError("checkpoint restore policy is invalid")
    expected_providers = {
        logical_name: (provider, role) for logical_name, (provider, role) in _EXPECTED_ROLES.items()
    }
    actual_providers = {
        item.logical_name: (item.provider, item.role) for item in checkpoint.providers
    }
    if actual_providers != expected_providers or checkpoint.providers != tuple(
        sorted(checkpoint.providers, key=lambda item: item.logical_name)
    ):
        raise QualificationError("checkpoint provider contracts are invalid")
    if _SHA256_RE.fullmatch(checkpoint.provider_binding_sha256) is None or any(
        not item.region
        or item.storage_class != ("DEEP_ARCHIVE" if item.provider == "aws" else "STANDARD")
        or item.read_mode != ("restore_required" if item.provider == "aws" else "immediate")
        for item in checkpoint.providers
    ):
        raise QualificationError("checkpoint provider details are invalid")
    if checkpoint.artifacts != tuple(
        sorted(checkpoint.artifacts, key=lambda item: item.surface)
    ) or any(
        item.surface not in {"b2-archive", "aws-deep-archive", "cloudfront-egress"}
        or _SHA256_RE.fullmatch(item.sha256) is None
        or item.objects <= 0
        or item.bytes < 0
        for item in checkpoint.artifacts
    ):
        raise QualificationError("checkpoint artifact identities are invalid")
    if _checkpoint_digest(checkpoint) != checkpoint.checkpoint_sha256:
        raise QualificationError("checkpoint digest does not match its contents")
    return checkpoint


def evidence_from_checkpoint(checkpoint: QualificationCheckpoint) -> dict[str, object]:
    if checkpoint.phase not in {"cleaned", "failed"}:
        raise QualificationError("final evidence requires a cleaned or failed checkpoint")
    return {
        "schema": EVIDENCE_SCHEMA,
        "run_id": checkpoint.run_id,
        "source_sha": checkpoint.source_sha,
        "source_ref": checkpoint.source_ref,
        "config_sha256": checkpoint.config_sha256,
        "corpus": {
            "profile": checkpoint.corpus_profile,
            "sha256": checkpoint.corpus_sha256,
            "files": checkpoint.corpus_files,
            "bytes": checkpoint.corpus_bytes,
        },
        "providers": [asdict(item) for item in checkpoint.providers],
        "artifacts": [asdict(item) for item in checkpoint.artifacts],
        "restore": {
            "tier": checkpoint.restore_tier,
            "copy_days": checkpoint.restore_copy_days,
            "deadline_at": checkpoint.restore_deadline_at,
        },
        "limits": {
            "monthly_download_quota_bytes": QUALIFICATION_MONTHLY_DOWNLOAD_QUOTA_BYTES,
            "corpus_bytes": checkpoint.corpus_bytes,
        },
        "egress": {
            "provider": "aws",
            "service": "cloudfront",
            "transport": "signed-https",
        },
        "status": "passed" if checkpoint.phase == "cleaned" else "failed",
        "started_at": checkpoint.started_at,
        "completed_at": checkpoint.updated_at,
        "checkpoint_sha256": checkpoint.checkpoint_sha256,
        "phases": [asdict(record) for record in checkpoint.history],
    }


def _boto3_clients(region: str) -> tuple[object, object]:
    try:
        import boto3
    except ImportError as exc:  # pragma: no cover - locked workspace includes boto3
        raise QualificationError("AWS qualification requires boto3") from exc
    return boto3.client("s3", region_name=region), boto3.client("cloudfront")


def _cloudfront_public_key(config: QualificationConfig, values: Mapping[str, str]) -> str:
    path = Path(_required_env(values, config.cloudfront.public_key_path_env)).expanduser()
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise QualificationError("cannot read the configured CloudFront public key") from exc
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        key = serialization.load_pem_public_key(content)
    except (TypeError, ValueError) as exc:
        raise QualificationError("CloudFront public key is not valid PEM") from exc
    if not isinstance(key, rsa.RSAPublicKey) or key.key_size != 2048:
        raise QualificationError("CloudFront signing requires an RSA 2048 public key")
    return content.decode("ascii")


def _cloudfront_private_key_path(
    config: QualificationConfig,
    values: Mapping[str, str],
) -> Path:
    path = (
        Path(_required_env(values, config.cloudfront.private_key_path_env)).expanduser().resolve()
    )
    try:
        mode = path.stat().st_mode
        content = path.read_bytes()
    except OSError as exc:
        raise QualificationError("cannot read the configured CloudFront private key") from exc
    if mode & 0o077:
        raise QualificationError("CloudFront private key must not be accessible by group or other")
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        key = serialization.load_pem_private_key(content, password=None)
    except (TypeError, ValueError) as exc:
        raise QualificationError("CloudFront private key is not valid unencrypted PEM") from exc
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size != 2048:
        raise QualificationError("CloudFront signing requires an RSA 2048 private key")
    return path


def _validate_cloudfront_key_pair(
    config: QualificationConfig,
    values: Mapping[str, str],
) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    public_key = serialization.load_pem_public_key(
        _cloudfront_public_key(config, values).encode("ascii")
    )
    private_key = serialization.load_pem_private_key(
        _cloudfront_private_key_path(config, values).read_bytes(),
        password=None,
    )
    if not isinstance(public_key, rsa.RSAPublicKey) or not isinstance(
        private_key, rsa.RSAPrivateKey
    ):  # pragma: no cover - both loaders validate these types
        raise QualificationError("CloudFront signing keys must be RSA")
    if public_key.public_numbers() != private_key.public_key().public_numbers():
        raise QualificationError("CloudFront public and private signing keys do not match")


def _s3_endpoint(bucket: ResolvedBucket, values: Mapping[str, str]) -> str:
    if bucket.provider == "aws":
        if bucket.region == "us-east-1":
            return "https://s3.amazonaws.com"
        return f"https://s3.{bucket.region}.amazonaws.com"
    override = values.get("RIVERHOG_QUALIFICATION_B2_S3_ENDPOINT_URL", "").strip()
    return override.rstrip("/") or f"https://s3.{bucket.region}.backblazeb2.com"


def _credential_prefix(logical_name: str) -> str:
    return f"RIVERHOG_QUALIFICATION_{logical_name.upper().replace('-', '_')}"


def _store_env_suffix(logical_name: str) -> str:
    return logical_name.upper().replace("-", "_")


def _runtime_credentials(
    bucket: ResolvedBucket,
    values: Mapping[str, str],
) -> tuple[str, str, str | None]:
    if bucket.provider == "aws":
        return (
            _required_env(values, "AWS_ACCESS_KEY_ID"),
            _required_env(values, "AWS_SECRET_ACCESS_KEY"),
            _required_env(values, "AWS_SESSION_TOKEN"),
        )
    prefix = _credential_prefix(bucket.logical_name)
    return (
        _required_env(values, f"{prefix}_ACCESS_KEY_ID"),
        _required_env(values, f"{prefix}_SECRET_ACCESS_KEY"),
        None,
    )


def _dotenv_value(value: str) -> str:
    if "\x00" in value or "\n" in value or "\r" in value:
        raise QualificationError("deployment environment values must be single-line text")
    return json.dumps(value.replace("$", "$$"), ensure_ascii=True)


def _write_private_environment(path: Path, values: Mapping[str, str]) -> None:
    path = path.expanduser().resolve()
    if path.exists():
        raise QualificationError("deployment environment output must not already exist")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            for name, value in sorted(values.items()):
                handle.write(f"{name}={_dotenv_value(value)}\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def write_runtime_environment(
    *,
    config: QualificationConfig,
    checkpoint: QualificationCheckpoint,
    buckets: Sequence[ResolvedBucket],
    cloudfront: CloudFrontManager,
    values: Mapping[str, str],
    output: Path,
) -> None:
    if checkpoint.config_sha256 != config.config_sha256:
        raise QualificationError("checkpoint and provider configuration do not match")
    _verify_checkpoint_providers(checkpoint, buckets, config)
    _validate_cloudfront_key_pair(config, values)
    by_name = {bucket.logical_name: bucket for bucket in buckets}
    cloudfront_base_url, cloudfront_public_key_id = cloudfront.runtime_configuration()
    private_key_path = _cloudfront_private_key_path(config, values)
    runtime: dict[str, str] = {
        "RIVERHOG_ARCHIVE_STORES": "b2-archive,aws-deep-archive",
        "RIVERHOG_ARCHIVE_WRITE_STORE": "b2-archive",
        "RIVERHOG_ARCHIVE_READ_ORDER": "aws-deep-archive,b2-archive",
        "RIVERHOG_ARCHIVE_PASSPHRASE": _required_env(
            values, "RIVERHOG_QUALIFICATION_ARCHIVE_PASSPHRASE"
        ),
        "RIVERHOG_ARCHIVE_REQUIRE_EXPLICIT_PASSPHRASE": "true",
        "RIVERHOG_BOOTSTRAP_TOKEN": _required_env(values, "RIVERHOG_QUALIFICATION_BOOTSTRAP_TOKEN"),
        "RIVERHOG_CLOUDFRONT_PRIVATE_KEY_HOST_PATH": str(private_key_path),
        "RIVERHOG_PUBLIC_BASE_URL": "http://app:8000",
        "RIVERHOG_RETRIEVAL_TIER": config.restore_tier,
        "RIVERHOG_RETRIEVAL_ESTIMATED_LATENCY": "48h",
        "RIVERHOG_RETRIEVAL_CACHE_PREFIX": checkpoint.namespace,
        "RIVERHOG_RETRIEVAL_CACHE_NEW_ARCHIVE_LEASE": f"{config.restore_copy_days}d",
        "RIVERHOG_RETRIEVAL_DEFAULT_LEASE": f"{config.restore_copy_days}d",
        "RIVERHOG_RETRIEVAL_MAX_LEASE": f"{config.restore_copy_days}d",
        "SOURCE_REVISION": checkpoint.source_sha,
        "TEST_COMPOSE_PROJECT_NAME": f"riverhog-qualification-{checkpoint.run_id[:12]}",
    }
    for logical_name in ("b2-archive", "aws-deep-archive"):
        bucket = by_name[logical_name]
        access_key, secret_key, session_token = _runtime_credentials(bucket, values)
        prefix = f"RIVERHOG_ARCHIVE_STORE_{_store_env_suffix(logical_name)}_"
        runtime.update(
            {
                f"{prefix}ENDPOINT_URL": _s3_endpoint(bucket, values),
                f"{prefix}REGION": bucket.region,
                f"{prefix}BUCKET": bucket.bucket_name,
                f"{prefix}ACCESS_KEY_ID": access_key,
                f"{prefix}SECRET_ACCESS_KEY": secret_key,
                f"{prefix}SESSION_TOKEN": session_token or "",
                f"{prefix}FORCE_PATH_STYLE": "false",
                f"{prefix}PREFIX": checkpoint.namespace,
                f"{prefix}BACKEND": bucket.provider,
                f"{prefix}STORAGE_CLASS": (
                    "DEEP_ARCHIVE" if logical_name == "aws-deep-archive" else "STANDARD"
                ),
                f"{prefix}READ_MODE": (
                    "restore_required" if logical_name == "aws-deep-archive" else "immediate"
                ),
            }
        )
    deep_prefix = "RIVERHOG_ARCHIVE_STORE_AWS_DEEP_ARCHIVE_"
    runtime.update(
        {
            f"{deep_prefix}CLOUDFRONT_BASE_URL": cloudfront_base_url,
            f"{deep_prefix}CLOUDFRONT_PUBLIC_KEY_ID": cloudfront_public_key_id,
            f"{deep_prefix}CLOUDFRONT_PRIVATE_KEY_PATH": ("/run/secrets/riverhog-cloudfront.pem"),
            f"{deep_prefix}MONTHLY_DOWNLOAD_ALLOWANCE_BYTES": "1TB",
            f"{deep_prefix}DOWNLOAD_SAFETY_BUFFER_BYTES": "10GB",
        }
    )
    cache = by_name["b2-retrieval-cache"]
    cache_key, cache_secret, _cache_token = _runtime_credentials(cache, values)
    runtime.update(
        {
            "RIVERHOG_RETRIEVAL_CACHE_ENDPOINT_URL": _s3_endpoint(cache, values),
            "RIVERHOG_RETRIEVAL_CACHE_REGION": cache.region,
            "RIVERHOG_RETRIEVAL_CACHE_BUCKET": cache.bucket_name,
            "RIVERHOG_RETRIEVAL_CACHE_ACCESS_KEY_ID": cache_key,
            "RIVERHOG_RETRIEVAL_CACHE_SECRET_ACCESS_KEY": cache_secret,
            "RIVERHOG_RETRIEVAL_CACHE_FORCE_PATH_STYLE": "false",
        }
    )
    _write_private_environment(output, runtime)


def verify_corpus(root: Path, corpus: CorpusManifest) -> None:
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise QualificationError("qualification corpus root does not exist")
    actual = {path.relative_to(root).as_posix(): path for path in root.rglob("*") if path.is_file()}
    expected = {item.path: item for item in corpus.files}
    if set(actual) != set(expected):
        raise QualificationError("qualification corpus files differ from its manifest")
    for relative, item in expected.items():
        path = actual[relative]
        digest = hashlib.sha256()
        byte_count = 0
        with path.open("rb") as handle:
            while chunk := handle.read(8 * MIB):
                byte_count += len(chunk)
                digest.update(chunk)
        if byte_count != item.bytes or digest.hexdigest() != item.sha256:
            raise QualificationError(f"qualification corpus identity changed: {relative}")


def _page_items(payload: Mapping[str, object], key: str) -> list[dict[str, Any]]:
    values = payload.get(key)
    if not isinstance(values, list):
        raise QualificationError(f"Riverhog response omitted {key}")
    return [cast(dict[str, Any], item) for item in values if isinstance(item, dict)]


def _collection_copy(
    payload: Mapping[str, object],
    store: str,
) -> dict[str, Any]:
    for item in _page_items(payload, "archive_copies"):
        if item.get("store") == store:
            return item
    raise QualificationError(f"collection does not have its {store} archive copy")


@contextlib.contextmanager
def _qualification_api(
    *,
    base_url: str,
    bootstrap_token: str,
    allow_insecure_http: bool,
) -> Iterator[tuple[Any, str]]:
    from riverhog_api_client.client import ApiClient

    bootstrap = ApiClient(
        base_url=base_url,
        token=bootstrap_token,
        allow_insecure_http=allow_insecure_http,
    )
    created = bootstrap.create_app_key(
        "provider-qualification",
        access=({"permission": "*", "resource": "*"},),
        expires_in_seconds=6 * 60 * 60,
    )
    token = created.get("token")
    key_id = created.get("id")
    if not isinstance(token, str) or not isinstance(key_id, str):
        bootstrap.close()
        raise QualificationError("Riverhog did not return a transient qualification key")
    try:
        quota = bootstrap.set_app_key_download_quota(
            "provider-qualification",
            key_id,
            monthly_bytes=QUALIFICATION_MONTHLY_DOWNLOAD_QUOTA_BYTES,
        )
        if quota.get("monthly_bytes") != QUALIFICATION_MONTHLY_DOWNLOAD_QUOTA_BYTES:
            raise QualificationError("Riverhog did not apply the bounded qualification quota")
    except BaseException:
        try:
            bootstrap.revoke_app_key("provider-qualification", key_id)
        finally:
            bootstrap.close()
        raise
    api = ApiClient(
        base_url=base_url,
        token=token,
        allow_insecure_http=allow_insecure_http,
    )
    try:
        yield api, token
    finally:
        api.close()
        try:
            bootstrap.revoke_app_key("provider-qualification", key_id)
        finally:
            bootstrap.close()


def _upload_collection_with_observation(
    api: Any,
    *,
    root: Path,
    checkpoint: QualificationCheckpoint,
    base_url: str,
    token: str,
    allow_insecure_http: bool,
) -> tuple[int, tuple[str, ...]]:
    executable = shutil.which("riverhog")
    if executable is None:
        raise QualificationError("the official riverhog CLI is unavailable")
    environment = os.environ.copy()
    environment.update(
        {
            "RIVERHOG_BASE_URL": base_url,
            "RIVERHOG_TOKEN": token,
            "RIVERHOG_ALLOW_INSECURE_HTTP": "true" if allow_insecure_http else "false",
            "RIVERHOG_CLI_PLAIN": "1",
        }
    )
    command = [
        executable,
        "collection",
        "upload",
        "start",
        str(root.expanduser().resolve()),
        "--archive-store",
        "b2-archive",
        "--idempotency-key",
        f"provider-qualification:{checkpoint.run_id}",
        "--omit-provenance",
        "deterministic provider qualification corpus",
        "--json",
    ]
    resolved_root = str(root.expanduser().resolve())
    observed: set[str] = set()
    collection_id: int | None = checkpoint.collection_id
    interrupt_client = checkpoint.corpus_profile == "multipart"
    interrupted = False

    def observe() -> None:
        nonlocal collection_id
        page = api.list_collection_upload_sessions(all_items=True)
        uploads = _page_items(page, "uploads")
        upload = next(
            (item for item in uploads if item.get("ingest_source") == resolved_root),
            None,
        )
        if upload is None:
            return
        collection_id = int(upload["collection_id"])
        session = api.get_collection_upload_session(collection_id)
        if session.get("state") in {"open", "uploading", "finalizing"}:
            observed.add("session-show")
        files = api.list_collection_upload_session_files(collection_id, all_items=True)
        if _page_items(files, "files"):
            observed.add("registered-file-list")
        volume_page = api.list_collection_upload_session_volumes(collection_id)
        volumes = _page_items(volume_page, "volumes")
        if volumes:
            observed.add("volume-list")
        for volume in volumes:
            volume_id = str(volume["volume_id"])
            shown = api.get_collection_upload_session_volume(collection_id, volume_id)
            observed.add("volume-show")
            units = shown.get("units")
            if not isinstance(units, list) or not units:
                continue
            unit = int(cast(dict[str, Any], units[0])["unit"])
            readback = api.get_collection_upload_session_unit(
                collection_id,
                volume_id,
                unit,
            )
            if int(readback.get("unit", -1)) == unit:
                observed.add("unit-readback")
                if readback.get("state") == "committed":
                    observed.add("committed-unit-readback")

    stdout = ""
    deadline = time.monotonic() + 6 * 60 * 60
    with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as stderr:
        while True:
            if interrupted:
                observed.add("multipart-client-restarted")
            process = subprocess.Popen(  # noqa: S603
                command,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=stderr,
                text=True,
            )
            restart = False
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    process.terminate()
                    raise QualificationError(
                        "Riverhog CLI upload exceeded its qualification deadline"
                    )
                try:
                    observe()
                except Exception:
                    # The session can move between transactional states while the observer polls.
                    pass
                if interrupt_client and not interrupted and "committed-unit-readback" in observed:
                    process.terminate()
                    try:
                        process.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=30)
                    interrupted = True
                    observed.add("multipart-client-interrupted")
                    restart = True
                    break
                time.sleep(0.1)
            stdout, _ = process.communicate()
            if restart:
                continue
            if process.returncode != 0:
                raise QualificationError("the official Riverhog CLI upload failed")
            try:
                observe()
            except Exception:
                # Preserve observations gathered while the client was active. A
                # finalized session can disappear from an active-session listing.
                pass
            break
    try:
        payload = json.loads(stdout)
        result_id = int(payload["collection_id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise QualificationError("the Riverhog CLI returned invalid upload JSON") from exc
    if collection_id is not None and collection_id != result_id:
        raise QualificationError("observed upload identity changed during finalization")
    required = {
        "committed-unit-readback",
        "session-show",
        "registered-file-list",
        "volume-list",
        "volume-show",
        "unit-readback",
    }
    if interrupt_client:
        required.update({"multipart-client-interrupted", "multipart-client-restarted"})
    if not required <= observed:
        raise QualificationError(
            "upload completed before required session observations were established: "
            + ", ".join(sorted(required - observed))
        )
    return result_id, tuple(sorted(observed))


def _wait_archive_copy(
    api: Any,
    *,
    collection_id: int,
    destination: str,
    source: str,
) -> dict[str, Any]:
    api.create_or_resume_archive_copy(
        collection_id,
        destination_store=destination,
        source_store=source,
        event_context={"qualification": "provider-v1"},
    )
    deadline = time.monotonic() + 60 * 60
    while True:
        payload = api.get_archive_copy_job(collection_id, destination_store=destination)
        state = str(payload.get("state", ""))
        if state == "completed":
            jobs = api.list_archive_copy_jobs(q=destination, all_items=True)
            if not any(
                int(item.get("collection_id", 0)) == collection_id
                and item.get("destination_store") == destination
                for item in _page_items(jobs, "copies")
            ):
                raise QualificationError("archive-copy list omitted the completed job")
            return cast(dict[str, Any], payload)
        if state in {"failed", "canceled", "expired"}:
            raise QualificationError(f"archive copy to {destination} ended in {state}")
        if time.monotonic() >= deadline:
            raise QualificationError(f"archive copy to {destination} exceeded one hour")
        time.sleep(2)


def _retrieval_files(collection_id: int, corpus: CorpusManifest) -> tuple[tuple[int, str], ...]:
    return tuple((collection_id, item.path) for item in corpus.files)


def _plan_sources(plan: Mapping[str, object]) -> set[str]:
    return {str(item.get("source_store", "")) for item in _page_items(plan, "objects")}


def _download_retrieval(
    api: Any,
    *,
    job: Mapping[str, object],
    corpus: CorpusManifest,
    output: Path,
) -> None:
    job_id = str(job["id"])
    collection_id = int(cast(list[dict[str, Any]], job["files"])[0]["collection_id"])
    output.mkdir(parents=True)
    for item in corpus.files:
        destination = output / item.path
        destination.parent.mkdir(parents=True, exist_ok=True)
        api.download_retrieval_file(
            job_id,
            collection_id=collection_id,
            path=item.path,
            output=destination,
            expected_bytes=item.bytes,
            expected_sha256=item.sha256,
        )
    verify_corpus(output, corpus)


def _ready_retrieval(
    api: Any,
    *,
    collection_id: int,
    corpus: CorpusManifest,
    expected_source: str,
    lease_seconds: int,
    output: Path,
) -> dict[str, Any]:
    files = _retrieval_files(collection_id, corpus)
    plan = api.plan_retrieval(files, lease_seconds=lease_seconds)
    if _plan_sources(plan) != {expected_source}:
        raise QualificationError(f"retrieval did not select {expected_source}")
    if int(plan.get("lease_seconds", -1)) != lease_seconds:
        raise QualificationError("retrieval plan did not preserve its bounded lease")
    job = api.create_retrieval_job(
        files,
        plan_etag=str(plan["etag"]),
        lease_seconds=lease_seconds,
        event_context={"qualification": "provider-v1"},
    )
    deadline = time.monotonic() + 30 * 60
    while str(job.get("state", "")) != "ready":
        state = str(job.get("state", ""))
        if state in {"failed", "canceled", "expired"}:
            raise QualificationError(f"immediate retrieval ended in {state}")
        if time.monotonic() >= deadline:
            raise QualificationError("immediate retrieval exceeded thirty minutes")
        time.sleep(2)
        job = api.get_retrieval_job(str(job["id"]))
    _download_retrieval(api, job=job, corpus=corpus, output=output)
    acknowledged = api.acknowledge_retrieval_job(str(job["id"]))
    if acknowledged.get("state") != "completed":
        raise QualificationError("retrieval acknowledgement did not complete")
    return cast(dict[str, Any], acknowledged)


def _cancel_retrieval(
    api: Any,
    *,
    collection_id: int,
    corpus: CorpusManifest,
    lease_seconds: int,
) -> None:
    files = _retrieval_files(collection_id, corpus)
    plan = api.plan_retrieval(files, lease_seconds=lease_seconds)
    job = api.create_retrieval_job(
        files,
        plan_etag=str(plan["etag"]),
        lease_seconds=lease_seconds,
    )
    canceled = api.cancel_retrieval_job(str(job["id"]))
    if canceled.get("state") != "canceled":
        raise QualificationError("retrieval cancellation did not converge")


def _assert_resourcesync(api: Any, collection_id: int) -> None:
    discovery = api.resourcesync_discovery()
    if {item.get("capability") for item in _page_items(discovery, "capabilities")} != {
        "capabilitylist"
    }:
        raise QualificationError("ResourceSync discovery is incomplete")
    capabilities = api.resourcesync_capabilities()
    if {item.get("capability") for item in _page_items(capabilities, "capabilities")} != {
        "resourcelist",
        "changelist",
    }:
        raise QualificationError("ResourceSync capability list is incomplete")
    pages = api.resourcesync_resource_pages().get("pages")
    if not isinstance(pages, list) or not pages:
        raise QualificationError("ResourceSync resource list has no pages")
    resources: list[dict[str, Any]] = []
    for page in range(1, len(pages) + 1):
        resources.extend(_page_items(api.resourcesync_resources(page=page), "resources"))
    resource = next(
        (item for item in resources if int(item.get("collection_id", 0)) == collection_id),
        None,
    )
    if resource is None:
        raise QualificationError("ResourceSync omitted the qualification collection")
    portable = api.get_portable_collection_manifest(collection_id)
    if portable.get("format") != "riverhog-collection/v1":
        raise QualificationError("portable collection manifest format is invalid")
    changes = api.catalog_changes(after=0)
    if not any(
        int(item.get("collection_id", 0)) == collection_id
        for item in _page_items(changes, "changes")
    ):
        raise QualificationError("ResourceSync change cursor omitted the collection")


def _assert_lifecycle_events(api: Any, required: set[str]) -> None:
    cursor: str | None = None
    types: set[str] = set()
    previous = "0"
    while True:
        page = api.list_lifecycle_events(after=cursor, limit=100)
        if int(page.next_cursor) < int(previous):
            raise QualificationError("lifecycle event cursor regressed")
        previous = page.next_cursor
        types.update(event.type.removeprefix("io.riverhog.riverhog.") for event in page.events)
        if not page.has_more:
            break
        cursor = page.next_cursor
    if not required <= types:
        raise QualificationError(
            "lifecycle event stream omitted: " + ", ".join(sorted(required - types))
        )


def _runtime_s3_client(bucket: ResolvedBucket, values: Mapping[str, str]) -> object:
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:  # pragma: no cover - locked workspace includes boto3
        raise QualificationError("provider recovery requires boto3") from exc
    access_key, secret_key, session_token = _runtime_credentials(bucket, values)
    return boto3.client(
        "s3",
        endpoint_url=_s3_endpoint(bucket, values),
        region_name=bucket.region,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        aws_session_token=session_token,
        config=Config(s3={"addressing_style": "virtual"}),
    )


def _archive_copy_prefix(api: Any, collection_id: int, store: str) -> str:
    collection = api.get_collection(collection_id)
    copy = _collection_copy(collection, store)
    if copy.get("state") != "uploaded":
        raise QualificationError(f"{store} archive copy is not uploaded")
    prefix = copy.get("storage_prefix")
    if not isinstance(prefix, str) or not prefix:
        raise QualificationError(f"{store} archive copy has no storage prefix")
    return prefix


def _list_archive_keys(client: object, *, bucket: str, prefix: str) -> tuple[str, ...]:
    request: dict[str, object] = {"Bucket": bucket, "Prefix": f"{prefix.rstrip('/')}/"}
    keys: list[str] = []
    while True:
        response = cast(dict[str, Any], cast(Any, client).list_objects_v2(**request))
        keys.extend(
            str(item["Key"])
            for item in response.get("Contents", [])
            if isinstance(item, dict) and item.get("Key")
        )
        if not response.get("IsTruncated"):
            break
        token = response.get("NextContinuationToken")
        if not isinstance(token, str) or not token:
            raise QualificationError("archive object listing omitted its continuation token")
        request["ContinuationToken"] = token
    if not keys:
        raise QualificationError("archive copy has no provider objects")
    return tuple(sorted(keys))


def _stream_s3_object(
    client: object,
    *,
    bucket: str,
    key: str,
    output: Path,
) -> tuple[int, str]:
    response = cast(
        dict[str, Any],
        cast(Any, client).get_object(Bucket=bucket, Key=key),
    )
    body = response["Body"]
    digest = hashlib.sha256()
    byte_count = 0
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with output.open("xb") as handle:
            for chunk in body.iter_chunks(chunk_size=MIB):
                if not chunk:
                    continue
                handle.write(chunk)
                digest.update(chunk)
                byte_count += len(chunk)
    finally:
        close = getattr(body, "close", None)
        if callable(close):
            close()
    return byte_count, digest.hexdigest()


def _verify_recovered_tree(output: Path, corpus: CorpusManifest) -> None:
    verify_corpus(output, corpus)


def _independent_provider_recovery(
    api: Any,
    *,
    bucket: ResolvedBucket,
    collection_id: int,
    store: str,
    corpus: CorpusManifest,
    passphrase: str,
    scratch: Path,
    values: Mapping[str, str],
) -> tuple[str, tuple[str, ...], int]:
    from riverhog_recover import recover_archive

    prefix = _archive_copy_prefix(api, collection_id, store)
    client = _runtime_s3_client(bucket, values)
    archive = scratch / f"{store}-archive"
    archive.mkdir(parents=True)
    identities: list[str] = []
    total_bytes = 0
    for key in _list_archive_keys(client, bucket=bucket.bucket_name, prefix=prefix):
        relative = key.removeprefix(f"{prefix.rstrip('/')}/")
        if not relative or relative == key:
            raise QualificationError("provider archive object escaped its copy prefix")
        byte_count, sha256 = _stream_s3_object(
            client,
            bucket=bucket.bucket_name,
            key=key,
            output=archive / relative,
        )
        identities.append(f"{relative}:{byte_count}:{sha256}")
        total_bytes += byte_count
    output = scratch / f"{store}-recovered"
    summary = recover_archive(archive, output, passphrase=passphrase)
    if summary.files != len(corpus.files) or summary.bytes != corpus.bytes:
        raise QualificationError("independent recovery summary differs from the corpus")
    _verify_recovered_tree(output, corpus)
    identity = hashlib.sha256(_canonical_json(sorted(identities))).hexdigest()
    return identity, tuple(sorted(identities)), total_bytes


def _cloudfront_signer(config: QualificationConfig, values: Mapping[str, str], key_id: str):  # type: ignore[no-untyped-def]
    from botocore.signers import CloudFrontSigner
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    path = _cloudfront_private_key_path(config, values)
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey):  # pragma: no cover - validated above
        raise QualificationError("CloudFront private key is not RSA")

    def sign(message: bytes) -> bytes:
        return key.sign(message, padding.PKCS1v15(), hashes.SHA1())

    return CloudFrontSigner(key_id, sign)


def _cloudfront_object_digest(url: str) -> tuple[int, str, str]:
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - locked workspace includes httpx
        raise QualificationError("CloudFront qualification requires httpx") from exc
    digest = hashlib.sha256()
    byte_count = 0
    with httpx.Client(
        http2=True,
        follow_redirects=False,
        timeout=httpx.Timeout(connect=10, read=300, write=60, pool=10),
    ) as client:
        with client.stream("GET", url, headers={"Accept-Encoding": "identity"}) as response:
            if not response.is_success:
                raise QualificationError(f"CloudFront egress returned HTTP {response.status_code}")
            cache = response.headers.get("x-cache", "")
            for chunk in response.iter_bytes(chunk_size=MIB):
                byte_count += len(chunk)
                digest.update(chunk)
    return byte_count, digest.hexdigest(), cache


def _verify_cloudfront_egress(
    api: Any,
    *,
    bucket: ResolvedBucket,
    collection_id: int,
    config: QualificationConfig,
    cloudfront: CloudFrontManager,
    values: Mapping[str, str],
) -> tuple[str, int, int]:
    from datetime import timedelta as _timedelta
    from urllib.parse import quote as _quote

    prefix = _archive_copy_prefix(api, collection_id, "aws-deep-archive")
    client = _runtime_s3_client(bucket, values)
    base_url, key_id = cloudfront.runtime_configuration()
    signer = _cloudfront_signer(config, values, key_id)
    keys = _list_archive_keys(client, bucket=bucket.bucket_name, prefix=prefix)
    identities: list[str] = []
    total_bytes = 0
    for key in keys:
        head = cast(
            dict[str, Any],
            cast(Any, client).head_object(Bucket=bucket.bucket_name, Key=key),
        )
        restore = str(head.get("Restore", ""))
        if 'ongoing-request="false"' not in restore:
            raise QualificationError("Deep Archive object is not restored for egress")
        if head.get("StorageClass") != "DEEP_ARCHIVE":
            raise QualificationError("AWS qualification object is not in Deep Archive")
        expected_bytes = int(head.get("ContentLength", -1))
        metadata = head.get("Metadata")
        expected_sha = (
            str(metadata.get("riverhog-stored-sha256", "")) if isinstance(metadata, dict) else ""
        )
        if _SHA256_RE.fullmatch(expected_sha) is None:
            raise QualificationError("Deep Archive object has no Riverhog ciphertext identity")
        object_url = f"{base_url}/{_quote(key, safe='/')}"
        version_id = head.get("VersionId")
        if isinstance(version_id, str) and version_id and version_id != "null":
            object_url = f"{object_url}?versionId={_quote(version_id, safe='')}"
        signed = signer.generate_presigned_url(
            object_url,
            date_less_than=datetime.now(UTC) + _timedelta(minutes=15),
        )
        first = _cloudfront_object_digest(signed)
        second = _cloudfront_object_digest(signed)
        if first[:2] != (expected_bytes, expected_sha) or second[:2] != first[:2]:
            raise QualificationError("CloudFront ciphertext identity verification failed")
        if "Hit" not in second[2]:
            raise QualificationError("CloudFront second egress read did not hit its cache")
        relative = key.removeprefix(f"{prefix.rstrip('/')}/")
        if not relative or relative == key:
            raise QualificationError("CloudFront object escaped its archive prefix")
        identities.append(f"{relative}:{expected_bytes}:{expected_sha}")
        total_bytes += expected_bytes
    return (
        hashlib.sha256(_canonical_json(sorted(identities))).hexdigest(),
        len(keys),
        total_bytes,
    )


def _retire_copy(api: Any, collection_id: int, store: str) -> None:
    plan = api.plan_archive_copy_retirement(collection_id, store=store)
    blockers = plan.get("blockers")
    if isinstance(blockers, list) and blockers:
        raise QualificationError(f"{store} qualification copy cannot be retired")
    challenge = plan.get("challenge")
    if not isinstance(challenge, str) or not challenge:
        raise QualificationError(f"{store} retirement challenge is missing")
    result = api.retire_archive_copy(collection_id, store=store, challenge=challenge)
    if result.get("status") not in {"retired", "already_absent"}:
        raise QualificationError(f"{store} qualification copy did not retire")


def _cancel_archive_copy(api: Any, collection_id: int, destination: str, source: str) -> None:
    api.create_or_resume_archive_copy(
        collection_id,
        destination_store=destination,
        source_store=source,
        event_context={"qualification": "cancellation-v1"},
    )
    payload = api.cancel_archive_copy_job(collection_id, destination_store=destination)
    deadline = time.monotonic() + 10 * 60
    while str(payload.get("state", "")) != "canceled":
        if str(payload.get("state", "")) not in {"canceling"}:
            raise QualificationError("archive-copy cancellation entered an invalid state")
        if time.monotonic() >= deadline:
            raise QualificationError("archive-copy cancellation exceeded ten minutes")
        time.sleep(1)
        payload = api.get_archive_copy_job(collection_id, destination_store=destination)


def operate_qualification(
    *,
    config: QualificationConfig,
    checkpoint: QualificationCheckpoint,
    checkpoint_path: Path,
    corpus: CorpusManifest,
    corpus_root: Path,
    base_url: str,
    allow_insecure_http: bool,
    buckets: Sequence[ResolvedBucket],
    cloudfront: CloudFrontManager,
    values: Mapping[str, str],
) -> QualificationCheckpoint:
    if checkpoint.config_sha256 != config.config_sha256:
        raise QualificationError("checkpoint and provider configuration do not match")
    if checkpoint.corpus_sha256 != corpus.sha256:
        raise QualificationError("checkpoint and qualification corpus do not match")
    _verify_checkpoint_providers(checkpoint, buckets, config)
    verify_corpus(corpus_root, corpus)
    by_name = {bucket.logical_name: bucket for bucket in buckets}
    passphrase = _required_env(values, "RIVERHOG_QUALIFICATION_ARCHIVE_PASSPHRASE")
    bootstrap_token = _required_env(values, "RIVERHOG_QUALIFICATION_BOOTSTRAP_TOKEN")
    lease_seconds = config.restore_copy_days * 24 * 60 * 60
    with _qualification_api(
        base_url=base_url,
        bootstrap_token=bootstrap_token,
        allow_insecure_http=allow_insecure_http,
    ) as (api, token):
        if checkpoint.phase == "created":
            collection_id, observations = _upload_collection_with_observation(
                api,
                root=corpus_root,
                checkpoint=checkpoint,
                base_url=base_url,
                token=token,
                allow_insecure_http=allow_insecure_http,
            )
            session = api.get_collection_upload_session(collection_id)
            if session.get("state") != "finalized":
                raise QualificationError("qualification upload did not finalize")
            if int(session.get("files_total", -1)) != len(corpus.files):
                raise QualificationError("finalized collection file count is invalid")
            _assert_resourcesync(api, collection_id)
            with tempfile.TemporaryDirectory(prefix="riverhog-qualification-immediate-") as raw:
                scratch = Path(raw)
                _ready_retrieval(
                    api,
                    collection_id=collection_id,
                    corpus=corpus,
                    expected_source="b2-archive",
                    lease_seconds=lease_seconds,
                    output=scratch / "client-retrieval",
                )
                _cancel_retrieval(
                    api,
                    collection_id=collection_id,
                    corpus=corpus,
                    lease_seconds=lease_seconds,
                )
                b2_identity, b2_objects, b2_bytes = _independent_provider_recovery(
                    api,
                    bucket=by_name["b2-archive"],
                    collection_id=collection_id,
                    store="b2-archive",
                    corpus=corpus,
                    passphrase=passphrase,
                    scratch=scratch,
                    values=values,
                )
            _assert_lifecycle_events(
                api,
                {
                    "collection.finalized",
                    "retrieval.ready",
                    "retrieval.completed",
                    "retrieval.canceled",
                },
            )
            checkpoint = advance_checkpoint(
                checkpoint,
                phase="immediate-qualified",
                assertions=(
                    *observations,
                    "b2-immediate-client-retrieval",
                    "b2-independent-recovery",
                    "resourcesync-complete",
                    "lifecycle-cursor-monotonic",
                    "download-quota-bounded",
                    "retrieval-lease-bounded",
                ),
                collection_id=collection_id,
                artifacts=(
                    ArtifactIdentity(
                        surface="b2-archive",
                        sha256=b2_identity,
                        objects=len(b2_objects),
                        bytes=b2_bytes,
                    ),
                ),
            )
            write_checkpoint(checkpoint_path, checkpoint)

        if checkpoint.phase == "immediate-qualified":
            if checkpoint.collection_id is None:
                raise QualificationError("qualification checkpoint has no collection identity")
            _cancel_archive_copy(
                api,
                checkpoint.collection_id,
                "aws-deep-archive",
                "b2-archive",
            )
            _wait_archive_copy(
                api,
                collection_id=checkpoint.collection_id,
                destination="aws-deep-archive",
                source="b2-archive",
            )
            _assert_lifecycle_events(
                api,
                {"archive_copy.requested", "archive_copy.canceled", "archive_copy.completed"},
            )
            checkpoint = advance_checkpoint(
                checkpoint,
                phase="deep-archive-uploaded",
                assertions=(
                    "deep-archive-copy-completed",
                    "archive-copy-list-show",
                    "archive-copy-cancellation",
                ),
            )
            write_checkpoint(checkpoint_path, checkpoint)

        if checkpoint.phase == "deep-archive-uploaded":
            if checkpoint.collection_id is None:
                raise QualificationError("qualification checkpoint has no collection identity")
            files = _retrieval_files(checkpoint.collection_id, corpus)
            plan = api.plan_retrieval(files, lease_seconds=lease_seconds)
            if _plan_sources(plan) != {"aws-deep-archive"}:
                raise QualificationError("Deep Archive retrieval did not select AWS")
            if {str(item.get("read_mode", "")) for item in _page_items(plan, "objects")} != {
                "restore_required"
            }:
                raise QualificationError("Deep Archive retrieval did not require restore")
            if int(plan.get("lease_seconds", -1)) != lease_seconds:
                raise QualificationError("Deep Archive plan did not preserve its bounded lease")
            job = api.create_retrieval_job(
                files,
                plan_etag=str(plan["etag"]),
                lease_seconds=lease_seconds,
                event_context={"qualification": "deep-archive-v1"},
            )
            job_id = str(job.get("id", ""))
            if not job_id or job.get("state") != "requested":
                raise QualificationError("Deep Archive retrieval was not requested")
            checkpoint = advance_checkpoint(
                checkpoint,
                phase="restore-requested",
                assertions=("deep-archive-restore-requested", "retrieval-plan-source-exact"),
                retrieval_job_id=job_id,
            )
            write_checkpoint(checkpoint_path, checkpoint)

        if checkpoint.phase in {"restore-requested", "restore-pending"}:
            if checkpoint.collection_id is None or checkpoint.retrieval_job_id is None:
                raise QualificationError("restore checkpoint identity is incomplete")
            collection_id = checkpoint.collection_id
            if _utc_now() > _parse_timestamp(checkpoint.restore_deadline_at):
                checkpoint = advance_checkpoint(
                    checkpoint,
                    phase="failed",
                    assertions=("deep-archive-restore-deadline-exceeded",),
                )
                write_checkpoint(checkpoint_path, checkpoint)
                return checkpoint
            job = api.get_retrieval_job(checkpoint.retrieval_job_id)
            state = str(job.get("state", ""))
            if state == "requested":
                checkpoint = advance_checkpoint(
                    checkpoint,
                    phase="restore-pending",
                    assertions=("deep-archive-restore-pending",),
                )
                write_checkpoint(checkpoint_path, checkpoint)
                return checkpoint
            if state in {"failed", "canceled", "expired"}:
                checkpoint = advance_checkpoint(
                    checkpoint,
                    phase="failed",
                    assertions=(f"deep-archive-retrieval-{state}",),
                )
                write_checkpoint(checkpoint_path, checkpoint)
                return checkpoint
            if state != "ready":
                raise QualificationError(f"Deep Archive retrieval has unknown state: {state}")
            checkpoint = advance_checkpoint(
                checkpoint,
                phase="restored",
                assertions=("deep-archive-restore-ready",),
            )
            write_checkpoint(checkpoint_path, checkpoint)
            cloudfront_identity, cloudfront_objects, cloudfront_bytes = _verify_cloudfront_egress(
                api,
                bucket=by_name["aws-deep-archive"],
                collection_id=collection_id,
                config=config,
                cloudfront=cloudfront,
                values=values,
            )
            with tempfile.TemporaryDirectory(prefix="riverhog-qualification-restored-") as raw:
                scratch = Path(raw)
                aws_identity, aws_objects, aws_bytes = _independent_provider_recovery(
                    api,
                    bucket=by_name["aws-deep-archive"],
                    collection_id=collection_id,
                    store="aws-deep-archive",
                    corpus=corpus,
                    passphrase=passphrase,
                    scratch=scratch,
                    values=values,
                )
                if (cloudfront_identity, cloudfront_objects, cloudfront_bytes) != (
                    aws_identity,
                    len(aws_objects),
                    aws_bytes,
                ):
                    raise QualificationError(
                        "CloudFront and direct AWS ciphertext identities differ"
                    )
                _download_retrieval(
                    api,
                    job=job,
                    corpus=corpus,
                    output=scratch / "client-retrieval",
                )
            acknowledged = api.acknowledge_retrieval_job(checkpoint.retrieval_job_id)
            if acknowledged.get("state") != "completed":
                raise QualificationError("Deep Archive retrieval acknowledgement failed")
            _assert_lifecycle_events(
                api,
                {"retrieval.requested", "retrieval.ready", "retrieval.completed"},
            )
            checkpoint = advance_checkpoint(
                checkpoint,
                phase="verified",
                assertions=(
                    "aws-direct-independent-recovery",
                    "cloudfront-signed-egress",
                    "cloudfront-warm-cache-hit",
                    "b2-retrieval-cache-hydrated",
                    "deep-client-retrieval",
                    "deep-retrieval-acknowledged",
                    "restart-boundary-survived",
                ),
                artifacts=(
                    ArtifactIdentity(
                        surface="aws-deep-archive",
                        sha256=aws_identity,
                        objects=len(aws_objects),
                        bytes=aws_bytes,
                    ),
                    ArtifactIdentity(
                        surface="cloudfront-egress",
                        sha256=cloudfront_identity,
                        objects=cloudfront_objects,
                        bytes=cloudfront_bytes,
                    ),
                ),
            )
            write_checkpoint(checkpoint_path, checkpoint)

        if checkpoint.phase == "verified":
            if checkpoint.collection_id is None:
                raise QualificationError("verified checkpoint has no collection identity")
            _retire_copy(api, checkpoint.collection_id, "b2-archive")
            checkpoint = advance_checkpoint(
                checkpoint,
                phase="cleaned",
                assertions=(
                    "b2-archive-copy-retired",
                    "aws-canary-retained-for-provider-minimum",
                    "retrieval-cache-lifecycle-bounded",
                ),
            )
            write_checkpoint(checkpoint_path, checkpoint)
        return checkpoint


def _real_managers(
    config: QualificationConfig,
    buckets: Sequence[ResolvedBucket],
    values: Mapping[str, str],
) -> tuple[dict[str, BucketManager], tuple[AdditionalInfrastructureManager, ...]]:
    aws_regions = {bucket.region for bucket in buckets if bucket.provider == "aws"}
    if len(aws_regions) != 1:
        raise QualificationError("AWS qualification buckets must resolve to one region")
    b2_client = B2NativeClient(
        key_id=_required_env(values, config.b2_provisioning.key_id_env),
        application_key=_required_env(values, config.b2_provisioning.application_key_env),
    )
    b2_client.authorize()
    aws_region = next(iter(aws_regions))
    s3_client, cloudfront_client = _boto3_clients(aws_region)
    managers: dict[str, BucketManager] = {
        "aws": AwsBucketManager(s3_client),
        "b2": B2BucketManager(b2_client),
    }
    if not config.cloudfront.enabled:
        return managers, ()
    deep_bucket = next(bucket for bucket in buckets if bucket.logical_name == "aws-deep-archive")
    return managers, (
        CloudFrontManager(
            cloudfront_client=cloudfront_client,
            s3_client=s3_client,
            bucket=deep_bucket,
            config=config,
            public_key_pem=_cloudfront_public_key(config, values),
        ),
    )


def _runtime_cloudfront_manager(
    config: QualificationConfig,
    buckets: Sequence[ResolvedBucket],
    values: Mapping[str, str],
) -> CloudFrontManager:
    if not config.cloudfront.enabled:
        raise QualificationError("CloudFront egress is required for the v1 qualification")
    deep_bucket = next(bucket for bucket in buckets if bucket.logical_name == "aws-deep-archive")
    s3_client, cloudfront_client = _boto3_clients(deep_bucket.region)
    return CloudFrontManager(
        cloudfront_client=cloudfront_client,
        s3_client=s3_client,
        bucket=deep_bucket,
        config=config,
        public_key_pem=_cloudfront_public_key(config, values),
    )


def _print_json(payload: object) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True), flush=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Provision and operate a resumable Riverhog provider qualification."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    config_parser = commands.add_parser("config-check", help="validate operator TOML")
    config_parser.add_argument("config", type=Path)
    config_parser.add_argument("--resolve", action="store_true", help="also require bucket env")

    infra = commands.add_parser("infrastructure", help="check or reconcile dedicated buckets")
    infra.add_argument("mode", choices=("plan", "check", "apply"))
    infra.add_argument("config", type=Path)

    runtime = commands.add_parser(
        "runtime-env",
        help="write a permission-restricted disposable deployment environment",
    )
    runtime.add_argument("config", type=Path)
    runtime.add_argument("--checkpoint", type=Path, required=True)
    runtime.add_argument("--output", type=Path, required=True)

    operate = commands.add_parser(
        "operate",
        help="advance one short, restartable provider-qualification invocation",
    )
    operate.add_argument("config", type=Path)
    operate.add_argument("--checkpoint", type=Path, required=True)
    operate.add_argument("--corpus-manifest", type=Path, required=True)
    operate.add_argument("--corpus-root", type=Path, required=True)
    operate.add_argument("--base-url", required=True)
    operate.add_argument(
        "--allow-insecure-http",
        action="store_true",
        help="explicitly opt into HTTP for a local disposable deployment",
    )

    corpus = commands.add_parser("corpus-create", help="create a deterministic corpus")
    corpus.add_argument("output", type=Path)
    corpus.add_argument("--profile", choices=("regular", "multipart"), default="regular")

    state = commands.add_parser("checkpoint-start", help="create a resumable checkpoint")
    state.add_argument("checkpoint", type=Path)
    state.add_argument("--config", type=Path, required=True)
    state.add_argument("--corpus-manifest", type=Path, required=True)
    state.add_argument("--source-sha", required=True)
    state.add_argument("--source-ref", required=True)
    state.add_argument("--run-id")

    status = commands.add_parser("checkpoint-show", help="verify and show a checkpoint")
    status.add_argument("checkpoint", type=Path)

    evidence = commands.add_parser("evidence", help="emit final machine evidence")
    evidence.add_argument("checkpoint", type=Path)
    evidence.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "config-check":
            config = load_config(args.config)
            resolved = resolve_buckets(config, os.environ) if args.resolve else ()
            _print_json(
                {
                    "schema": CONFIG_SCHEMA,
                    "config_sha256": config.config_sha256,
                    "buckets": [bucket.logical_name for bucket in config.buckets],
                    "resolved": bool(resolved),
                }
            )
            return 0
        if args.command == "infrastructure":
            config = load_config(args.config)
            buckets = resolve_buckets(config, os.environ)
            managers, additional = _real_managers(config, buckets, os.environ)
            if args.mode == "apply":
                plan = apply_infrastructure(config, buckets, managers, additional)
            else:
                plan = infrastructure_plan(config, buckets, managers, additional)
            _print_json(plan.as_dict())
            return 0 if args.mode != "check" or plan.ready else 1
        if args.command == "runtime-env":
            config = load_config(args.config)
            checkpoint = load_checkpoint(args.checkpoint)
            buckets = resolve_buckets(config, os.environ)
            write_runtime_environment(
                config=config,
                checkpoint=checkpoint,
                buckets=buckets,
                cloudfront=_runtime_cloudfront_manager(config, buckets, os.environ),
                values=os.environ,
                output=args.output,
            )
            _print_json({"written": True, "schema": CONFIG_SCHEMA})
            return 0
        if args.command == "operate":
            config = load_config(args.config)
            checkpoint = load_checkpoint(args.checkpoint)
            corpus = load_corpus_manifest(args.corpus_manifest)
            buckets = resolve_buckets(config, os.environ)
            checkpoint = operate_qualification(
                config=config,
                checkpoint=checkpoint,
                checkpoint_path=args.checkpoint,
                corpus=corpus,
                corpus_root=args.corpus_root,
                base_url=args.base_url,
                allow_insecure_http=args.allow_insecure_http,
                buckets=buckets,
                cloudfront=_runtime_cloudfront_manager(config, buckets, os.environ),
                values=os.environ,
            )
            _print_json(checkpoint.as_dict())
            return 1 if checkpoint.phase == "failed" else 0
        if args.command == "corpus-create":
            manifest = create_corpus(args.output, profile=args.profile)
            _print_json(
                {
                    "manifest": manifest.as_dict(),
                    "manifest_path": str(corpus_manifest_path(args.output)),
                }
            )
            return 0
        if args.command == "checkpoint-start":
            config = load_config(args.config)
            corpus = load_corpus_manifest(args.corpus_manifest)
            buckets = resolve_buckets(config, os.environ)
            checkpoint = new_checkpoint(
                source_sha=args.source_sha,
                source_ref=args.source_ref,
                config=config,
                corpus=corpus,
                buckets=buckets,
                run_id=args.run_id,
            )
            write_checkpoint(args.checkpoint, checkpoint)
            _print_json(checkpoint.as_dict())
            return 0
        if args.command == "checkpoint-show":
            _print_json(load_checkpoint(args.checkpoint).as_dict())
            return 0
        if args.command == "evidence":
            payload = evidence_from_checkpoint(load_checkpoint(args.checkpoint))
            content = _canonical_json(payload) + b"\n"
            if args.output is None:
                sys.stdout.buffer.write(content)
            else:
                args.output.write_bytes(content)
            return 0
    except QualificationError as exc:
        parser.exit(2, f"provider-qualification: {exc}\n")
    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())
