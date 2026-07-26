from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from riverhog_core.runtime_config import (
    DEFAULT_DATABASE_URL,
    DEV_ARCHIVE_PASSPHRASE,
    ArchiveStoreConfig,
    RuntimeConfig,
    load_runtime_config,
)

from tests.unit.db_helpers import sqlite_url


def _config(tmp_path: Path, **overrides: object) -> RuntimeConfig:
    return RuntimeConfig(database_url=sqlite_url(tmp_path / "state.sqlite3"), **overrides)


def test_retrieval_max_lease_covers_the_default_lease(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="RIVERHOG_RETRIEVAL_MAX_LEASE must be at least"):
        _config(
            tmp_path,
            retrieval_default_lease=timedelta(days=8),
            retrieval_max_lease=timedelta(days=7),
        )


def test_restore_required_store_requires_retrieval_cache(tmp_path: Path) -> None:
    deep = _config(tmp_path).archive_store("deep")
    with pytest.raises(ValueError, match="RIVERHOG_RETRIEVAL_CACHE"):
        _config(tmp_path, archive_stores={"deep": replace(deep, read_mode="restore_required")})


def test_load_runtime_config_parses_archive_security_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RIVERHOG_ARCHIVE_REQUIRE_EXPLICIT_PASSPHRASE", "true")
    monkeypatch.setenv("RIVERHOG_ARCHIVE_PASSPHRASE", "archive-secret")
    monkeypatch.setenv("RIVERHOG_ARCHIVE_SCRYPT_WORK_FACTOR", "12")

    config = load_runtime_config()

    assert config.archive_require_explicit_passphrase is True
    assert config.archive_passphrase == "archive-secret"
    assert config.archive_scrypt_work_factor == 12


def test_load_runtime_config_parses_archive_multipart_safeguards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RIVERHOG_ARCHIVE_MULTIPART_MAX_AGE", "96h")
    monkeypatch.setenv("RIVERHOG_ARCHIVE_MULTIPART_SWEEP_INTERVAL", "2h")

    config = load_runtime_config()

    assert config.archive_multipart_max_age == timedelta(hours=96)
    assert config.archive_multipart_sweep_interval == timedelta(hours=2)


def test_load_runtime_config_parses_ingress_cleanup_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RIVERHOG_INGRESS_CLEANUP_CONCURRENCY", "12")
    monkeypatch.setenv("RIVERHOG_INGRESS_CLEANUP_RETRY_DELAY", "7m")
    monkeypatch.setenv("RIVERHOG_INGRESS_CLEANUP_SWEEP_INTERVAL", "15s")

    config = load_runtime_config()

    assert config.ingress_cleanup_concurrency == 12
    assert config.ingress_cleanup_retry_delay == timedelta(minutes=7)
    assert config.ingress_cleanup_sweep_interval == timedelta(seconds=15)


def test_load_runtime_config_parses_lifecycle_event_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RIVERHOG_EVENT_SOURCE", "urn:test:riverhog")
    monkeypatch.setenv("RIVERHOG_EVENT_CONTEXT_RETENTION", "14d")

    config = load_runtime_config()

    assert config.event_source == "urn:test:riverhog"
    assert config.event_context_retention == timedelta(days=14)


def test_load_runtime_config_parses_proof_maturation_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RIVERHOG_OTS_UPGRADE_COMMAND", "custom-ots --calendar example")
    monkeypatch.setenv("RIVERHOG_PROOF_MATURATION_RETRY_DELAY", "3h")
    monkeypatch.setenv("RIVERHOG_PROOF_MATURATION_SWEEP_INTERVAL", "20m")

    config = load_runtime_config()

    assert config.ots_upgrade_command == ("custom-ots", "--calendar", "example")
    assert config.proof_maturation_retry_delay == timedelta(hours=3)
    assert config.proof_maturation_sweep_interval == timedelta(minutes=20)


def test_load_runtime_config_parses_archive_attestation_key_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RIVERHOG_ATTESTATION_SECRET_KEY_FILE", "/run/keys/minisign.key.age")
    monkeypatch.setenv("RIVERHOG_ATTESTATION_PUBLIC_KEY_FILE", "/run/keys/minisign.pub")

    config = load_runtime_config()

    assert config.attestation_secret_key_file == Path("/run/keys/minisign.key.age")
    assert config.attestation_public_key_file == Path("/run/keys/minisign.pub")


def test_archive_attestation_key_configuration_must_be_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RIVERHOG_ATTESTATION_SECRET_KEY_FILE", "/run/keys/minisign.key.age")

    with pytest.raises(ValueError, match="attestation key configuration is incomplete"):
        load_runtime_config()


def test_load_runtime_config_defaults_to_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RIVERHOG_DATABASE_URL", raising=False)
    config = load_runtime_config()
    assert config.database_url == DEFAULT_DATABASE_URL


