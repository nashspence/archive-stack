from __future__ import annotations

import importlib.util
import json
import sys
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from botocore.session import get_session
from botocore.validate import validate_parameters
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts/provider_qualification.py"
CONFIG = REPO_ROOT / "config/provider-qualification.example.toml"


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("provider_qualification", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def resolved_buckets(module: ModuleType, config) -> tuple:  # type: ignore[no-untyped-def]
    values = {
        definition.name_env: f"qualification-{definition.logical_name}"
        for definition in config.buckets
    }
    values.update(
        {
            definition.region_env: "us-west-004" if definition.provider == "b2" else "us-west-2"
            for definition in config.buckets
        }
    )
    return module.resolve_buckets(config, values)


def test_checked_config_is_complete_and_resolves_distinct_provider_roles() -> None:
    module = load_script()
    config = module.load_config(CONFIG)
    values = {
        definition.name_env: f"qualification-{definition.logical_name}"
        for definition in config.buckets
    }
    values.update(
        {
            definition.region_env: "us-west-004" if definition.provider == "b2" else "us-west-2"
            for definition in config.buckets
        }
    )

    buckets = module.resolve_buckets(config, values)

    assert config.cloudfront.enabled is True
    assert config.aws_expiration_days >= module.AWS_DEEP_ARCHIVE_MINIMUM_DAYS
    assert {(item.logical_name, item.provider, item.role) for item in buckets} == {
        ("b2-archive", "b2", "archive"),
        ("b2-retrieval-cache", "b2", "retrieval-cache"),
        ("aws-deep-archive", "aws", "deep-archive"),
    }
    assert len({item.bucket_name for item in buckets}) == 3
    assert len(config.config_sha256) == 64


def test_provider_lifecycle_contracts_bound_run_state() -> None:
    module = load_script()
    config = module.load_config(CONFIG)

    aws = module._aws_lifecycle(config)["Rules"][0]
    b2 = module._b2_lifecycle(config)[0]

    assert aws["Filter"] == {"Prefix": "qualification/"}
    assert aws["Expiration"]["Days"] == 185
    assert aws["AbortIncompleteMultipartUpload"]["DaysAfterInitiation"] == 3
    assert b2 == {
        "fileNamePrefix": "",
        "daysFromUploadingToHiding": None,
        "daysFromHidingToDeleting": 1,
        "daysFromStartingToCancelingUnfinishedLargeFiles": None,
    }


def test_provider_bucket_ownership_refuses_versioned_or_shared_state() -> None:
    module = load_script()
    config = module.load_config(CONFIG)
    bucket = module.ResolvedBucket(
        logical_name="aws-deep-archive",
        provider="aws",
        role="deep-archive",
        bucket_name="qualification-deep",
        region="us-west-2",
    )

    class Client:
        def __init__(self, *, versioning: str | None, extra_tag: bool) -> None:
            self.versioning = versioning
            self.extra_tag = extra_tag

        def head_bucket(self, **_kwargs: object) -> dict[str, object]:
            return {}

        def get_bucket_tagging(self, **_kwargs: object) -> dict[str, object]:
            tags = [
                {"Key": "riverhog-purpose", "Value": module.QUALIFICATION_MARKER},
                {"Key": "riverhog-logical-name", "Value": bucket.logical_name},
            ]
            if self.extra_tag:
                tags.append({"Key": "shared", "Value": "true"})
            return {"TagSet": tags}

        def get_bucket_versioning(self, **_kwargs: object) -> dict[str, object]:
            return {} if self.versioning is None else {"Status": self.versioning}

        def get_bucket_encryption(self, **_kwargs: object) -> dict[str, object]:
            return {
                "ServerSideEncryptionConfiguration": {
                    "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
                }
            }

        def get_public_access_block(self, **_kwargs: object) -> dict[str, object]:
            return {
                "PublicAccessBlockConfiguration": {
                    "BlockPublicAcls": True,
                    "IgnorePublicAcls": True,
                    "BlockPublicPolicy": True,
                    "RestrictPublicBuckets": True,
                }
            }

        def get_bucket_ownership_controls(self, **_kwargs: object) -> dict[str, object]:
            return {"OwnershipControls": {"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]}}

        def get_bucket_lifecycle_configuration(self, **_kwargs: object) -> dict[str, object]:
            return module._aws_lifecycle(config)

    versioned = module.AwsBucketManager(Client(versioning="Suspended", extra_tag=False)).plan(
        bucket, config
    )
    shared = module.AwsBucketManager(Client(versioning=None, extra_tag=True)).plan(bucket, config)

    assert versioned.action == "blocked" and "versioning" in versioned.changes
    assert shared.action == "blocked" and shared.changes == ("unmanaged-tags",)


def test_b2_manual_check_uses_v4_scope_and_never_mutates() -> None:
    module = load_script()
    config = module.load_config(CONFIG)
    bucket = module.ResolvedBucket(
        logical_name="b2-archive",
        provider="b2",
        role="archive",
        bucket_name="qualification-b2",
        region="us-west-004",
    )
    client = module.B2NativeClient(key_id="key-id", application_key="application-key")
    capabilities = sorted(
        {
            "deleteFiles",
            "listBuckets",
            "listFiles",
            "readBucketLifecycleRules",
            "readFiles",
            "writeFiles",
        }
    )
    operations: list[str] = []

    def request(url: str, **kwargs: object) -> dict[str, object]:
        payload = kwargs.get("payload")
        if payload is None:
            return {
                "accountId": "account-id",
                "authorizationToken": "authorization-token",
                "apiInfo": {
                    "storageApi": {
                        "apiUrl": "https://api.example.test",
                        "s3ApiUrl": "https://s3.us-west-004.backblazeb2.com",
                        "allowed": {
                            "buckets": [{"id": "bucket-id", "name": bucket.bucket_name}],
                            "capabilities": capabilities,
                            "namePrefix": "qualification/",
                        },
                    }
                },
            }
        operations.append(url.rsplit("/", maxsplit=1)[-1])
        return {
            "buckets": [
                {
                    "bucketId": "bucket-id",
                    "bucketType": "allPrivate",
                    "corsRules": [],
                    "lifecycleRules": module._b2_lifecycle(config),
                    "defaultServerSideEncryption": {
                        "isClientAuthorizedToRead": True,
                        "value": {"algorithm": None, "mode": None},
                    },
                    "fileLockConfiguration": {
                        "isClientAuthorizedToRead": False,
                        "value": None,
                    },
                }
            ]
        }

    client._request = request  # type: ignore[method-assign]
    checker = module.B2ManualBucketChecker(
        client,
        endpoint_url="https://s3.us-west-004.backblazeb2.com",
    )

    plan = checker.plan(bucket, config)

    assert plan.action == "ready"
    assert plan.changes == ()
    assert operations == ["b2_list_buckets"]


def test_b2_manual_check_blocks_drift_instead_of_reconciling() -> None:
    module = load_script()
    config = module.load_config(CONFIG)
    bucket = module.ResolvedBucket(
        logical_name="b2-archive",
        provider="b2",
        role="archive",
        bucket_name="qualification-b2",
        region="us-west-004",
    )
    client = module.B2NativeClient(key_id="key-id", application_key="application-key")
    client.account_id = "account-id"
    client.allowed_buckets = (("other-id", "other-bucket"),)
    client.capabilities = frozenset(
        {
            "deleteFiles",
            "listBuckets",
            "listFiles",
            "readBucketLifecycleRules",
            "readFiles",
            "writeFiles",
        }
    )
    client.name_prefix = None
    client.s3_api_url = "https://s3.us-west-004.backblazeb2.com"
    client.authorize = lambda: None  # type: ignore[method-assign]
    client.call = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
        "buckets": [
            {
                "bucketType": "allPublic",
                "corsRules": [{"corsRuleName": "unexpected"}],
                "lifecycleRules": [],
                "defaultServerSideEncryption": {
                    "isClientAuthorizedToRead": True,
                    "value": {"algorithm": None, "mode": None},
                },
                "fileLockConfiguration": {
                    "isClientAuthorizedToRead": True,
                    "value": {"isFileLockEnabled": True},
                },
            }
        ]
    }
    checker = module.B2ManualBucketChecker(client, endpoint_url="s3.us-west-004.backblazeb2.com")

    plan = checker.plan(bucket, config)

    assert plan.action == "blocked"
    assert set(plan.changes) == {
        "bucket-scoped-key",
        "cors",
        "endpoint-url",
        "lifecycle",
        "object-lock",
        "private-access",
    }


def test_b2_terminal_cleanup_removes_versions_markers_and_multipart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_script()
    bucket = module.ResolvedBucket(
        logical_name="b2-archive",
        provider="b2",
        role="archive",
        bucket_name="qualification-b2",
        region="us-west-004",
    )

    class Client:
        version_calls = 0
        upload_calls = 0
        aborted: list[tuple[str, str]] = []
        deleted: list[dict[str, str]] = []

        def list_object_versions(self, **kwargs: object) -> dict[str, object]:
            assert kwargs["Prefix"] == "qualification/run-id/"
            self.version_calls += 1
            if self.version_calls == 1:
                return {
                    "IsTruncated": False,
                    "Versions": [{"Key": "qualification/run-id/archive/object", "VersionId": "v1"}],
                    "DeleteMarkers": [
                        {"Key": "qualification/run-id/cache/object", "VersionId": "d1"}
                    ],
                }
            return {"IsTruncated": False, "Versions": [], "DeleteMarkers": []}

        def list_multipart_uploads(self, **kwargs: object) -> dict[str, object]:
            assert kwargs["Prefix"] == "qualification/run-id/"
            self.upload_calls += 1
            if self.upload_calls == 1:
                return {
                    "IsTruncated": False,
                    "Uploads": [
                        {"Key": "qualification/run-id/cache/pending", "UploadId": "upload-1"}
                    ],
                }
            return {"IsTruncated": False, "Uploads": []}

        def abort_multipart_upload(self, **kwargs: object) -> None:
            self.aborted.append((str(kwargs["Key"]), str(kwargs["UploadId"])))

        def delete_objects(self, **kwargs: object) -> dict[str, object]:
            delete = kwargs["Delete"]
            assert isinstance(delete, dict)
            self.deleted.extend(delete["Objects"])  # type: ignore[arg-type]
            return {"Errors": []}

    client = Client()
    monkeypatch.setattr(module, "_runtime_s3_client", lambda *_args, **_kwargs: client)

    module._cleanup_b2_namespace(
        SimpleNamespace(namespace="qualification/run-id"),
        (bucket,),
        {},
    )

    assert client.aborted == [("qualification/run-id/cache/pending", "upload-1")]
    assert client.deleted == [
        {"Key": "qualification/run-id/archive/object", "VersionId": "v1"},
        {"Key": "qualification/run-id/cache/object", "VersionId": "d1"},
    ]
    assert client.version_calls == 2
    assert client.upload_calls == 2


def test_corpus_is_deterministic_and_manifest_is_not_an_uploaded_member(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_script()
    monkeypatch.setattr(
        module,
        "_corpus_layout",
        lambda _profile: (
            ("empty.txt", 0),
            ("packed/readme.txt", len(b"Riverhog provider qualification\n")),
            ("direct/sample.bin", 4097),
        ),
    )
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"

    first = module.create_corpus(first_root, profile="regular")
    second = module.create_corpus(second_root, profile="regular")
    manifest_path = module.corpus_manifest_path(first_root)

    assert first == second
    assert {
        path.relative_to(first_root).as_posix() for path in first_root.rglob("*") if path.is_file()
    } == {item.path for item in first.files}
    assert manifest_path.parent == first_root.parent
    assert manifest_path not in first_root.rglob("*")
    assert module.load_corpus_manifest(manifest_path) == first
    assert (first_root / "empty.txt").read_bytes() == b""


def test_corpus_profiles_exercise_pack_and_direct_multipart_boundaries() -> None:
    module = load_script()

    regular = module._corpus_layout("regular")
    multipart = module._corpus_layout("multipart")

    assert any(byte_count == 0 for _path, byte_count in regular)
    assert any(len(path.encode()) > 100 for path, _byte_count in regular)
    assert any(path.startswith("packed/") for path, _byte_count in regular)
    assert any(path.startswith("direct/") for path, _byte_count in regular)
    assert sum(size for _path, size in multipart) > 128 * module.MIB


def test_checkpoint_is_restartable_tamper_evident_and_emits_bounded_evidence(
    tmp_path: Path,
) -> None:
    module = load_script()
    config = module.load_config(CONFIG)
    corpus = module.CorpusManifest(
        profile="regular",
        files=(module.CorpusFile(path="empty.txt", bytes=0, sha256="0" * 64),),
        bytes=0,
        sha256="1" * 64,
    )
    now = datetime(2026, 8, 12, 12, tzinfo=UTC)
    checkpoint = module.new_checkpoint(
        source_sha="a" * 40,
        source_ref="release/v1",
        config=config,
        corpus=corpus,
        buckets=resolved_buckets(module, config),
        run_id="12345678123456781234567812345678",
        now=now,
    )
    checkpoint = module.bind_qualification_key(
        checkpoint,
        "a" * 16,
        now=now + timedelta(seconds=1),
    )
    phases = (
        "immediate-qualified",
        "deep-archive-uploaded",
        "deep-archive-cache-observed",
        "restore-requested",
        "restore-pending",
        "restored",
        "verified",
        "cleaned",
    )
    for offset, phase in enumerate(phases, 1):
        required = module._REQUIRED_PASS_ASSERTIONS_BY_PHASE.get(phase)
        checkpoint = module.advance_checkpoint(
            checkpoint,
            phase=phase,
            assertions=tuple(sorted(required)) if required else (f"{phase}-contract",),
            collection_id=42 if phase == "immediate-qualified" else None,
            retrieval_job_id="retrieval-42" if phase == "restore-requested" else None,
            artifacts=(
                (
                    module.ArtifactIdentity(
                        surface="b2-archive",
                        sha256="2" * 64,
                        objects=1,
                        bytes=0,
                    ),
                    module.ArtifactIdentity(
                        surface="aws-deep-archive",
                        sha256="2" * 64,
                        objects=1,
                        bytes=0,
                    ),
                    module.ArtifactIdentity(
                        surface="cloudfront-egress",
                        sha256="2" * 64,
                        objects=1,
                        bytes=0,
                    ),
                )
                if phase == "verified"
                else ()
            ),
            now=now + timedelta(minutes=offset),
        )
    checkpoint_path = tmp_path / "checkpoint.json"
    module.write_checkpoint(checkpoint_path, checkpoint)

    restored = module.load_checkpoint(checkpoint_path)
    evidence = module.evidence_from_checkpoint(restored)

    assert restored == checkpoint
    assert restored.collection_id == 42
    assert restored.retrieval_job_id == "retrieval-42"
    assert evidence["status"] == "passed"
    assert evidence["providers"] == [
        {
            "logical_name": "aws-deep-archive",
            "provider": "aws",
            "read_mode": "restore_required",
            "region": "us-west-2",
            "role": "deep-archive",
            "storage_class": "DEEP_ARCHIVE",
        },
        {
            "logical_name": "b2-archive",
            "provider": "b2",
            "read_mode": "immediate",
            "region": "us-west-004",
            "role": "archive",
            "storage_class": "STANDARD",
        },
        {
            "logical_name": "b2-retrieval-cache",
            "provider": "b2",
            "read_mode": "immediate",
            "region": "us-west-004",
            "role": "retrieval-cache",
            "storage_class": "STANDARD",
        },
    ]
    assert evidence["egress"] == {
        "provider": "aws",
        "service": "cloudfront",
        "transport": "signed-https",
    }
    assert evidence["restore"] == {
        "tier": "bulk",
        "copy_days": 3,
        "deadline_at": checkpoint.restore_deadline_at,
    }
    assert evidence["limits"] == {
        "monthly_download_quota_bytes": 2 * 1024 * 1024 * 1024,
        "corpus_bytes": 0,
    }
    assert evidence["retrieval_cache"] == {
        "new_archive_insertion": True,
        "new_archive_lease_seconds": 3600,
        "retrieval_default_lease_seconds": 3 * 24 * 60 * 60,
        "retrieval_max_lease_seconds": 3 * 24 * 60 * 60,
        "pending_timeout_seconds": 72 * 60 * 60,
        "restore_hold_seconds": 24 * 60 * 60,
        "sweep_interval_seconds": 30,
        "restore_poll_interval_seconds": 60,
        "opportunistic_restore_policy": "never",
    }
    assert set(evidence["proof"]["required_assertions"]) <= set(
        evidence["proof"]["observed_assertions"]
    )
    assert {
        "ingress-cache-list-show-status",
        "ingress-cache-retrieval-verified",
        "new-archive-lease-observed",
        "opportunistic-cache-retrieval",
        "retrieval-policy-effective-values",
        "new-archive-cache-expired",
        "cache-sweep-cadence-observed",
        "opportunistic-plan-cost-boundary",
        "b2-retrieval-cache-hydrated",
        "retrieval-cache-list-show-status",
        "retrieval-renewal",
        "restore-poll-cadence-observed",
        "restart-boundary-survived",
        "cloudfront-signed-egress",
        "cloudfront-warm-cache-hit",
        "aws-direct-independent-recovery",
    } <= set(evidence["proof"]["required_assertions"])
    assert evidence["proof"]["artifact_surfaces"] == [
        "aws-deep-archive",
        "b2-archive",
        "cloudfront-egress",
    ]
    assert [item["phase"] for item in evidence["phases"]] == ["created", *phases]
    assert "collection_id" not in evidence
    assert "retrieval_job_id" not in evidence

    incomplete = replace(
        checkpoint,
        history=tuple(
            replace(
                record,
                assertions=tuple(
                    value for value in record.assertions if value != "opportunistic-cache-retrieval"
                ),
            )
            if record.phase == "deep-archive-cache-observed"
            else record
            for record in checkpoint.history
        ),
    )
    with pytest.raises(module.QualificationError, match="missing required assertions"):
        module.evidence_from_checkpoint(incomplete)

    payload = json.loads(checkpoint_path.read_text())
    payload["phase"] = "failed"
    checkpoint_path.write_text(json.dumps(payload))
    with pytest.raises(module.QualificationError, match="phase history|digest"):
        module.load_checkpoint(checkpoint_path)


def test_cloudfront_contract_is_private_signed_and_version_exact() -> None:
    module = load_script()
    config = module.load_config(CONFIG)
    bucket = module.ResolvedBucket(
        logical_name="aws-deep-archive",
        provider="aws",
        role="deep-archive",
        bucket_name="qualification-private-origin",
        region="us-west-2",
    )
    manager = module.CloudFrontManager(
        cloudfront_client=object(),
        s3_client=object(),
        bucket=bucket,
        config=config,
        public_key_pem="public-key",
    )

    distribution = manager._desired_distribution(
        caller_reference="test",
        oac_id="oac-id",
        key_group_id="key-group-id",
    )
    behavior = distribution["DefaultCacheBehavior"]
    origin = distribution["Origins"]["Items"][0]
    policy = manager._bucket_policy_statement("arn:aws:cloudfront::account:distribution/id")

    assert origin["OriginAccessControlId"] == "oac-id"
    assert origin["S3OriginConfig"] == {"OriginAccessIdentity": ""}
    assert behavior["ViewerProtocolPolicy"] == "https-only"
    assert behavior["TrustedKeyGroups"] == {
        "Enabled": True,
        "Quantity": 1,
        "Items": ["key-group-id"],
    }
    assert behavior["ForwardedValues"]["QueryStringCacheKeys"] == {
        "Quantity": 1,
        "Items": ["versionId"],
    }
    assert policy["Principal"] == {"Service": "cloudfront.amazonaws.com"}
    assert policy["Resource"] == ("arn:aws:s3:::qualification-private-origin/qualification/*")
    assert policy["Condition"] == {
        "StringEquals": {"AWS:SourceArn": "arn:aws:cloudfront::account:distribution/id"}
    }
    drifted = json.loads(json.dumps(distribution))
    drifted["DefaultCacheBehavior"]["Compress"] = True
    assert manager._normalize_distribution(drifted) != manager._normalize_distribution(distribution)
    assert manager._distribution_changes(drifted, distribution) == ("Behavior.Compress",)
    provider_defaults = json.loads(json.dumps(distribution))
    provider_defaults["ViewerCertificate"].update(
        {"CertificateSource": "cloudfront", "MinimumProtocolVersion": "TLSv1"}
    )
    provider_defaults["Origins"]["Items"][0]["S3OriginConfig"]["OriginReadTimeout"] = 30
    assert manager._normalize_distribution(provider_defaults) == manager._normalize_distribution(
        distribution
    )
    assert manager._distribution_changes(provider_defaults, distribution) == ()
    nondefault_origin_timeout = json.loads(json.dumps(provider_defaults))
    nondefault_origin_timeout["Origins"]["Items"][0]["S3OriginConfig"]["OriginReadTimeout"] = 45
    assert manager._distribution_changes(nondefault_origin_timeout, distribution) == (
        "Origin.S3OriginConfig.OriginReadTimeout",
    )
    assert manager._pay_as_you_go_billing_changes(distribution) == ()
    paid_features = json.loads(json.dumps(distribution))
    paid_features["Logging"] = {"Enabled": True}
    paid_features["WebACLId"] = "web-acl-id"
    paid_features["DefaultCacheBehavior"]["LambdaFunctionAssociations"] = {
        "Quantity": 1,
        "Items": [{"LambdaFunctionARN": "arn:example", "EventType": "viewer-request"}],
    }
    paid_features["DefaultCacheBehavior"]["RealtimeLogConfigArn"] = "arn:example"
    paid_features["DefaultCacheBehavior"]["FieldLevelEncryptionId"] = "field-encryption-id"
    paid_features["Origins"]["Items"][0]["OriginShield"] = {"Enabled": True}
    assert manager._pay_as_you_go_billing_changes(paid_features) == (
        "access-logging",
        "web-acl",
        "lambda-edge",
        "real-time-logging",
        "field-level-encryption",
        "origin-shield",
    )
    flat_rate_eligible = json.loads(json.dumps(distribution))
    del flat_rate_eligible["DefaultCacheBehavior"]["ForwardedValues"]
    flat_rate_eligible["DefaultCacheBehavior"]["CachePolicyId"] = "cache-policy-id"
    assert manager._pay_as_you_go_billing_changes(flat_rate_eligible) == (
        "pricing-plan-eligibility",
    )
    provider_managed = json.loads(json.dumps(paid_features))
    provider_managed.update(
        {
            "Aliases": {"Quantity": 0},
            "ConnectionMode": "direct",
            "Staging": False,
        }
    )
    updated = manager._updated_distribution(
        provider_managed,
        oac_id="oac-id",
        key_group_id="key-group-id",
    )
    assert updated["CallerReference"] == "test"
    assert updated["ConnectionMode"] == "direct"
    assert updated["Staging"] is False
    assert updated["Aliases"] == {"Quantity": 0}
    assert updated["Logging"] == {
        "Enabled": False,
        "IncludeCookies": False,
        "Bucket": "",
        "Prefix": "",
    }
    assert updated["WebACLId"] == ""
    assert updated["DefaultCacheBehavior"]["LambdaFunctionAssociations"] == {"Quantity": 0}
    assert "RealtimeLogConfigArn" not in updated["DefaultCacheBehavior"]
    assert manager._pay_as_you_go_billing_changes(updated) == ()
    assert manager._normalize_distribution(updated) == manager._normalize_distribution(distribution)
    manager._bucket_policy = lambda: {  # type: ignore[method-assign]
        "Version": "2012-10-17",
        "Statement": [{"Sid": "Unmanaged"}],
    }
    assert manager._bucket_policy_has_unmanaged_statements() is True

    cloudfront = get_session().get_service_model("cloudfront")
    validate_parameters(
        {"DistributionConfig": distribution},
        cloudfront.operation_model("CreateDistribution").input_shape,
    )
    validate_parameters(
        {"OriginAccessControlConfig": manager._desired_oac()},
        cloudfront.operation_model("CreateOriginAccessControl").input_shape,
    )
    validate_parameters(
        {"KeyGroupConfig": manager._desired_key_group("public-key-id")},
        cloudfront.operation_model("CreateKeyGroup").input_shape,
    )


def test_cloudfront_fresh_origin_has_an_empty_policy() -> None:
    module = load_script()
    config = module.load_config(CONFIG)
    bucket = module.ResolvedBucket(
        logical_name="aws-deep-archive",
        provider="aws",
        role="deep-archive",
        bucket_name="qualification-fresh-origin",
        region="us-west-2",
    )

    class FreshOriginError(RuntimeError):
        response = {"Error": {"Code": "NoSuchBucket"}}

    class FreshOrigin:
        def get_bucket_policy(self, **_kwargs: object) -> dict[str, object]:
            raise FreshOriginError("fresh origin")

    manager = module.CloudFrontManager(
        cloudfront_client=object(),
        s3_client=FreshOrigin(),
        bucket=bucket,
        config=config,
        public_key_pem="public-key",
    )

    assert manager._bucket_policy() == {"Version": "2012-10-17", "Statement": []}
    assert manager._bucket_policy_has_unmanaged_statements() is False


def test_cloudfront_finds_paginated_aws_key_group_summary() -> None:
    module = load_script()
    config = module.load_config(CONFIG)
    bucket = module.ResolvedBucket(
        logical_name="aws-deep-archive",
        provider="aws",
        role="deep-archive",
        bucket_name="qualification-private-origin",
        region="us-west-2",
    )

    class CloudFront:
        def __init__(self) -> None:
            self.markers: list[str | None] = []

        def list_key_groups(self, **kwargs: object) -> dict[str, object]:
            marker = kwargs.get("Marker")
            self.markers.append(str(marker) if marker is not None else None)
            if marker is None:
                return {
                    "KeyGroupList": {
                        "Items": [],
                        "NextMarker": "next-page",
                    }
                }
            return {
                "KeyGroupList": {
                    "Items": [
                        {
                            "KeyGroup": {
                                "Id": "key-group-id",
                                "KeyGroupConfig": {
                                    "Name": "qualification-key-group",
                                    "Items": ["public-key-id"],
                                    "Comment": manager.marker,
                                },
                            }
                        }
                    ]
                }
            }

        def get_key_group_config(self, **_kwargs: object) -> dict[str, object]:
            return {
                "KeyGroupConfig": manager._desired_key_group("public-key-id"),
                "ETag": "etag",
            }

        def create_key_group(self, **_kwargs: object) -> dict[str, object]:
            raise AssertionError("an existing key group must not be recreated")

    cloudfront = CloudFront()
    manager = module.CloudFrontManager(
        cloudfront_client=cloudfront,
        s3_client=object(),
        bucket=bucket,
        config=config,
        public_key_pem="public-key",
    )

    assert manager._ensure_key_group("public-key-id") == "key-group-id"
    assert cloudfront.markers == [None, "next-page"]


def test_infrastructure_evidence_uses_logical_names_only() -> None:
    module = load_script()
    plan = module.InfrastructurePlan(
        config_sha256="a" * 64,
        actions=(
            module.InfrastructureAction(
                logical_name="aws-cloudfront-egress",
                provider="aws",
                action="ready",
                changes=(),
            ),
        ),
    )

    encoded = json.dumps(plan.as_dict(), sort_keys=True)

    assert plan.ready is True
    assert "aws-cloudfront-egress" in encoded
    assert "bucket-name" not in encoded
    assert "distribution-id" not in encoded


def test_runtime_environment_uses_scoped_credentials_and_cloudfront(
    tmp_path: Path,
) -> None:
    module = load_script()
    config = module.load_config(CONFIG)
    values: dict[str, str] = {
        "AWS_ACCESS_KEY_ID": "aws-key",
        "AWS_SECRET_ACCESS_KEY": "aws-secret",
        "AWS_SESSION_TOKEN": "aws-session",
        "RIVERHOG_QUALIFICATION_ARCHIVE_PASSPHRASE": "archive-passphrase",
        "RIVERHOG_QUALIFICATION_BOOTSTRAP_TOKEN": "bootstrap-token",
    }
    for definition in config.buckets:
        values[definition.name_env] = f"qualification-{definition.logical_name}"
        values[definition.region_env] = (
            "us-west-004" if definition.provider == "b2" else "us-west-2"
        )
        if definition.provider == "b2":
            prefix = module._credential_prefix(definition.logical_name)
            values[f"{prefix}_ACCESS_KEY_ID"] = f"{definition.logical_name}-key"
            values[f"{prefix}_SECRET_ACCESS_KEY"] = f"{definition.logical_name}-secret"
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_path = tmp_path / "cloudfront.pem"
    private_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    private_path.chmod(0o600)
    values[config.cloudfront.private_key_path_env] = str(private_path)
    public_path = tmp_path / "cloudfront.pub.pem"
    public_path.write_bytes(
        private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    values[config.cloudfront.public_key_path_env] = str(public_path)
    corpus = module.CorpusManifest(profile="regular", files=(), bytes=0, sha256="1" * 64)
    checkpoint = module.new_checkpoint(
        source_sha="a" * 40,
        source_ref="release/v1",
        config=config,
        corpus=corpus,
        buckets=resolved_buckets(module, config),
        run_id="12345678123456781234567812345678",
    )

    class _CloudFront:
        def runtime_configuration(self) -> tuple[str, str]:
            return "https://distribution.example.test", "public-key-id"

    output = tmp_path / "runtime.env"
    module.write_runtime_environment(
        config=config,
        checkpoint=checkpoint,
        buckets=module.resolve_buckets(config, values),
        cloudfront=_CloudFront(),
        values=values,
        output=output,
    )
    text = output.read_text()

    assert output.stat().st_mode & 0o777 == 0o600
    assert f'RIVERHOG_COMPOSE_ENV_FILE="{output.resolve()}"' in text
    assert 'RIVERHOG_PUBLIC_BASE_URL=""' in text
    assert 'RIVERHOG_ARCHIVE_STORES="b2-archive,aws-deep-archive"' in text
    assert 'RIVERHOG_ARCHIVE_WRITE_STORE="b2-archive"' in text
    assert 'RIVERHOG_ARCHIVE_READ_ORDER="aws-deep-archive,b2-archive"' in text
    assert 'RIVERHOG_ARCHIVE_STORE_AWS_DEEP_ARCHIVE_STORAGE_CLASS="DEEP_ARCHIVE"' in text
    assert 'RIVERHOG_ARCHIVE_STORE_AWS_DEEP_ARCHIVE_READ_MODE="restore_required"' in text
    assert 'RIVERHOG_ARCHIVE_STORE_AWS_DEEP_ARCHIVE_SESSION_TOKEN="aws-session"' in text
    assert "RIVERHOG_ARCHIVE_STORE_AWS_DEEP_ARCHIVE_CLOUDFRONT_BASE_URL=" in text
    assert 'RIVERHOG_ARCHIVE_STORE_AWS_DEEP_ARCHIVE_MONTHLY_DOWNLOAD_ALLOWANCE_BYTES="1TB"' in text
    assert 'RIVERHOG_RETRIEVAL_CACHE_BUCKET="qualification-b2-retrieval-cache"' in text
    assert 'RIVERHOG_RETRIEVAL_CACHE_SECRET_ACCESS_KEY="b2-retrieval-cache-secret"' in text
    assert 'RIVERHOG_RETRIEVAL_CACHE_NEW_ARCHIVE_ENABLED="true"' in text
    assert 'RIVERHOG_RETRIEVAL_CACHE_NEW_ARCHIVE_LEASE="1h"' in text
    assert 'RIVERHOG_RETRIEVAL_CACHE_SWEEP_INTERVAL="30s"' in text
    assert 'RIVERHOG_RETRIEVAL_RESTORE_POLL_INTERVAL="1m"' in text
    assert 'RIVERHOG_ARCHIVE_STORE_B2_ARCHIVE_SECRET_ACCESS_KEY="b2-archive-secret"' in text
    assert f'RIVERHOG_ARCHIVE_STORE_B2_ARCHIVE_PREFIX="{checkpoint.namespace}"' in text


def test_runtime_environment_rejects_mismatched_cloudfront_signing_keys(
    tmp_path: Path,
) -> None:
    module = load_script()
    config = module.load_config(CONFIG)
    values: dict[str, str] = {
        "AWS_ACCESS_KEY_ID": "aws-key",
        "AWS_SECRET_ACCESS_KEY": "aws-secret",
        "AWS_SESSION_TOKEN": "aws-session",
        "RIVERHOG_QUALIFICATION_ARCHIVE_PASSPHRASE": "archive-passphrase",
        "RIVERHOG_QUALIFICATION_BOOTSTRAP_TOKEN": "bootstrap-token",
    }
    for definition in config.buckets:
        values[definition.name_env] = f"qualification-{definition.logical_name}"
        values[definition.region_env] = (
            "us-west-004" if definition.provider == "b2" else "us-west-2"
        )
        if definition.provider == "b2":
            prefix = module._credential_prefix(definition.logical_name)
            values[f"{prefix}_ACCESS_KEY_ID"] = "key"
            values[f"{prefix}_SECRET_ACCESS_KEY"] = "secret"
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_path = tmp_path / "cloudfront.pem"
    private_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    private_path.chmod(0o600)
    public_path = tmp_path / "cloudfront.pub.pem"
    public_path.write_bytes(
        other_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    values[config.cloudfront.private_key_path_env] = str(private_path)
    values[config.cloudfront.public_key_path_env] = str(public_path)
    checkpoint = module.new_checkpoint(
        source_sha="a" * 40,
        source_ref="release/v1",
        config=config,
        corpus=module.CorpusManifest(profile="regular", files=(), bytes=0, sha256="1" * 64),
        buckets=module.resolve_buckets(config, values),
    )

    with pytest.raises(module.QualificationError, match="do not match"):
        module.write_runtime_environment(
            config=config,
            checkpoint=checkpoint,
            buckets=module.resolve_buckets(config, values),
            cloudfront=object(),
            values=values,
            output=tmp_path / "runtime.env",
        )


@pytest.mark.parametrize(
    ("profile", "expected_processes", "multipart_observations"),
    (
        ("regular", 1, ()),
        (
            "multipart",
            2,
            ("multipart-client-interrupted", "multipart-client-restarted"),
        ),
    ),
)
def test_official_upload_client_writes_directly_and_resumes_multipart(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    profile: str,
    expected_processes: int,
    multipart_observations: tuple[str, ...],
) -> None:
    module = load_script()
    captured: dict[str, object] = {"commands": []}

    class _Process:
        stdout = None

        def __init__(self, *, interruptible: bool) -> None:
            self.interruptible = interruptible
            self.polls = 0
            self.returncode: int | None = None

        def poll(self) -> int | None:
            if self.returncode is not None:
                return self.returncode
            self.polls += 1
            if self.interruptible or self.polls == 1:
                return None
            self.returncode = 0
            return self.returncode

        def communicate(self) -> tuple[str, str]:
            if self.returncode == 0:
                return '{"collection_id": 42}', ""
            return "", ""

        def terminate(self) -> None:
            self.returncode = -15

        def kill(self) -> None:
            self.returncode = -9

        def wait(self, *, timeout: int) -> int:
            assert timeout == 30
            assert self.returncode is not None
            return self.returncode

    def popen(command, **kwargs):  # type: ignore[no-untyped-def]
        commands = captured["commands"]
        assert isinstance(commands, list)
        commands.append(command)
        captured["environment"] = kwargs["env"]
        return _Process(interruptible=profile == "multipart" and len(commands) == 1)

    root = tmp_path / "corpus"
    root.mkdir()
    resolved_root = str(root.resolve())

    class _Api:
        def list_collection_upload_sessions(self, **_kwargs: object) -> dict[str, object]:
            return {"uploads": [{"collection_id": 42, "ingest_source": resolved_root}]}

        def get_collection_upload_session(self, _collection_id: int) -> dict[str, object]:
            return {"state": "uploading"}

        def list_collection_upload_session_files(
            self, _collection_id: int, **_kwargs: object
        ) -> dict[str, object]:
            return {"files": [{"path": "file.txt"}]}

        def list_collection_upload_session_volumes(self, _collection_id: int) -> dict[str, object]:
            return {"volumes": [{"volume_id": "volume-1"}]}

        def get_collection_upload_session_volume(
            self, _collection_id: int, _volume_id: str
        ) -> dict[str, object]:
            return {"units": [{"unit": 0}]}

        def get_collection_upload_session_unit(
            self, _collection_id: int, _volume_id: str, unit: int
        ) -> dict[str, object]:
            return {"unit": unit, "state": "committed"}

    monkeypatch.setattr(module.shutil, "which", lambda _name: "/usr/bin/riverhog")
    monkeypatch.setattr(module.subprocess, "Popen", popen)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)

    collection_id, observations = module._upload_collection_with_observation(
        _Api(),
        root=root,
        checkpoint=SimpleNamespace(
            run_id="qualification-run",
            collection_id=None,
            corpus_profile=profile,
        ),
        base_url="http://127.0.0.1:8000",
        token="token",
        allow_insecure_http=True,
    )

    commands = captured["commands"]
    assert isinstance(commands, list)
    assert len(commands) == expected_processes
    for command in commands:
        archive_store_index = command.index("--archive-store")
        assert command[archive_store_index + 1] == "b2-archive"
        idempotency_index = command.index("--idempotency-key")
        assert command[idempotency_index + 1] == ("provider-qualification:qualification-run")
    assert collection_id == 42
    assert set(observations) == {
        "committed-unit-readback",
        *multipart_observations,
        "registered-file-list",
        "session-show",
        "unit-readback",
        "volume-list",
        "volume-show",
    }


def test_operator_advances_across_short_restore_invocations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_script()
    config = module.load_config(CONFIG)
    corpus_root = tmp_path / "corpus"
    corpus_root.mkdir()
    (corpus_root / "file.txt").write_bytes(b"content")
    corpus = module.CorpusManifest(
        profile="regular",
        files=(
            module.CorpusFile(
                path="file.txt",
                bytes=7,
                sha256="ed7002b439e9ac845f22357d822bac1444730fbdb6016d3ec9432297b9ec9f73",
            ),
        ),
        bytes=7,
        sha256="1" * 64,
    )
    checkpoint = module.new_checkpoint(
        source_sha="a" * 40,
        source_ref="release/v1",
        config=config,
        corpus=corpus,
        buckets=resolved_buckets(module, config),
        run_id="12345678123456781234567812345678",
    )
    checkpoint_path = tmp_path / "checkpoint.json"
    module.write_checkpoint(checkpoint_path, checkpoint)
    values = {
        definition.name_env: f"qualification-{definition.logical_name}"
        for definition in config.buckets
    }
    values.update(
        {
            definition.region_env: "us-west-004" if definition.provider == "b2" else "us-west-2"
            for definition in config.buckets
        }
    )
    values["RIVERHOG_QUALIFICATION_ARCHIVE_PASSPHRASE"] = "passphrase"
    values["RIVERHOG_QUALIFICATION_BOOTSTRAP_TOKEN"] = "bootstrap"
    calls: list[str] = []

    class _Api:
        ready = False

        def get_collection_upload_session(self, collection_id: int) -> dict[str, object]:
            assert collection_id == 42
            return {"state": "finalized", "files_total": 1}

        def plan_retrieval(self, files, **kwargs) -> dict[str, object]:  # type: ignore[no-untyped-def]
            assert files == ((42, "file.txt"),)
            assert kwargs["lease_seconds"] == 3 * 24 * 60 * 60
            return {
                "etag": "plan",
                "lease_seconds": kwargs["lease_seconds"],
                "requires_restore": True,
                "objects": [{"source_store": "aws-deep-archive", "read_mode": "restore_required"}],
            }

        def create_retrieval_job(self, files, **kwargs) -> dict[str, object]:  # type: ignore[no-untyped-def]
            assert files == ((42, "file.txt"),)
            assert kwargs["plan_etag"] == "plan"
            assert kwargs["lease_seconds"] == 3 * 24 * 60 * 60
            return {"id": "job-42", "state": "requested"}

        def get_retrieval_job(self, job_id: str) -> dict[str, object]:
            assert job_id == "job-42"
            if not self.ready:
                return {"id": job_id, "state": "requested"}
            return {
                "id": job_id,
                "state": "ready",
                "files": [{"collection_id": 42, "path": "file.txt"}],
            }

        def acknowledge_retrieval_job(self, job_id: str) -> dict[str, object]:
            assert job_id == "job-42"
            return {"state": "completed"}

        def renew_retrieval_job(
            self,
            job_id: str,
            *,
            lease_seconds: int,
        ) -> dict[str, object]:
            assert job_id == "job-42"
            assert lease_seconds == 3 * 24 * 60 * 60
            return {
                "id": job_id,
                "state": "ready",
                "files": [{"collection_id": 42, "path": "file.txt"}],
            }

    api = _Api()

    qualification_key_ids: list[str | None] = []

    @contextmanager
    def qualification_api(**kwargs):  # type: ignore[no-untyped-def]
        qualification_key_ids.append(kwargs["qualification_key_id"])
        yield api, "rotated-token", "a" * 16

    monkeypatch.setattr(module, "_qualification_api", qualification_api)
    monkeypatch.setattr(
        module,
        "_upload_collection_with_observation",
        lambda *_args, **_kwargs: (
            42,
            (
                "committed-unit-readback",
                "registered-file-list",
                "session-show",
                "unit-readback",
                "volume-list",
                "volume-show",
            ),
        ),
    )
    for name in (
        "_wait_archive_copy",
        "_assert_resourcesync",
        "_assert_retrieval_cache_surface",
        "_ready_retrieval",
        "_cancel_retrieval",
        "_assert_lifecycle_events",
        "_cancel_archive_copy",
        "_cleanup_b2_namespace",
        "_download_retrieval",
        "_retire_copy",
    ):
        monkeypatch.setattr(
            module,
            name,
            lambda *_args, _name=name, **_kwargs: calls.append(_name),
        )
    monkeypatch.setattr(
        module,
        "_independent_provider_recovery",
        lambda *_args, **_kwargs: (
            calls.append("_independent_provider_recovery") or "a" * 64,
            (f"object:7:{'b' * 64}",),
            7,
        ),
    )
    monkeypatch.setattr(
        module,
        "_verify_cloudfront_egress",
        lambda *_args, **_kwargs: (
            calls.append("_verify_cloudfront_egress") or "a" * 64,
            1,
            7,
        ),
    )

    first = module.operate_qualification(
        config=config,
        checkpoint=checkpoint,
        checkpoint_path=checkpoint_path,
        corpus=corpus,
        corpus_root=corpus_root,
        base_url="http://127.0.0.1:8000",
        allow_insecure_http=True,
        buckets=module.resolve_buckets(config, values),
        cloudfront=object(),
        values=values,
    )

    assert first.phase == "restore-pending"
    assert module.load_checkpoint(checkpoint_path) == first
    assert first.collection_id == 42
    assert first.retrieval_job_id == "job-42"
    assert first.qualification_key_id == "a" * 16

    api.ready = True
    second = module.operate_qualification(
        config=config,
        checkpoint=first,
        checkpoint_path=checkpoint_path,
        corpus=corpus,
        corpus_root=corpus_root,
        base_url="http://127.0.0.1:8000",
        allow_insecure_http=True,
        buckets=module.resolve_buckets(config, values),
        cloudfront=object(),
        values=values,
    )

    assert second.phase == "cleaned"
    assert module.evidence_from_checkpoint(second)["status"] == "passed"
    assert "_verify_cloudfront_egress" in calls
    assert calls.count("_wait_archive_copy") == 1
    assert calls.count("_retire_copy") == 1
    assert calls.count("_cleanup_b2_namespace") == 1
    assert qualification_key_ids == [None, "a" * 16]
    assert {item.surface for item in second.artifacts} == {
        "aws-deep-archive",
        "b2-archive",
        "cloudfront-egress",
    }


def test_qualification_api_rotates_one_durable_job_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_script()
    from riverhog_api_client import client as client_module

    calls: list[tuple[object, ...]] = []

    class _Client:
        def __init__(self, *, token: str, **_kwargs: object) -> None:
            self.token = token

        def create_app_key(self, app: str, **_kwargs: object) -> dict[str, object]:
            calls.append(("create", app))
            return {"id": "a" * 16, "token": "first-token"}

        def rotate_app_key(self, app: str, key_id: str) -> dict[str, object]:
            calls.append(("rotate", app, key_id))
            return {"id": key_id, "token": "rotated-token"}

        def set_app_key_download_quota(
            self,
            app: str,
            key_id: str,
            *,
            monthly_bytes: int,
        ) -> dict[str, object]:
            calls.append(("quota", app, key_id, monthly_bytes))
            return {"monthly_bytes": monthly_bytes}

        def revoke_app_key(self, app: str, key_id: str) -> dict[str, object]:
            calls.append(("revoke", app, key_id))
            return {"id": key_id}

        def close(self) -> None:
            calls.append(("close", self.token))

    monkeypatch.setattr(client_module, "ApiClient", _Client)

    with module._qualification_api(
        base_url="http://127.0.0.1:8000",
        bootstrap_token="bootstrap",
        allow_insecure_http=True,
        qualification_key_id=None,
    ) as (_api, token, key_id):
        assert (token, key_id) == ("first-token", "a" * 16)

    with module._qualification_api(
        base_url="http://127.0.0.1:8000",
        bootstrap_token="bootstrap",
        allow_insecure_http=True,
        qualification_key_id="a" * 16,
    ) as (_api, token, key_id):
        assert (token, key_id) == ("rotated-token", "a" * 16)

    assert ("create", "provider-qualification") in calls
    assert ("rotate", "provider-qualification", "a" * 16) in calls
    assert not any(call[0] == "revoke" for call in calls)
