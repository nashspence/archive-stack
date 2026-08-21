from __future__ import annotations

import ast
from dataclasses import fields, replace
from datetime import timedelta
from pathlib import Path

import pytest
from riverhog_core.collection_plan import CollectionVolumePolicy
from riverhog_core.pack_retrieval import PackRangeRetrievalPolicy
from riverhog_core.runtime_config import (
    DEFAULT_DATABASE_URL,
    RuntimeConfig,
    StorageAdapterRegistration,
    load_runtime_config,
)
from riverhog_core.throughput import ArchiveThroughputTuning

from tests.unit.db_helpers import sqlite_url

_SERVER_SOURCE = Path(__file__).parents[2] / "riverhog" / "server" / "src"


def _config(tmp_path: Path, **overrides: object) -> RuntimeConfig:
    return RuntimeConfig(database_url=sqlite_url(tmp_path / "state.sqlite3"), **overrides)


def test_runtime_configuration_fields_have_explicit_production_consumers() -> None:
    consumed = {
        node.attr
        for path in _SERVER_SOURCE.rglob("*.py")
        if path.name != "runtime_config.py"
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Attribute)
    }
    validation_only = {"archive_require_explicit_passphrase"}
    configuration_fields = {
        field.name
        for model in (
            RuntimeConfig,
            StorageAdapterRegistration,
            CollectionVolumePolicy,
            PackRangeRetrievalPolicy,
            ArchiveThroughputTuning,
        )
        for field in fields(model)
    }

    assert configuration_fields - consumed == validation_only

    witnessed = {
        node.attr
        for root in (Path(__file__).parents[1],)
        for path in root.rglob("*.py")
        if path != Path(__file__)
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Attribute)
    } | {
        node.id
        for root in (Path(__file__).parents[1],)
        for path in root.rglob("*.py")
        if path != Path(__file__)
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Name)
    }
    locally_witnessed = {
        node.attr
        for node in ast.walk(ast.parse(Path(__file__).read_text()))
        if isinstance(node, ast.Attribute)
    } | {
        node.arg
        for node in ast.walk(ast.parse(Path(__file__).read_text()))
        if isinstance(node, ast.keyword) and node.arg is not None
    }
    assert configuration_fields - witnessed - locally_witnessed == set()


def test_public_base_url_is_normalized_and_rejects_ambiguous_authority(
    tmp_path: Path,
) -> None:
    assert (
        _config(tmp_path, public_base_url="http://riverhog.example.test/prefix/").public_base_url
        == "http://riverhog.example.test/prefix"
    )
    with pytest.raises(ValueError, match="RIVERHOG_PUBLIC_BASE_URL"):
        _config(tmp_path, public_base_url="https://user@riverhog.example.test")


def test_retrieval_max_lease_covers_the_default_lease(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="RIVERHOG_RETRIEVAL_MAX_LEASE must be at least"):
        _config(
            tmp_path,
            retrieval_default_lease=timedelta(days=8),
            retrieval_max_lease=timedelta(days=7),
        )


def test_storage_adapter_registration_is_provider_neutral() -> None:
    assert {field.name for field in fields(StorageAdapterRegistration)} == {
        "name",
        "base_url",
        "token_file",
        "allow_insecure_http",
        "maximum_connections",
        "timeout_seconds",
        "monthly_download_allowance_bytes",
        "download_safety_buffer_bytes",
    }
    assert RuntimeConfig().archive_store("archive").allow_insecure_http is False


def test_storage_adapter_http_requires_explicit_opt_in(tmp_path: Path) -> None:
    registration = StorageAdapterRegistration(
        name="archive",
        base_url="http://adapter.example.test",
        token_file=tmp_path / "adapter.token",
    )

    with pytest.raises(ValueError, match="archive store archive adapter URL"):
        _config(tmp_path, archive_stores={"archive": registration})

    configured = _config(
        tmp_path,
        archive_stores={"archive": replace(registration, allow_insecure_http=True)},
        archive_passphrase="archive-secret",
    )
    assert configured.archive_store("archive").base_url == "http://adapter.example.test"


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


def test_load_runtime_config_connects_archive_sweep_and_proof_verifier_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RIVERHOG_ARCHIVE_UPLOAD_SWEEP_INTERVAL", "17s")
    monkeypatch.setenv("RIVERHOG_ARCHIVE_MULTIPART_PART_BYTES", "7MiB")
    monkeypatch.setenv("RIVERHOG_LOG_LEVEL", "debug")
    monkeypatch.setenv("RIVERHOG_OTS_STAMP_COMMAND", "custom-ots --calendar example")
    monkeypatch.setenv("RIVERHOG_OTS_VERIFY_COMMAND", "custom-ots --verify-policy strict")

    config = load_runtime_config()

    assert config.archive_upload_sweep_interval == timedelta(seconds=17)
    assert config.archive_multipart_part_bytes == 7 * 1024 * 1024
    assert config.log_level == "DEBUG"
    assert config.ots_stamp_command == ("custom-ots", "--calendar", "example")
    assert config.ots_verify_command == ("custom-ots", "--verify-policy", "strict")


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
    monkeypatch.setenv("RIVERHOG_ATTESTATION_SECRET_KEY_FILE", "/run/keys/minisign.key")
    monkeypatch.setenv("RIVERHOG_ATTESTATION_PUBLIC_KEY_FILE", "/run/keys/minisign.pub")

    config = load_runtime_config()

    assert config.attestation_secret_key_file == Path("/run/keys/minisign.key")
    assert config.attestation_public_key_file == Path("/run/keys/minisign.pub")


