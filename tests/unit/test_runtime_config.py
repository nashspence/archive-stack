from __future__ import annotations

import ast
from dataclasses import fields
from datetime import timedelta
from pathlib import Path

import pytest
from riverhog_core.collection_plan import CollectionVolumePolicy
from riverhog_core.pack_retrieval import PackRangeRetrievalPolicy
from riverhog_core.runtime_config import (
    DEFAULT_DATABASE_URL,
    DEV_ARCHIVE_PASSPHRASE,
    ArchiveStoreConfig,
    RetrievalCacheConfig,
    RuntimeConfig,
    StorageAdapterRegistration,
    load_runtime_config,
)
from riverhog_core.throughput import ArchiveThroughputTuning
from riverhog_storage_adapter_protocol import StorageProfile, StorageProfilePayload

from tests.unit.db_helpers import sqlite_url

_SERVER_SOURCE = Path(__file__).parents[2] / "riverhog" / "server" / "src"


def _config(tmp_path: Path, **overrides: object) -> RuntimeConfig:
    return RuntimeConfig(database_url=sqlite_url(tmp_path / "state.sqlite3"), **overrides)


def _profile(
    profile_id: str = "example.storage/v1",
    *,
    read_mode: str = "immediate",
    accounting: str = "example-egress",
) -> StorageProfile:
    return StorageProfile.seal(
        StorageProfilePayload(
            profile_id=profile_id,
            read_mode=read_mode,  # type: ignore[arg-type]
            egress_accounting_id=accounting,
        )
    )


def _registration(
    name: str,
    *,
    endpoint_url: str = "https://adapter.example.test",
    read_mode: str = "immediate",
) -> StorageAdapterRegistration:
    profile = _profile(f"example.{name}/v1", read_mode=read_mode)
    return StorageAdapterRegistration(
        name=name,
        endpoint_url=endpoint_url,
        token_file=Path(f"/run/secrets/{name}-token"),
        expected_profile_id=profile.profile_id,
        expected_profile_read_mode=profile.read_mode,
        expected_egress_accounting_id=profile.egress_accounting_id,
        expected_profile_contract_sha256=profile.profile_contract_sha256,
        expected_implementation_id=f"example.{name}-adapter/v1",
    )


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
            ArchiveStoreConfig,
            RetrievalCacheConfig,
            CollectionVolumePolicy,
            PackRangeRetrievalPolicy,
            ArchiveThroughputTuning,
        )
        for field in fields(model)
    }

    assert configuration_fields - consumed == validation_only


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


def test_storage_adapter_plaintext_http_requires_explicit_opt_in() -> None:
    profile = _profile()
    values = {
        "name": "lan",
        "endpoint_url": "http://adapter.lan:8080",
        "token_file": Path("/run/secrets/lan-token"),
        "expected_profile_id": profile.profile_id,
        "expected_profile_read_mode": profile.read_mode,
        "expected_egress_accounting_id": profile.egress_accounting_id,
        "expected_profile_contract_sha256": profile.profile_contract_sha256,
    }
    with pytest.raises(ValueError, match="explicit insecure opt-in"):
        StorageAdapterRegistration(**values)  # type: ignore[arg-type]
    assert StorageAdapterRegistration(
        **values, allow_insecure_http=True  # type: ignore[arg-type]
    ).endpoint_url == "http://adapter.lan:8080"


def test_storage_adapter_profile_snapshot_digest_is_verified() -> None:
    profile = _profile()
    with pytest.raises(ValueError, match="storage profile digest"):
        StorageAdapterRegistration(
            name="archive",
            endpoint_url="https://adapter.example.test",
            token_file=Path("/run/secrets/archive-token"),
            expected_profile_id=profile.profile_id,
            expected_profile_read_mode=profile.read_mode,
            expected_egress_accounting_id=profile.egress_accounting_id,
            expected_profile_contract_sha256="0" * 64,
        )