def test_load_runtime_config_parses_upload_lifecycle_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RIVERHOG_UPLOAD_FILE_TTL", "12h")
    monkeypatch.setenv("RIVERHOG_UPLOAD_EXPIRY_SWEEP_INTERVAL", "45s")

    config = load_runtime_config()

    assert config.upload_file_ttl == timedelta(hours=12)
    assert config.upload_expiry_sweep_interval == timedelta(seconds=45)


def test_load_runtime_config_parses_retrieval_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RIVERHOG_RETRIEVAL_ESTIMATED_LATENCY", "6h")
    monkeypatch.setenv("RIVERHOG_RETRIEVAL_TIER", "standard")
    monkeypatch.setenv("RIVERHOG_RETRIEVAL_INITIAL_INGESTION_LEASE", "20d")
    monkeypatch.setenv("RIVERHOG_RETRIEVAL_DEFAULT_LEASE", "2d")
    monkeypatch.setenv("RIVERHOG_RETRIEVAL_MAX_LEASE", "20d")

    config = load_runtime_config()

    assert config.retrieval_estimated_latency == timedelta(hours=6)
    assert config.retrieval_tier == "standard"
    assert config.retrieval_initial_ingestion_lease == timedelta(days=20)
    assert config.retrieval_default_lease == timedelta(days=2)


def test_load_runtime_config_builds_named_archive_stores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RIVERHOG_ARCHIVE_STORES", "deep,b2")
    monkeypatch.setenv("RIVERHOG_ARCHIVE_WRITE_STORE", "deep")
    monkeypatch.setenv("RIVERHOG_ARCHIVE_READ_ORDER", "b2,deep")
    monkeypatch.setenv("RIVERHOG_ARCHIVE_STORE_B2_ENDPOINT_URL", "https://s3.example.test")
    monkeypatch.setenv("RIVERHOG_ARCHIVE_STORE_B2_REGION", "us-west-004")
    monkeypatch.setenv("RIVERHOG_ARCHIVE_STORE_B2_BUCKET", "riverhog-b2")
    monkeypatch.setenv("RIVERHOG_ARCHIVE_STORE_B2_ACCESS_KEY_ID", "b2-key")
    monkeypatch.setenv("RIVERHOG_ARCHIVE_STORE_B2_SECRET_ACCESS_KEY", "b2-secret")
    monkeypatch.setenv("RIVERHOG_ARCHIVE_STORE_B2_FORCE_PATH_STYLE", "false")
    monkeypatch.setenv("RIVERHOG_ARCHIVE_STORE_B2_PREFIX", "")
    monkeypatch.setenv("RIVERHOG_ARCHIVE_STORE_B2_BACKEND", "b2")
    monkeypatch.setenv("RIVERHOG_ARCHIVE_STORE_B2_STORAGE_CLASS", "STANDARD")
    monkeypatch.setenv("RIVERHOG_ARCHIVE_PASSPHRASE", "archive-secret")

    config = load_runtime_config()

    assert tuple(config.archive_stores) == ("deep", "b2")
    assert config.archive_write_store == "deep"
    assert config.archive_read_order == ("b2", "deep")
    assert config.archive_store("b2").bucket == "riverhog-b2"
    assert config.archive_store("b2").backend == "b2"
    assert config.archive_store("b2").storage_class == "STANDARD"
    assert config.archive_store("b2").prefix == ""


def test_load_runtime_config_enables_cloudfront_downloads_per_aws_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RIVERHOG_ARCHIVE_STORE_DEEP_BACKEND", "aws")
    monkeypatch.setenv(
        "RIVERHOG_ARCHIVE_STORE_DEEP_CLOUDFRONT_BASE_URL",
        "https://archive.example.test/",
    )
    monkeypatch.setenv(
        "RIVERHOG_ARCHIVE_STORE_DEEP_CLOUDFRONT_PUBLIC_KEY_ID",
        "example-key-id",
    )
    monkeypatch.setenv(
        "RIVERHOG_ARCHIVE_STORE_DEEP_CLOUDFRONT_PRIVATE_KEY_PATH",
        "/run/secrets/cloudfront.pem",
    )

    store = load_runtime_config().archive_store("deep")

    assert store.cloudfront_base_url == "https://archive.example.test"
    assert store.cloudfront_public_key_id == "example-key-id"
    assert store.cloudfront_private_key_path == Path("/run/secrets/cloudfront.pem")


def test_load_runtime_config_enables_a_monthly_download_allowance_per_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "RIVERHOG_ARCHIVE_STORE_DEEP_MONTHLY_DOWNLOAD_ALLOWANCE_BYTES",
        "1TB",
    )
    monkeypatch.setenv(
        "RIVERHOG_ARCHIVE_STORE_DEEP_DOWNLOAD_SAFETY_BUFFER_BYTES",
        "50GB",
    )

    store = load_runtime_config().archive_store("deep")

    assert store.monthly_download_allowance_bytes == 1_000_000_000_000
    assert store.download_safety_buffer_bytes == 50_000_000_000