def test_archive_attestation_key_configuration_must_be_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RIVERHOG_ATTESTATION_SECRET_KEY_FILE", "/run/keys/minisign.key")

    with pytest.raises(ValueError, match="attestation key configuration is incomplete"):
        load_runtime_config()


def test_load_runtime_config_defaults_to_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RIVERHOG_DATABASE_URL", raising=False)
    config = load_runtime_config()
    assert config.database_url == DEFAULT_DATABASE_URL


def test_load_runtime_config_parses_retrieval_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RIVERHOG_RETRIEVAL_ESTIMATED_LATENCY", "6h")
    monkeypatch.setenv("RIVERHOG_RETRIEVAL_CACHE_NEW_ARCHIVE_ENABLED", "false")
    monkeypatch.setenv("RIVERHOG_RETRIEVAL_CACHE_NEW_ARCHIVE_LEASE", "3d")
    monkeypatch.setenv("RIVERHOG_RETRIEVAL_DEFAULT_LEASE", "2d")
    monkeypatch.setenv("RIVERHOG_RETRIEVAL_MAX_LEASE", "20d")
    monkeypatch.setenv("RIVERHOG_RETRIEVAL_PENDING_TIMEOUT", "4d")
    monkeypatch.setenv("RIVERHOG_RETRIEVAL_CACHE_SWEEP_INTERVAL", "7m")
    monkeypatch.setenv("RIVERHOG_RETRIEVAL_RESTORE_POLL_INTERVAL", "11m")

    config = load_runtime_config()

    assert config.retrieval_estimated_latency == timedelta(hours=6)
    assert config.retrieval_cache_new_archive_enabled is False
    assert config.retrieval_cache_new_archive_lease == timedelta(days=3)
    assert config.retrieval_default_lease == timedelta(days=2)
    assert config.retrieval_max_lease == timedelta(days=20)
    assert config.retrieval_pending_timeout == timedelta(days=4)
    assert config.retrieval_cache_sweep_interval == timedelta(minutes=7)
    assert config.retrieval_restore_poll_interval == timedelta(minutes=11)


def test_load_runtime_config_builds_named_archive_stores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RIVERHOG_ARCHIVE_STORES", "deep,b2")
    monkeypatch.setenv("RIVERHOG_ARCHIVE_WRITE_STORE", "deep")
    monkeypatch.setenv("RIVERHOG_ARCHIVE_READ_ORDER", "b2,deep")
    monkeypatch.setenv("RIVERHOG_ARCHIVE_STORE_DEEP_ADAPTER_URL", "https://deep.example.test")
    monkeypatch.setenv("RIVERHOG_ARCHIVE_STORE_DEEP_ADAPTER_TOKEN_FILE", "/run/secrets/deep.token")
    monkeypatch.setenv("RIVERHOG_ARCHIVE_STORE_B2_ADAPTER_URL", "https://b2.example.test/")
    monkeypatch.setenv("RIVERHOG_ARCHIVE_STORE_B2_ADAPTER_TOKEN_FILE", "/run/secrets/b2.token")
    monkeypatch.setenv("RIVERHOG_ARCHIVE_STORE_B2_ADAPTER_MAX_CONNECTIONS", "48")
    monkeypatch.setenv("RIVERHOG_ARCHIVE_STORE_B2_ADAPTER_TIMEOUT_SECONDS", "75.5")
    monkeypatch.setenv("RIVERHOG_ARCHIVE_PASSPHRASE", "archive-secret")

    config = load_runtime_config()

    assert tuple(config.archive_stores) == ("deep", "b2")
    assert config.archive_write_store == "deep"
    assert config.archive_read_order == ("b2", "deep")
    assert config.archive_store("b2") == StorageAdapterRegistration(
        name="b2",
        base_url="https://b2.example.test",
        token_file=Path("/run/secrets/b2.token"),
        maximum_connections=48,
        timeout_seconds=75.5,
    )


def test_load_runtime_config_enables_a_monthly_download_allowance_per_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "RIVERHOG_ARCHIVE_STORE_ARCHIVE_MONTHLY_DOWNLOAD_ALLOWANCE_BYTES",
        "1TB",
    )
    monkeypatch.setenv(
        "RIVERHOG_ARCHIVE_STORE_ARCHIVE_DOWNLOAD_SAFETY_BUFFER_BYTES",
        "50GB",
    )

    store = load_runtime_config().archive_store("archive")

    assert store.monthly_download_allowance_bytes == 1_000_000_000_000
    assert store.download_safety_buffer_bytes == 50_000_000_000


