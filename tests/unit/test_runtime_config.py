from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from riverhog_core.runtime_config import DEFAULT_DATABASE_URL, RuntimeConfig, load_runtime_config
from tests.unit.db_helpers import sqlite_url


def _config(tmp_path: Path, **overrides: object) -> RuntimeConfig:
    return RuntimeConfig(database_url=sqlite_url(tmp_path / "state.sqlite3"), **overrides)


def test_restore_ready_window_covers_webhook_retry(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="RIVERHOG_ARCHIVE_RESTORE_READY_TTL must be at least"):
        _config(
            tmp_path,
            operator_webhook_url="http://example.invalid/operator",
            archive_restore_ready_ttl=timedelta(seconds=5),
            operator_webhook_timeout=timedelta(seconds=5),
            operator_webhook_retry_delay=timedelta(seconds=1),
        )


def test_restore_window_is_independent_without_webhook(tmp_path: Path) -> None:
    config = _config(tmp_path, archive_restore_ready_ttl=timedelta(seconds=4))
    assert config.archive_restore_ready_ttl == timedelta(seconds=4)


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


def test_load_runtime_config_requires_archive_encryption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RIVERHOG_ARCHIVE_ENCRYPTION", "none")
    with pytest.raises(ValueError, match="RIVERHOG_ARCHIVE_ENCRYPTION"):
        load_runtime_config()


def test_load_runtime_config_defaults_to_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RIVERHOG_DATABASE_URL", raising=False)
    config = load_runtime_config()
    assert config.database_url == DEFAULT_DATABASE_URL
    assert config.operator_webhook_reminder_interval == timedelta(hours=24)


def test_load_runtime_config_parses_archive_restore_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RIVERHOG_ARCHIVE_RESTORE_LATENCY", "6h")
    monkeypatch.setenv("RIVERHOG_ARCHIVE_RESTORE_READY_TTL", "2d")
    monkeypatch.setenv("RIVERHOG_ARCHIVE_RESTORE_RETRIEVAL_TIER", "standard")

    config = load_runtime_config()

    assert config.archive_restore_latency == timedelta(hours=6)
    assert config.archive_restore_ready_ttl == timedelta(days=2)
    assert config.archive_restore_retrieval_tier == "standard"
