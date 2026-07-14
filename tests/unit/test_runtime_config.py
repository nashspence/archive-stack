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
from tests.unit.db_helpers import sqlite_url


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
        database_url=sqlite_url(tmp_path / "state.sqlite3"),
        **overrides,
    )


def test_runtime_config_rejects_recovery_ready_ttl_shorter_than_retry_window(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        ValueError,
        match="RIVERHOG_ARCHIVE_RESTORE_READY_TTL must be at least",
    ):
        _base_runtime_config(
            tmp_path,
            operator_webhook_url="http://example.invalid/webhooks/operator",
            archive_restore_ready_ttl=timedelta(seconds=10),
            operator_webhook_timeout=timedelta(seconds=10),
            operator_webhook_retry_delay=timedelta(seconds=1),
        )


def test_runtime_config_allows_recovery_ready_ttl_matching_timeout_plus_retry(
    tmp_path: Path,
) -> None:
    config = _base_runtime_config(
        tmp_path,
        operator_webhook_url="http://example.invalid/webhooks/operator",
        archive_restore_ready_ttl=timedelta(seconds=6),
        operator_webhook_retry_delay=timedelta(seconds=1),
    )

    assert config.operator_webhook_timeout == timedelta(seconds=5)


def test_runtime_config_does_not_enforce_recovery_timing_without_webhook_url(
    tmp_path: Path,
) -> None:
    config = _base_runtime_config(
        tmp_path,
        archive_restore_ready_ttl=timedelta(seconds=4),
        operator_webhook_retry_delay=timedelta(seconds=1),
    )

    assert config.operator_webhook_url is None


def test_load_runtime_config_accepts_explicit_test_recovery_passphrase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RIVERHOG_RECOVERY_PAYLOAD_REQUIRE_EXPLICIT_PASSPHRASE", "true")
    monkeypatch.setenv("RIVERHOG_RECOVERY_PAYLOAD_PASSPHRASE", "unit-test-secret")

    config = load_runtime_config()

    assert config.recovery_payload_require_explicit_passphrase is True
    assert config.recovery_payload_passphrase == "unit-test-secret"
    assert config.database_url == DEFAULT_DATABASE_URL


def test_load_runtime_config_parses_archive_encryption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RIVERHOG_ARCHIVE_ENCRYPTION", "age-scrypt")
    monkeypatch.setenv("RIVERHOG_ARCHIVE_REQUIRE_EXPLICIT_PASSPHRASE", "true")
    monkeypatch.setenv("RIVERHOG_ARCHIVE_PASSPHRASE", "archive-secret")
    monkeypatch.setenv("RIVERHOG_ARCHIVE_WORK_FACTOR", "12")

    config = load_runtime_config()

    assert config.archive_encryption == "age_scrypt"
    assert config.archive_require_explicit_passphrase is True
    assert config.archive_passphrase == "archive-secret"
    assert config.archive_work_factor == 12


def test_load_runtime_config_rejects_disabled_archive_encryption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RIVERHOG_ARCHIVE_ENCRYPTION", "none")

    with pytest.raises(ValueError, match="RIVERHOG_ARCHIVE_ENCRYPTION"):
        load_runtime_config()


def test_load_runtime_config_rejects_required_missing_archive_passphrase_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RIVERHOG_ARCHIVE_ENCRYPTION", "age_scrypt")
    monkeypatch.setenv("RIVERHOG_ARCHIVE_REQUIRE_EXPLICIT_PASSPHRASE", "true")
    monkeypatch.delenv("RIVERHOG_ARCHIVE_PASSPHRASE", raising=False)

    with pytest.raises(ValueError, match="RIVERHOG_ARCHIVE_PASSPHRASE"):
        load_runtime_config()


def test_load_runtime_config_rejects_removed_db_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RIVERHOG_DB_PATH", str(tmp_path / "state.sqlite3"))

    with pytest.raises(ValueError, match="RIVERHOG_DB_PATH has been removed"):
        load_runtime_config()


def test_load_runtime_config_rejects_non_postgres_database_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RIVERHOG_DB_PATH", raising=False)
    monkeypatch.setenv("RIVERHOG_DATABASE_URL", sqlite_url(tmp_path / "state.sqlite3"))

    with pytest.raises(ValueError, match="RIVERHOG_DATABASE_URL must use postgresql"):
        load_runtime_config()