def test_download_safety_buffer_requires_an_allowance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "RIVERHOG_ARCHIVE_STORE_ARCHIVE_DOWNLOAD_SAFETY_BUFFER_BYTES",
        "50GB",
    )

    with pytest.raises(ValueError, match="safety buffer requires a monthly download allowance"):
        load_runtime_config()


def test_download_safety_buffer_must_leave_a_positive_effective_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "RIVERHOG_ARCHIVE_STORE_ARCHIVE_MONTHLY_DOWNLOAD_ALLOWANCE_BYTES",
        "50GB",
    )
    monkeypatch.setenv(
        "RIVERHOG_ARCHIVE_STORE_ARCHIVE_DOWNLOAD_SAFETY_BUFFER_BYTES",
        "50GB",
    )

    with pytest.raises(ValueError, match="safety buffer must be smaller"):
        load_runtime_config()


def test_metered_download_source_cannot_be_bypassed_through_a_store_alias(
    tmp_path: Path,
) -> None:
    archive = _config(tmp_path).archive_store("archive")
    metered = replace(
        archive,
        monthly_download_allowance_bytes=1_000,
        download_safety_buffer_bytes=100,
    )
    alias = replace(archive, name="alias")

    with pytest.raises(ValueError, match="metered archive download source.*alias, archive"):
        _config(tmp_path, archive_stores={"archive": metered, "alias": alias})


@pytest.mark.parametrize(
    "base_url",
    (
        "http://archive.example.test",
        "https://user@archive.example.test",
        "https://archive.example.test?token=secret",
        "https://archive.example.test#fragment",
    ),
)
def test_storage_adapter_url_requires_an_unambiguous_https_authority(
    monkeypatch: pytest.MonkeyPatch,
    base_url: str,
) -> None:
    monkeypatch.setenv("RIVERHOG_ARCHIVE_STORE_ARCHIVE_ADAPTER_URL", base_url)
    monkeypatch.setenv(
        "RIVERHOG_ARCHIVE_STORE_ARCHIVE_ADAPTER_TOKEN_FILE", "/run/secrets/archive.token"
    )
    monkeypatch.setenv("RIVERHOG_ARCHIVE_PASSPHRASE", "archive-secret")

    with pytest.raises(ValueError, match="archive store archive adapter URL"):
        load_runtime_config()


def test_configured_archive_store_requires_complete_connection_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RIVERHOG_ARCHIVE_STORES", "deep,b2")
    monkeypatch.setenv("RIVERHOG_ARCHIVE_STORE_DEEP_ADAPTER_URL", "https://deep.example.test")
    monkeypatch.setenv("RIVERHOG_ARCHIVE_STORE_DEEP_ADAPTER_TOKEN_FILE", "/run/secrets/deep.token")

    with pytest.raises(ValueError, match="archive store b2 adapter connection is incomplete"):
        load_runtime_config()


def test_load_runtime_config_builds_retrieval_cache_adapter_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "RIVERHOG_RETRIEVAL_CACHE_ADAPTER_URL", "https://cache.example.test/adapter/"
    )
    monkeypatch.setenv("RIVERHOG_RETRIEVAL_CACHE_ADAPTER_TOKEN_FILE", "/run/secrets/cache.token")
    monkeypatch.setenv("RIVERHOG_RETRIEVAL_CACHE_ADAPTER_MAX_CONNECTIONS", "24")
    monkeypatch.setenv("RIVERHOG_RETRIEVAL_CACHE_ADAPTER_TIMEOUT_SECONDS", "90")

    config = load_runtime_config()

    assert config.retrieval_cache == StorageAdapterRegistration(
        name="retrieval-cache",
        base_url="https://cache.example.test/adapter",
        token_file=Path("/run/secrets/cache.token"),
        maximum_connections=24,
        timeout_seconds=90,
    )


def test_retrieval_cache_adapter_allows_explicit_remote_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RIVERHOG_RETRIEVAL_CACHE_ADAPTER_URL", "http://cache.example.test")
    monkeypatch.setenv("RIVERHOG_RETRIEVAL_CACHE_ADAPTER_TOKEN_FILE", "/run/secrets/cache.token")
    monkeypatch.setenv("RIVERHOG_RETRIEVAL_CACHE_ADAPTER_ALLOW_INSECURE_HTTP", "true")

    config = load_runtime_config()

    assert config.retrieval_cache is not None
    assert config.retrieval_cache.base_url == "http://cache.example.test"
    assert config.retrieval_cache.allow_insecure_http is True


def test_retrieval_cache_adapter_connection_is_atomic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RIVERHOG_RETRIEVAL_CACHE_ADAPTER_TOKEN_FILE", "/run/secrets/cache.token")

    with pytest.raises(ValueError, match="RIVERHOG_RETRIEVAL_CACHE_.*incomplete"):
        load_runtime_config()