def test_download_safety_buffer_requires_an_allowance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "RIVERHOG_ARCHIVE_STORE_DEEP_DOWNLOAD_SAFETY_BUFFER_BYTES",
        "50GB",
    )

    with pytest.raises(ValueError, match="safety buffer requires a monthly download allowance"):
        load_runtime_config()


def test_download_safety_buffer_must_leave_a_positive_effective_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "RIVERHOG_ARCHIVE_STORE_DEEP_MONTHLY_DOWNLOAD_ALLOWANCE_BYTES",
        "50GB",
    )
    monkeypatch.setenv(
        "RIVERHOG_ARCHIVE_STORE_DEEP_DOWNLOAD_SAFETY_BUFFER_BYTES",
        "50GB",
    )

    with pytest.raises(ValueError, match="safety buffer must be smaller"):
        load_runtime_config()


def test_cloudfront_download_configuration_is_atomic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RIVERHOG_ARCHIVE_STORE_DEEP_BACKEND", "aws")
    monkeypatch.setenv(
        "RIVERHOG_ARCHIVE_STORE_DEEP_CLOUDFRONT_BASE_URL",
        "https://archive.example.test",
    )

    with pytest.raises(ValueError, match="must set base URL, public key id, and private key path"):
        load_runtime_config()


def test_metered_download_source_cannot_be_bypassed_through_a_store_alias(
    tmp_path: Path,
) -> None:
    deep = _config(tmp_path).archive_store("deep")
    metered = replace(
        deep,
        monthly_download_allowance_bytes=1_000,
        download_safety_buffer_bytes=100,
    )
    alias = replace(deep, name="alias")

    with pytest.raises(ValueError, match="metered archive download source.*alias, deep"):
        _config(tmp_path, archive_stores={"deep": metered, "alias": alias})


def test_cloudfront_downloads_require_an_aws_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "RIVERHOG_ARCHIVE_STORE_DEEP_CLOUDFRONT_BASE_URL",
        "https://archive.example.test",
    )
    monkeypatch.setenv(
        "RIVERHOG_ARCHIVE_STORE_DEEP_CLOUDFRONT_PUBLIC_KEY_ID",
        "example-key-id",
    )
    monkeypatch.setenv(
        "RIVERHOG_ARCHIVE_STORE_DEEP_CLOUDFRONT_PRIVATE_KEY_PATH",
        "/run/secrets/cloudfront.pem",
    )

    with pytest.raises(ValueError, match="require the aws backend"):
        load_runtime_config()


@pytest.mark.parametrize(
    "base_url",
    (
        "http://archive.example.test",
        "https://user@archive.example.test",
        "https://archive.example.test?token=secret",
        "https://archive.example.test#fragment",
    ),
)
def test_cloudfront_base_url_requires_a_private_https_origin_contract(
    monkeypatch: pytest.MonkeyPatch,
    base_url: str,
) -> None:
    monkeypatch.setenv("RIVERHOG_ARCHIVE_STORE_DEEP_BACKEND", "aws")
    monkeypatch.setenv("RIVERHOG_ARCHIVE_STORE_DEEP_CLOUDFRONT_BASE_URL", base_url)
    monkeypatch.setenv(
        "RIVERHOG_ARCHIVE_STORE_DEEP_CLOUDFRONT_PUBLIC_KEY_ID",
        "example-key-id",
    )
    monkeypatch.setenv(
        "RIVERHOG_ARCHIVE_STORE_DEEP_CLOUDFRONT_PRIVATE_KEY_PATH",
        "/run/secrets/cloudfront.pem",
    )

    with pytest.raises(ValueError, match="must be an HTTPS URL"):
        load_runtime_config()


def test_remote_archive_store_requires_non_development_passphrase(tmp_path: Path) -> None:
    remote = ArchiveStoreConfig(
        name="b2",
        endpoint_url="https://s3.example.test",
        region="example-region",
        bucket="riverhog",
        access_key_id="example-key",
        secret_access_key="example-secret",
        force_path_style=False,
        prefix="",
        backend="b2",
        storage_class="STANDARD",
    )

    with pytest.raises(ValueError, match="non-development secret for archive store.*b2"):
        _config(
            tmp_path,
            archive_write_store="b2",
            archive_read_order=("b2",),
            archive_stores={"b2": remote},
            archive_passphrase=DEV_ARCHIVE_PASSPHRASE,
        )

    config = _config(
        tmp_path,
        archive_write_store="b2",
        archive_read_order=("b2",),
        archive_stores={"b2": remote},
        archive_passphrase="archive-secret",
    )
    assert config.archive_passphrase == "archive-secret"


def test_configured_archive_store_requires_complete_connection_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RIVERHOG_ARCHIVE_STORES", "deep,b2")
    monkeypatch.setenv("RIVERHOG_ARCHIVE_STORE_B2_ENDPOINT_URL", "")

    with pytest.raises(ValueError, match="archive store b2 has blank required fields"):
        load_runtime_config()