def test_archive_store_and_cache_reference_known_adapter_registrations(tmp_path: Path) -> None:
    adapter = _registration("archive")
    with pytest.raises(ValueError, match="unknown storage adapter"):
        _config(
            tmp_path,
            storage_adapters={"archive": adapter},
            archive_stores={
                "archive": ArchiveStoreConfig(name="archive", storage_adapter="missing")
            },
            archive_passphrase="archive-secret",
        )
    with pytest.raises(ValueError, match="retrieval cache references an unknown"):
        _config(
            tmp_path,
            storage_adapters={"archive": adapter},
            retrieval_cache=RetrievalCacheConfig(storage_adapter="missing"),
            archive_passphrase="archive-secret",
        )


def test_remote_adapter_requires_non_development_archive_passphrase(tmp_path: Path) -> None:
    remote = _registration("remote")
    store = ArchiveStoreConfig(name="remote", storage_adapter="remote")
    with pytest.raises(ValueError, match="non-development secret.*remote"):
        _config(
            tmp_path,
            storage_adapters={"remote": remote},
            archive_write_store="remote",
            archive_read_order=("remote",),
            archive_stores={"remote": store},
            archive_passphrase=DEV_ARCHIVE_PASSPHRASE,
        )
    assert (
        _config(
            tmp_path,
            storage_adapters={"remote": remote},
            archive_write_store="remote",
            archive_read_order=("remote",),
            archive_stores={"remote": store},
            archive_passphrase="archive-secret",
        ).archive_passphrase
        == "archive-secret"
    )


def test_metered_adapter_cannot_be_bypassed_through_store_alias(tmp_path: Path) -> None:
    adapter = _registration("remote")
    stores = {
        "primary": ArchiveStoreConfig(
            name="primary",
            storage_adapter="remote",
            monthly_download_allowance_bytes=1_000,
            download_safety_buffer_bytes=100,
        ),
        "alias": ArchiveStoreConfig(
            name="alias",
            storage_adapter="remote",
            monthly_download_allowance_bytes=1_000,
            download_safety_buffer_bytes=100,
        ),
    }
    with pytest.raises(ValueError, match="duplicate aliases"):
        _config(
            tmp_path,
            storage_adapters={"remote": adapter},
            archive_write_store="primary",
            archive_read_order=("primary", "alias"),
            archive_stores=stores,
            archive_passphrase="archive-secret",
        )


def test_load_runtime_config_parses_archive_and_retrieval_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RIVERHOG_ARCHIVE_REQUIRE_EXPLICIT_PASSPHRASE", "true")
    monkeypatch.setenv("RIVERHOG_STORAGE_ADAPTER_MAX_CONNECTIONS", "41")
    monkeypatch.setenv("RIVERHOG_ARCHIVE_PASSPHRASE", "archive-secret")
    monkeypatch.setenv("RIVERHOG_ARCHIVE_SCRYPT_WORK_FACTOR", "12")
    monkeypatch.setenv("RIVERHOG_ARCHIVE_MULTIPART_MAX_AGE", "96h")
    monkeypatch.setenv("RIVERHOG_ARCHIVE_MULTIPART_SWEEP_INTERVAL", "2h")
    monkeypatch.setenv("RIVERHOG_ARCHIVE_UPLOAD_SWEEP_INTERVAL", "17s")
    monkeypatch.setenv("RIVERHOG_ARCHIVE_MULTIPART_PART_BYTES", "7MiB")
    monkeypatch.setenv("RIVERHOG_RETRIEVAL_CACHE_NEW_ARCHIVE_ENABLED", "false")
    monkeypatch.setenv("RIVERHOG_RETRIEVAL_CACHE_NEW_ARCHIVE_LEASE", "3d")
    monkeypatch.setenv("RIVERHOG_RETRIEVAL_DEFAULT_LEASE", "2d")
    monkeypatch.setenv("RIVERHOG_RETRIEVAL_MAX_LEASE", "20d")
    monkeypatch.setenv("RIVERHOG_RETRIEVAL_PENDING_TIMEOUT", "4d")
    monkeypatch.setenv("RIVERHOG_RETRIEVAL_CACHE_SWEEP_INTERVAL", "7m")
    monkeypatch.setenv("RIVERHOG_RETRIEVAL_RESTORE_POLL_INTERVAL", "11m")

    config = load_runtime_config()

    assert config.archive_require_explicit_passphrase
    assert config.storage_adapter_max_connections == 41
    assert config.archive_passphrase == "archive-secret"
    assert config.archive_scrypt_work_factor == 12
    assert config.archive_multipart_max_age == timedelta(hours=96)
    assert config.archive_multipart_sweep_interval == timedelta(hours=2)
    assert config.archive_upload_sweep_interval == timedelta(seconds=17)
    assert config.archive_multipart_part_bytes == 7 * 1024**2
    assert not config.retrieval_cache_new_archive_enabled
    assert config.retrieval_cache_new_archive_lease == timedelta(days=3)
    assert config.retrieval_default_lease == timedelta(days=2)
    assert config.retrieval_max_lease == timedelta(days=20)
    assert config.retrieval_pending_timeout == timedelta(days=4)
    assert config.retrieval_cache_sweep_interval == timedelta(minutes=7)
    assert config.retrieval_restore_poll_interval == timedelta(minutes=11)


