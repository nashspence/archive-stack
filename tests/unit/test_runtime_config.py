from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from riverhog_core.runtime_config import (
    DEFAULT_DATABASE_URL,
    DEV_RECOVERY_PAYLOAD_PASSPHRASE,
    RuntimeConfig,
    load_runtime_config,
)


def _base_runtime_config(tmp_path: Path, **overrides: object) -> RuntimeConfig:
    return RuntimeConfig(
        object_store="s3",
        s3_endpoint_url="http://example.invalid:9000",
        s3_region="us-east-1",
        s3_bucket="riverhog",
        s3_access_key_id="test-access",
        s3_secret_access_key="test-secret",
        s3_force_path_style=True,
        tusd_base_url="http://example.invalid:1080/files",
        tusd_hook_secret="hook-secret",
        sqlite_path=tmp_path / "state.sqlite3",
        **overrides,
    )


def test_runtime_config_rejects_recovery_ready_ttl_shorter_than_retry_window(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        ValueError,
        match="RIVERHOG_GLACIER_RECOVERY_READY_TTL must be at least",
    ):
        _base_runtime_config(
            tmp_path,
            glacier_recovery_webhook_url="http://example.invalid/webhooks/recovery",
            glacier_recovery_ready_ttl=timedelta(seconds=10),
            glacier_recovery_webhook_retry_delay=timedelta(seconds=1),
        )


def test_runtime_config_allows_recovery_ready_ttl_matching_timeout_plus_retry(
    tmp_path: Path,
) -> None:
    config = _base_runtime_config(
        tmp_path,
        glacier_recovery_webhook_url="http://example.invalid/webhooks/recovery",
        glacier_recovery_ready_ttl=timedelta(seconds=11),
        glacier_recovery_webhook_retry_delay=timedelta(seconds=1),
    )

    assert config.glacier_recovery_webhook_timeout == timedelta(seconds=10)


def test_runtime_config_does_not_enforce_recovery_timing_without_webhook_url(
    tmp_path: Path,
) -> None:
    config = _base_runtime_config(
        tmp_path,
        glacier_recovery_ready_ttl=timedelta(seconds=4),
        glacier_recovery_webhook_retry_delay=timedelta(seconds=1),
    )

    assert config.glacier_recovery_webhook_url is None


def test_load_runtime_config_accepts_explicit_test_recovery_passphrase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RIVERHOG_DB_PATH", str(tmp_path / "state.sqlite3"))
    monkeypatch.setenv("RIVERHOG_RECOVERY_PAYLOAD_REQUIRE_EXPLICIT_PASSPHRASE", "true")
    monkeypatch.setenv("RIVERHOG_RECOVERY_PAYLOAD_PASSPHRASE", "unit-test-secret")

    config = load_runtime_config()

    assert config.recovery_payload_require_explicit_passphrase is True
    assert config.recovery_payload_passphrase == "unit-test-secret"
    assert config.database_url == f"sqlite:///{tmp_path / 'state.sqlite3'}"


def test_load_runtime_config_prefers_database_url_over_legacy_db_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RIVERHOG_DB_PATH", str(tmp_path / "state.sqlite3"))
    monkeypatch.setenv("RIVERHOG_DATABASE_URL", "postgresql+psycopg://unit:unit@postgres/unit")

    config = load_runtime_config()

    assert config.database_url == "postgresql+psycopg://unit:unit@postgres/unit"
    assert config.sqlite_path == tmp_path / "state.sqlite3"


def test_load_runtime_config_defaults_to_postgres_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RIVERHOG_DB_PATH", raising=False)
    monkeypatch.delenv("RIVERHOG_DATABASE_URL", raising=False)

    config = load_runtime_config()

    assert config.database_url == DEFAULT_DATABASE_URL
    assert config.sqlite_path is None


def test_load_runtime_config_rejects_required_default_recovery_passphrase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RIVERHOG_DB_PATH", str(tmp_path / "state.sqlite3"))
    monkeypatch.setenv("RIVERHOG_RECOVERY_PAYLOAD_REQUIRE_EXPLICIT_PASSPHRASE", "true")
    monkeypatch.setenv("RIVERHOG_RECOVERY_PAYLOAD_PASSPHRASE", DEV_RECOVERY_PAYLOAD_PASSPHRASE)

    with pytest.raises(ValueError, match="non-development secret"):
        load_runtime_config()


def test_load_runtime_config_rejects_required_missing_recovery_passphrase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RIVERHOG_DB_PATH", str(tmp_path / "state.sqlite3"))
    monkeypatch.setenv("RIVERHOG_RECOVERY_PAYLOAD_REQUIRE_EXPLICIT_PASSPHRASE", "true")
    monkeypatch.delenv("RIVERHOG_RECOVERY_PAYLOAD_PASSPHRASE", raising=False)

    with pytest.raises(ValueError, match="RIVERHOG_RECOVERY_PAYLOAD_PASSPHRASE"):
        load_runtime_config()