def test_load_runtime_config_defaults_to_postgres_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RIVERHOG_DB_PATH", raising=False)
    monkeypatch.delenv("RIVERHOG_DATABASE_URL", raising=False)
    monkeypatch.delenv("RIVERHOG_OPERATOR_WEBHOOK_REMINDER_INTERVAL", raising=False)
    monkeypatch.delenv("RIVERHOG_OPERATOR_WEBHOOK_REMINDER_TIME", raising=False)
    monkeypatch.delenv("RIVERHOG_OPERATOR_WEBHOOK_REMINDER_TIMEZONE", raising=False)
    monkeypatch.delenv("RIVERHOG_PLANNER_MIN_FILL_RATIO", raising=False)
    monkeypatch.delenv("RIVERHOG_PLANNER_MIN_FILL_BYTES", raising=False)
    monkeypatch.delenv("RIVERHOG_PLANNER_UNPLANNED_SATURATION_BYTES", raising=False)
    monkeypatch.delenv("RIVERHOG_LOG_LEVEL", raising=False)
    monkeypatch.delenv("RIVERHOG_UPLOAD_SESSION_IDLE_TTL", raising=False)
    monkeypatch.delenv("RIVERHOG_UPLOAD_STAGING_ROOT", raising=False)

    config = load_runtime_config()

    assert config.database_url == DEFAULT_DATABASE_URL
    assert config.planner_min_fill_ratio == 0.99
    assert config.planner_min_fill_bytes == 49_500_000_000
    assert config.planner_unplanned_saturation_bytes == 300_000_000_000
    assert config.recovery_payload_work_factor == 12
    assert config.recovery_payload_max_work_factor == 30
    assert config.log_level == "INFO"
    assert config.operator_webhook_reminder_interval == timedelta(hours=24)
    assert config.operator_webhook_reminder_time is None
    assert config.operator_webhook_reminder_timezone == "UTC"
    assert config.upload_session_idle_ttl == timedelta(hours=168)
    assert config.upload_staging_root == Path(".riverhog/uploads").resolve()