def test_load_runtime_config_builds_named_adapter_profiles_and_store_bindings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deep = _profile("example.deep/v1", read_mode="restore_required", accounting="aws-egress")
    fast = _profile("example.fast/v1", accounting="b2-egress")
    monkeypatch.setenv("RIVERHOG_STORAGE_ADAPTERS", "deep,fast")
    monkeypatch.setenv("RIVERHOG_STORAGE_ADAPTER_DEEP_ENDPOINT_URL", "https://deep.example")
    monkeypatch.setenv("RIVERHOG_STORAGE_ADAPTER_DEEP_TOKEN_FILE", "/run/secrets/deep")
    monkeypatch.setenv("RIVERHOG_STORAGE_ADAPTER_DEEP_EXPECTED_PROFILE_ID", deep.profile_id)
    monkeypatch.setenv(
        "RIVERHOG_STORAGE_ADAPTER_DEEP_EXPECTED_PROFILE_READ_MODE", deep.read_mode
    )
    monkeypatch.setenv(
        "RIVERHOG_STORAGE_ADAPTER_DEEP_EXPECTED_EGRESS_ACCOUNTING_ID",
        deep.egress_accounting_id,
    )
    monkeypatch.setenv(
        "RIVERHOG_STORAGE_ADAPTER_DEEP_EXPECTED_PROFILE_CONTRACT_SHA256",
        deep.profile_contract_sha256,
    )
    monkeypatch.setenv("RIVERHOG_STORAGE_ADAPTER_FAST_ENDPOINT_URL", "https://fast.example")
    monkeypatch.setenv("RIVERHOG_STORAGE_ADAPTER_FAST_TOKEN_FILE", "/run/secrets/fast")
    monkeypatch.setenv("RIVERHOG_STORAGE_ADAPTER_FAST_EXPECTED_PROFILE_ID", fast.profile_id)
    monkeypatch.setenv(
        "RIVERHOG_STORAGE_ADAPTER_FAST_EXPECTED_PROFILE_READ_MODE", fast.read_mode
    )
    monkeypatch.setenv(
        "RIVERHOG_STORAGE_ADAPTER_FAST_EXPECTED_EGRESS_ACCOUNTING_ID",
        fast.egress_accounting_id,
    )
    monkeypatch.setenv(
        "RIVERHOG_STORAGE_ADAPTER_FAST_EXPECTED_PROFILE_CONTRACT_SHA256",
        fast.profile_contract_sha256,
    )
    monkeypatch.setenv("RIVERHOG_ARCHIVE_STORES", "archive,mirror")
    monkeypatch.setenv("RIVERHOG_ARCHIVE_WRITE_STORE", "archive")
    monkeypatch.setenv("RIVERHOG_ARCHIVE_READ_ORDER", "mirror,archive")
    monkeypatch.setenv("RIVERHOG_ARCHIVE_STORE_ARCHIVE_STORAGE_ADAPTER", "deep")
    monkeypatch.setenv("RIVERHOG_ARCHIVE_STORE_MIRROR_STORAGE_ADAPTER", "fast")
    monkeypatch.setenv("RIVERHOG_RETRIEVAL_CACHE_STORAGE_ADAPTER", "fast")
    monkeypatch.setenv("RIVERHOG_ARCHIVE_PASSPHRASE", "archive-secret")

    config = load_runtime_config()

    assert tuple(config.storage_adapters) == ("deep", "fast")
    assert config.storage_adapter("deep").expected_profile_read_mode == "restore_required"
    assert config.archive_store("archive").storage_adapter == "deep"
    assert config.archive_read_order == ("mirror", "archive")
    assert config.retrieval_cache == RetrievalCacheConfig(storage_adapter="fast")


