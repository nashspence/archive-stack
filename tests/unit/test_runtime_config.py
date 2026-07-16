from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from riverhog_core.runtime_config import DEFAULT_DATABASE_URL, RuntimeConfig, load_runtime_config
from tests.unit.db_helpers import sqlite_url


def _config(tmp_path: Path, **overrides: object) -> RuntimeConfig:
    return RuntimeConfig(database_url=sqlite_url(tmp_path / "state.sqlite3"), **overrides)


def test_restore_ready_window_covers_webhook_retry(tmp_path: Path) -> None:
    with pytest.raises(
        ValueError, match="RIVERHOG_ARCHIVE_RESTORE_AVAILABILITY_TTL must be at least"
    ):
        _config(
            tmp_path,
            operator_webhook_url="http://example.invalid/operator",
            archive_restore_availability_ttl=timedelta(seconds=5),
            webhook_timeout=timedelta(seconds=5),
            operator_webhook_retry_delay=timedelta(seconds=1),
        )


def test_restore_window_is_independent_without_webhook(tmp_path: Path) -> None:
    config = _config(tmp_path, archive_restore_availability_ttl=timedelta(seconds=4))
    assert config.archive_restore_availability_ttl == timedelta(seconds=4)


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


def test_load_runtime_config_parses_collection_webhooks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "RIVERHOG_COLLECTION_WEBHOOKS",
        '{"operator":"https://operator.example.test/hook"}',
    )
    monkeypatch.setenv("RIVERHOG_COLLECTION_WEBHOOK_DEFAULT_RECIPIENTS", "operator")

    config = load_runtime_config()

    assert config.collection_webhook_urls == {
        "operator": "https://operator.example.test/hook"
    }
    assert config.collection_webhook_default_recipients == ("operator",)


def test_load_runtime_config_defaults_to_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RIVERHOG_DATABASE_URL", raising=False)
    config = load_runtime_config()
    assert config.database_url == DEFAULT_DATABASE_URL
    assert config.operator_webhook_reminder_interval == timedelta(hours=24)


def test_load_runtime_config_parses_upload_lifecycle_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RIVERHOG_UPLOAD_FILE_TTL", "12h")
    monkeypatch.setenv("RIVERHOG_UPLOAD_EXPIRY_SWEEP_INTERVAL", "45s")

    config = load_runtime_config()

    assert config.upload_file_ttl == timedelta(hours=12)
    assert config.upload_expiry_sweep_interval == timedelta(seconds=45)


def test_load_runtime_config_parses_archive_restore_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RIVERHOG_ARCHIVE_RESTORE_ESTIMATED_LATENCY", "6h")
    monkeypatch.setenv("RIVERHOG_ARCHIVE_RESTORE_AVAILABILITY_TTL", "2d")
    monkeypatch.setenv("RIVERHOG_ARCHIVE_RESTORE_RETRIEVAL_TIER", "standard")

    config = load_runtime_config()

    assert config.archive_restore_estimated_latency == timedelta(hours=6)
    assert config.archive_restore_availability_ttl == timedelta(days=2)
    assert config.archive_restore_retrieval_tier == "standard"


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

    config = load_runtime_config()

    assert tuple(config.archive_stores) == ("deep", "b2")
    assert config.archive_write_store == "deep"
    assert config.archive_read_order == ("b2", "deep")
    assert config.archive_store("b2").bucket == "riverhog-b2"
    assert config.archive_store("b2").backend == "b2"
    assert config.archive_store("b2").storage_class == "STANDARD"
    assert config.archive_store("b2").prefix == ""


def test_configured_archive_store_requires_complete_connection_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RIVERHOG_ARCHIVE_STORES", "deep,b2")
    monkeypatch.setenv("RIVERHOG_ARCHIVE_STORE_B2_ENDPOINT_URL", "")

    with pytest.raises(ValueError, match="archive store b2 has blank required fields"):
        load_runtime_config()