def test_load_runtime_config_parses_planner_runtime_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RIVERHOG_PLANNER_DISC_TARGET_BYTES", "50GB")
    monkeypatch.setenv("RIVERHOG_PLANNER_MIN_FILL_RATIO", "96%")
    monkeypatch.setenv("RIVERHOG_PLANNER_UNPLANNED_SATURATION_BYTES", "300GB")
    monkeypatch.setenv("RIVERHOG_PLANNER_IMAGE_ROOT", str(tmp_path / "planner"))
    monkeypatch.setenv("RIVERHOG_UNBURNED_COLLECTION_BYTES_LIMIT", "500GB")
    monkeypatch.setenv("RIVERHOG_TUSD_APPEND_TIMEOUT_SECONDS", "45.5")
    monkeypatch.setenv("RIVERHOG_S3_MAX_POOL_CONNECTIONS", "40")
    monkeypatch.setenv("RIVERHOG_ARCHIVE_MULTIPART_PART_BYTES", "128MiB")
    monkeypatch.setenv("RIVERHOG_ARCHIVE_MULTIPART_CONCURRENCY", "8")
    monkeypatch.setenv("RIVERHOG_HOT_PROMOTION_CONCURRENCY", "6")
    monkeypatch.setenv("RIVERHOG_HOT_SINGLE_PUT_MAX_BYTES", "32MiB")
    monkeypatch.setenv("RIVERHOG_OPERATOR_WEBHOOK_URL", "http://example.invalid/webhook/riverhog")
    monkeypatch.setenv(
        "RIVERHOG_NOTIFY_WEBHOOKS",
        '{"nash":"http://example.invalid/webhook/nash"}',
    )
    monkeypatch.setenv("RIVERHOG_NOTIFY_WEBHOOK_KATIE", "http://example.invalid/webhook/katie")
    monkeypatch.setenv("RIVERHOG_NOTIFY_DEFAULT_RECIPIENTS", "nash,katie")
    monkeypatch.setenv("RIVERHOG_OPERATOR_WEBHOOK_TIMEOUT", "2s")
    monkeypatch.setenv("RIVERHOG_OPERATOR_WEBHOOK_RETRY_DELAY", "3s")
    monkeypatch.setenv("RIVERHOG_OPERATOR_WEBHOOK_REMINDER_INTERVAL", "4s")
    monkeypatch.setenv("RIVERHOG_OPERATOR_WEBHOOK_REMINDER_TIME", "2:03")
    monkeypatch.setenv("RIVERHOG_OPERATOR_WEBHOOK_REMINDER_TIMEZONE", "America/Los_Angeles")
    monkeypatch.setenv("RIVERHOG_LOG_LEVEL", "debug")
    monkeypatch.setenv("RIVERHOG_UPLOAD_SESSION_IDLE_TTL", "12h")
    monkeypatch.setenv("RIVERHOG_UPLOAD_STAGING_ROOT", str(tmp_path / "uploads"))

    config = load_runtime_config()

    assert config.planner_disc_target_bytes == 50_000_000_000
    assert config.planner_min_fill_ratio == 0.96
    assert config.planner_min_fill_bytes == 48_000_000_000
    assert config.planner_unplanned_saturation_bytes == 300_000_000_000
    assert config.planner_image_root == tmp_path / "planner"
    assert config.unburned_collection_bytes_limit == 500_000_000_000
    assert config.tusd_append_timeout_seconds == 45.5
    assert config.s3_max_pool_connections == 40
    assert config.archive_multipart_part_bytes == 128 * 1024**2
    assert config.archive_multipart_concurrency == 8
    assert config.hot_promotion_concurrency == 6
    assert config.hot_single_put_max_bytes == 32 * 1024**2
    assert config.log_level == "DEBUG"
    assert config.operator_webhook_url == "http://example.invalid/webhook/riverhog"
    assert config.notify_webhook_urls == {
        "nash": "http://example.invalid/webhook/nash",
        "katie": "http://example.invalid/webhook/katie",
    }
    assert config.notify_default_recipients == ("nash", "katie")
    assert config.operator_webhook_timeout == timedelta(seconds=2)
    assert config.operator_webhook_retry_delay == timedelta(seconds=3)
    assert config.operator_webhook_reminder_interval == timedelta(seconds=4)
    assert config.operator_webhook_reminder_time == "02:03"
    assert config.operator_webhook_reminder_timezone == "America/Los_Angeles"
    assert config.upload_session_idle_ttl == timedelta(hours=12)
    assert config.upload_staging_root == tmp_path / "uploads"


def test_load_runtime_config_accepts_explicit_planner_min_fill_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RIVERHOG_PLANNER_DISC_TARGET_BYTES", "1GiB")
    monkeypatch.setenv("RIVERHOG_PLANNER_MIN_FILL_BYTES", "900MiB")

    config = load_runtime_config()

    assert config.planner_disc_target_bytes == 1024**3
    assert config.planner_min_fill_bytes == 900 * 1024**2


def test_runtime_config_rejects_planner_min_fill_larger_than_target(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="RIVERHOG_PLANNER_MIN_FILL_BYTES"):
        _base_runtime_config(
            tmp_path,
            planner_disc_target_bytes=100,
            planner_min_fill_bytes=101,
        )


def test_load_runtime_config_rejects_required_default_recovery_passphrase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RIVERHOG_RECOVERY_PAYLOAD_REQUIRE_EXPLICIT_PASSPHRASE", "true")
    monkeypatch.setenv("RIVERHOG_RECOVERY_PAYLOAD_PASSPHRASE", DEV_RECOVERY_PAYLOAD_PASSPHRASE)

    with pytest.raises(ValueError, match="non-development secret"):
        load_runtime_config()


def test_load_runtime_config_rejects_required_missing_recovery_passphrase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RIVERHOG_RECOVERY_PAYLOAD_REQUIRE_EXPLICIT_PASSPHRASE", "true")
    monkeypatch.delenv("RIVERHOG_RECOVERY_PAYLOAD_PASSPHRASE", raising=False)

    with pytest.raises(ValueError, match="RIVERHOG_RECOVERY_PAYLOAD_PASSPHRASE"):
        load_runtime_config()