def test_load_runtime_config_parses_allowance_commands_events_and_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "RIVERHOG_ARCHIVE_STORE_ARCHIVE_MONTHLY_DOWNLOAD_ALLOWANCE_BYTES", "1TB"
    )
    monkeypatch.setenv(
        "RIVERHOG_ARCHIVE_STORE_ARCHIVE_DOWNLOAD_SAFETY_BUFFER_BYTES", "50GB"
    )
    monkeypatch.setenv("RIVERHOG_LOG_LEVEL", "debug")
    monkeypatch.setenv("RIVERHOG_OTS_STAMP_COMMAND", "custom-ots --calendar example")
    monkeypatch.setenv("RIVERHOG_OTS_VERIFY_COMMAND", "custom-ots --verify-policy strict")
    monkeypatch.setenv("RIVERHOG_OTS_UPGRADE_COMMAND", "custom-ots --calendar example")
    monkeypatch.setenv("RIVERHOG_EVENT_SOURCE", "urn:test:riverhog")
    monkeypatch.setenv("RIVERHOG_EVENT_CONTEXT_RETENTION", "14d")
    monkeypatch.setenv("RIVERHOG_PROOF_MATURATION_RETRY_DELAY", "3h")
    monkeypatch.setenv("RIVERHOG_PROOF_MATURATION_SWEEP_INTERVAL", "20m")
    monkeypatch.setenv("RIVERHOG_ATTESTATION_SECRET_KEY_FILE", "/run/keys/minisign.key")
    monkeypatch.setenv("RIVERHOG_ATTESTATION_PUBLIC_KEY_FILE", "/run/keys/minisign.pub")

    config = load_runtime_config()
    store = config.archive_store("archive")

    assert store.monthly_download_allowance_bytes == 1_000_000_000_000
    assert store.download_safety_buffer_bytes == 50_000_000_000
    assert config.log_level == "DEBUG"
    assert config.ots_stamp_command == ("custom-ots", "--calendar", "example")
    assert config.ots_verify_command == ("custom-ots", "--verify-policy", "strict")
    assert config.ots_upgrade_command == ("custom-ots", "--calendar", "example")
    assert config.event_source == "urn:test:riverhog"
    assert config.event_context_retention == timedelta(days=14)
    assert config.proof_maturation_retry_delay == timedelta(hours=3)
    assert config.proof_maturation_sweep_interval == timedelta(minutes=20)
    assert config.attestation_secret_key_file == Path("/run/keys/minisign.key")
    assert config.attestation_public_key_file == Path("/run/keys/minisign.pub")


def test_load_runtime_config_defaults_to_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RIVERHOG_DATABASE_URL", raising=False)
    assert load_runtime_config().database_url == DEFAULT_DATABASE_URL


def test_archive_attestation_key_configuration_must_be_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RIVERHOG_ATTESTATION_SECRET_KEY_FILE", "/run/keys/minisign.key")
    with pytest.raises(ValueError, match="attestation key configuration is incomplete"):
        load_runtime_config()
